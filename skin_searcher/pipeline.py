"""Execution pipeline for group member crawling, keyword search, judging, and avatar saving."""
import random
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import requests

from skin_searcher.config import (
    CATALOG_DIR,
    DEFAULT_SETTINGS,
    EMBED_DIMS,
    EMBED_WORKERS,
    MAX_GROUP_PAGES,
    NAME_RE,
    OUTPUT_DIR,
    load_metadata,
    load_seen,
    save_metadata,
    save_seen,
)
from skin_searcher.group_stats import GroupTracker
from skin_searcher.judge import DailyQuotaExceeded, GeminiJudge, embed_image
from skin_searcher.roblox import (
    fetch_avatar_images,
    find_groups,
    follower_counts,
    group_member_pages,
    resolve_usernames,
    search_users,
)
from skin_searcher.taste import (
    KnownLooks,
    SavedLooks,
    TasteModel,
    is_plain_avatar,
    load_feedback,
    look_hash,
    rate_avatar,
    save_feedback,
)


def save_avatar(user, image, meta=None, saved_looks=None):
    """Saves avatar PNG and updates avatar metadata with user ID and look hash."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"{user['name']}.png").write_bytes(image)
    h = look_hash(image)
    if saved_looks is not None:
        saved_looks.add(user["name"], image)
    all_meta = load_metadata()
    record = {
        "id": user.get("id"),
        "name": user.get("name"),
        "look_hash": h,
        "saved_at": int(time.time()),
    }
    if meta:
        record.update(meta)
    all_meta[user["name"]] = record
    save_metadata(all_meta)


class CandidateFeed:
    """Loads fresh, not-yet-judged avatars in the background for the discovery page."""

    def __init__(self, session, terms, ready_target=30):
        self.session, self.terms, self.ready_target = session, terms, ready_target
        self.ready = deque()
        self.shown = {}
        self.handed = set()
        self.finished = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.looks = KnownLooks()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _streams(self):
        streams = []
        for term in self.terms:
            try:
                groups = find_groups(self.session, term)
            except requests.RequestException:
                continue
            used = 0
            for group in groups:
                if used >= DEFAULT_SETTINGS["groups_per_term"] or self.stop.is_set():
                    break
                pages = group_member_pages(self.session, group["id"])
                try:
                    first = next(pages)
                except (requests.RequestException, StopIteration):
                    continue
                streams.append([pages, first])
                used += 1
        return streams

    def _run(self):
        try:
            streams = self._streams()
            while streams and not self.stop.is_set():
                if len(self.ready) >= self.ready_target:
                    time.sleep(0.4)
                    continue
                stream = streams.pop(0)
                page, stream[1] = stream[1], None
                try:
                    page = page if page is not None else next(stream[0])
                except (requests.RequestException, StopIteration):
                    continue
                streams.append(stream)
                self._queue_page(page)
        finally:
            self.finished = True

    def _queue_page(self, page):
        seen, rated = load_seen(), load_feedback()
        fresh = [u for u in page
                 if u["id"] not in seen and u["id"] not in self.handed and u["name"] not in rated
                 and not u.get("hasVerifiedBadge") and NAME_RE.fullmatch(u["name"])
                 and not (OUTPUT_DIR / f"{u['name']}.png").exists()]
        random.shuffle(fresh)
        fresh = fresh[:30]
        self.handed.update(u["id"] for u in fresh)
        images = fetch_avatar_images(self.session, [u["id"] for u in fresh])
        with self.lock:
            for u in fresh:
                if u["id"] in images and not self.looks.is_junk(images[u["id"]]):
                    self.ready.append({"id": u["id"], "name": u["name"], "image": images[u["id"]]})

    def take(self, n):
        with self.lock:
            items = [self.ready.popleft() for _ in range(min(n, len(self.ready)))]
            for item in items:
                self.shown[item["name"]] = item
            return [i["name"] for i in items], (self.finished and not self.ready)


class AutoRun:
    """Pipeline for evaluating candidates with Gemini, filtering, and saving."""

    def __init__(self, args, session, seen, judge, taste=None, group_tracker=None):
        self.args, self.session, self.seen, self.judge, self.taste = args, session, seen, judge, taste
        self.stats = Counter()
        self.handled = set()
        self.looks = KnownLooks()
        self.saved_looks = SavedLooks()
        self.group_tracker = group_tracker or GroupTracker()

    def process(self, users, limit=None):
        """Processes users and returns (fresh_count, judged_candidates_count, saved_count)."""
        fresh = [u for u in users if u["id"] not in self.seen and u["id"] not in self.handled]
        self.stats["already seen"] += len(users) - len(fresh)
        fresh = fresh[:limit]
        self.handled.update(u["id"] for u in fresh)

        candidates = []
        for user in fresh:
            if user.get("hasVerifiedBadge"):
                self.stats["verified badge"] += 1
                self.seen.add(user["id"])
            else:
                candidates.append(user)

        images = fetch_avatar_images(self.session, [u["id"] for u in candidates])
        ready = [u for u in candidates if u["id"] in images]
        self.stats["no avatar render (retried next run)"] += len(candidates) - len(ready)

        use_vib = getattr(self.args, "use_vibrance_filter", DEFAULT_SETTINGS.get("use_vibrance_filter", 1))
        vib_min = getattr(self.args, "vibrance_min", DEFAULT_SETTINGS.get("vibrance_min", 4))

        kept = []
        for user in ready:
            img = images[user["id"]]
            if self.looks.is_junk(img):
                self.seen.add(user["id"])
                self.stats["look like avatars you disliked"] += 1
            elif use_vib and is_plain_avatar(img, min_colourful=vib_min):
                self.seen.add(user["id"])
                self.stats["plain / lifeless (local color pre-filter)"] += 1
            else:
                is_dup, dup_name = self.saved_looks.is_duplicate(img)
                if is_dup:
                    self.seen.add(user["id"])
                    self.stats[f"near duplicate of saved avatar ({dup_name})"] += 1
                else:
                    kept.append(user)
        ready = kept

        size = self.args.batch_size
        batch_evaluated = len(ready)
        batch_saved = 0
        for i in range(0, len(ready), size):
            saved_in_batch = self._judge_batch(ready[i:i + size], images)
            batch_saved += saved_in_batch
            save_seen(self.seen)
        return len(fresh), batch_evaluated, batch_saved

    def _taste_check(self, passed, images):
        def embed(item):
            try:
                return embed_image(images[item[0]["id"]], self.taste.api_key)
            except DailyQuotaExceeded as e:
                return e

        with ThreadPoolExecutor(EMBED_WORKERS) as pool:
            vecs = list(pool.map(embed, passed))
        for vec in vecs:
            if isinstance(vec, DailyQuotaExceeded):
                raise vec
        kept = []
        for item, vec in zip(passed, vecs):
            if vec is None or len(vec) != EMBED_DIMS:
                self.stats["taste check failed (retried next run)"] += 1
            elif self.taste.accepts(vec):
                kept.append(item)
            else:
                self.seen.add(item[0]["id"])
                self.stats["AI liked it, your taste model didn't"] += 1
        return kept

    def _judge_batch(self, batch, images):
        try:
            verdicts = self.judge.judge([images[u["id"]] for u in batch])
        except DailyQuotaExceeded:
            raise
        except RuntimeError as e:
            print(f"  batch of {len(batch)} not judged ({e}), retried next run")
            self.stats["not judged (retried next run)"] += len(batch)
            return 0

        passed = []
        for idx, user in enumerate(batch):
            verdict = verdicts.get(idx)
            if verdict is None:
                self.stats["not judged (retried next run)"] += 1
            elif verdict["score"] >= self.args.min_score:
                passed.append((user, verdict))
            else:
                self.seen.add(user["id"])
                self.stats["judged, not a match"] += 1

        if self.taste is not None and passed:
            passed = self._taste_check(passed, images)
        if not passed:
            return 0

        counts = follower_counts(self.session, [user for user, _ in passed])
        saved_count = 0
        for user, verdict in passed:
            count = counts.get(user["id"])
            if count is None:
                self.stats["follower lookup failed (retried next run)"] += 1
            elif count > self.args.max_followers:
                self.seen.add(user["id"])
                self.stats[f"matched but over {self.args.max_followers} followers"] += 1
            else:
                save_avatar(user, images[user["id"]], meta=verdict, saved_looks=self.saved_looks)
                self.seen.add(user["id"])
                self.stats["saved"] += 1
                saved_count += 1
                theme_str = f" [{verdict.get('theme')}]" if verdict.get("theme") else ""
                colors_str = f" ({', '.join(verdict.get('dominant_colors', []))})" if verdict.get("dominant_colors") else ""
                flags = []
                if verdict.get("clutter"):
                    flags.append("cluttered")
                if verdict.get("funny"):
                    flags.append("funny")
                flags_str = f" <{', '.join(flags)}>" if flags else ""
                print(f"  + {user['name']} [{verdict['score']}/10]{theme_str}{colors_str}{flags_str} - {verdict.get('reason', '')}")
        return saved_count


def search_keywords(run):
    keywords = list(run.args.keywords)
    if run.args.shuffle:
        random.shuffle(keywords)
    for kw in keywords:
        print(f"Searching '{kw}'...")
        try:
            users = search_users(run.session, kw, run.args.per_keyword)
        except requests.RequestException as e:
            print(f"  search failed: {e}")
            continue
        run.process(users)


def search_groups(run):
    done = set()
    tracker = run.group_tracker
    bail_min_eval = getattr(run.args, "group_bail_min_eval", DEFAULT_SETTINGS["group_bail_min_eval"])
    bail_min_yield = getattr(run.args, "group_bail_min_yield", DEFAULT_SETTINGS["group_bail_min_yield"])

    for term in run.args.groups:
        try:
            groups = find_groups(run.session, term)
        except requests.RequestException as e:
            print(f"Group lookup '{term}' failed: {e}")
            continue
        if not groups:
            print(f"No groups found for '{term}'")
        used = 0
        for group in groups:
            if used >= run.args.groups_per_term:
                break
            if group["id"] in done:
                continue
            done.add(group["id"])

            effective_limit = tracker.get_effective_limit(group["id"], run.args.per_group)
            budget_note = f" (quota adjusted to {effective_limit})" if effective_limit != run.args.per_group else ""
            print(f"Group '{group['name']}' ({group.get('memberCount', 0):,} members){budget_note}...")

            new_checked, group_evaluated, group_saved = 0, 0, 0
            readable = False
            try:
                for page_no, page in enumerate(group_member_pages(run.session, group["id"]), 1):
                    readable = True
                    fresh_n, eval_n, save_n = run.process(page, limit=effective_limit - new_checked)
                    new_checked += fresh_n
                    group_evaluated += eval_n
                    group_saved += save_n

                    # Statistical early bail check
                    bail, reason = tracker.should_early_bail(group_evaluated, group_saved,
                                                             min_eval=bail_min_eval, min_yield=bail_min_yield)
                    if bail:
                        print(f"  [Yield Notice] {reason}")
                        break

                    if new_checked >= effective_limit or page_no >= MAX_GROUP_PAGES:
                        break
            except requests.RequestException as e:
                if not readable:
                    print("  member list is hidden without logging in, trying the next group")
                    continue
                print(f"  member list failed partway: {e}")

            if group_evaluated > 0:
                tracker.record(group["id"], group["name"], group_evaluated, group_saved)
                yield_pct = 100.0 * group_saved / group_evaluated
                print(f"  Group summary: {group_saved} saved out of {group_evaluated} evaluated ({yield_pct:.1f}% yield)")

            used += 1


def run_usernames(args, session, seen):
    names = [n.lstrip("@") for n in args.usernames]
    users = resolve_usernames(session, names)
    counts = follower_counts(session, users)
    keep = []
    for user in users:
        count = counts.get(user["id"])
        if count is None:
            print(f"  {user['name']}: couldn't check follower count, skipped (try again)")
        elif count > args.max_followers:
            print(f"  {user['name']}: skipped ({count:,} followers)")
        else:
            keep.append(user)

    images = fetch_avatar_images(session, [u["id"] for u in keep])
    saved = 0
    saved_looks = SavedLooks()
    for user in keep:
        image = images.get(user["id"])
        if not image:
            print(f"  {user['name']}: no avatar render available, skipped")
            continue
        save_avatar(user, image, meta={"source": "manual_pick"}, saved_looks=saved_looks)
        rate_avatar(user["name"], "like")
        seen.add(user["id"])
        saved += 1
        print(f"  + {user['name']}")
    save_seen(seen)
    print(f"\nDone. Saved {saved} avatar(s) to {OUTPUT_DIR}/")


def refresh_catalog(session):
    """Re-resolves usernames by user ID and detects if saved avatars changed appearance."""
    metadata = load_metadata()
    if not OUTPUT_DIR.is_dir():
        print("Catalog directory does not exist.")
        return

    avatar_files = list(OUTPUT_DIR.glob("*.png"))
    if not avatar_files:
        print("Catalog is currently empty.")
        return

    print(f"\nRefreshing {len(avatar_files)} catalog avatars...")
    user_ids, user_map = [], {}
    for p in avatar_files:
        meta = metadata.get(p.stem, {})
        uid = meta.get("id")
        if uid:
            user_ids.append(uid)
            user_map[uid] = p.stem
        else:
            print(f"  {p.stem}: no user_id recorded, skipped")

    if not user_ids:
        print("No accounts with user IDs to refresh.")
        return

    # Check for renamed accounts
    id_to_current_name = {}
    for i in range(0, len(user_ids), 100):
        batch = user_ids[i:i + 100]
        try:
            resp = session.post(
                "https://users.roblox.com/v1/users",
                json={"userIds": batch, "excludeBannedUsers": False},
                timeout=15,
            )
            if resp.ok:
                for u in resp.json().get("data", []):
                    id_to_current_name[u["id"]] = u["name"]
        except Exception as e:
            print(f"  Username resolution error: {e}")

    renamed = 0
    feedback = load_feedback()
    for uid, old_name in list(user_map.items()):
        new_name = id_to_current_name.get(uid)
        if new_name and new_name != old_name:
            old_file = OUTPUT_DIR / f"{old_name}.png"
            new_file = OUTPUT_DIR / f"{new_name}.png"
            if old_file.is_file():
                old_file.rename(new_file)
                print(f"  [Username Changed] {old_name} -> {new_name}")
                renamed += 1
                if old_name in metadata:
                    meta = metadata.pop(old_name)
                    meta["name"] = new_name
                    metadata[new_name] = meta
                if old_name in feedback:
                    fb_entry = feedback.pop(old_name)
                    feedback[new_name] = fb_entry
                user_map[uid] = new_name

    if renamed:
        save_metadata(metadata)
        save_feedback(feedback)
        print(f"Updated {renamed} renamed account(s).")
    else:
        print("All usernames are up to date.")

    # Check for outfit changes
    print("\nChecking for changed avatar looks...")
    fresh_images = fetch_avatar_images(session, list(user_map.keys()))
    changed = 0
    for uid, name in user_map.items():
        img = fresh_images.get(uid)
        if not img:
            continue
        new_hash = look_hash(img)
        file_path = OUTPUT_DIR / f"{name}.png"
        saved_hash = metadata.get(name, {}).get("look_hash")
        if saved_hash is None and file_path.is_file():
            saved_hash = look_hash(file_path.read_bytes())

        if new_hash is not None and saved_hash is not None:
            dist = bin(new_hash ^ saved_hash).count("1")
            if dist > 6:
                changed += 1
                print(f"  [Look Changed] {name}: visual difference {dist}/144")
                metadata.setdefault(name, {})["look_changed"] = True
                metadata[name]["current_look_hash"] = new_hash

    save_metadata(metadata)
    print(f"\nDone. {changed} avatar(s) detected with changed looks.")


def reset_catalog():
    """Resets the catalog avatars, metadata, and seen users cache."""
    backup_dir = CATALOG_DIR / f"avatars_backup_{int(time.time())}"
    cleared = 0
    if OUTPUT_DIR.is_dir():
        pngs = list(OUTPUT_DIR.glob("*.png"))
        if pngs:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for p in pngs:
                shutil.copyfile(p, backup_dir / p.name)
            for p in pngs:
                p.unlink(missing_ok=True)
            cleared = len(pngs)
            print(f"Backed up {cleared} avatars to {backup_dir.name}/ and cleared {OUTPUT_DIR.name}/")

    save_metadata({})
    save_seen(set())
    print("Catalog reset successfully: cleared saved avatars, metadata, and seen_users.json.")


def run_auto(args, session, seen, api_key):
    judge = GeminiJudge(api_key)
    taste = None
    if args.taste_keep < 100:
        model = TasteModel(api_key, args.taste_keep)
        try:
            model.train()
            taste = model
            print(f"Taste model on: learned from {model.info['perfect']} Perfect + {model.info['disliked']} Disliked ratings. "
                  f"In a self-test it keeps {model.info['kept']}% of your Perfects and rejects {model.info['rejected']}% "
                  f"of your Disliked.\n")
        except ValueError as e:
            print(f"Tip: {e}\n")
        except ImportError:
            print("Taste model skipped: it needs numpy. Run python -m pip install numpy and try again.\n")
        except RuntimeError as e:
            print(f"Taste model skipped: {e}.\n")

    tracker = GroupTracker()
    run = AutoRun(args, session, seen, judge, taste, group_tracker=tracker)
    try:
        if args.keywords:
            search_keywords(run)
        else:
            search_groups(run)
    except DailyQuotaExceeded as e:
        print(f"\n{e} - stopping here, rerun tomorrow to continue.")
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        save_seen(seen)
        tracker.save()

    print(f"\nDone. Saved {run.stats['saved']} new avatar(s) to {OUTPUT_DIR}/")
    for label, count in run.stats.items():
        if label != "saved":
            print(f"  {label}: {count}")
