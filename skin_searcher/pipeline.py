"""Execution pipeline for group member crawling, keyword search, judging, and avatar saving."""
import random
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import requests

from skin_searcher.config import (
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
from skin_searcher.taste import KnownLooks, SavedLooks, TasteModel, load_feedback, rate_avatar


def save_avatar(user, image, meta=None, saved_looks=None):
    """Saves avatar PNG and updates avatar metadata."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"{user['name']}.png").write_bytes(image)
    if saved_looks is not None:
        saved_looks.add(user["name"], image)
    if meta is not None:
        all_meta = load_metadata()
        all_meta[user["name"]] = {
            "id": user.get("id"),
            "name": user.get("name"),
            "saved_at": int(time.time()),
            **meta,
        }
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

        kept = []
        for user in ready:
            img = images[user["id"]]
            if self.looks.is_junk(img):
                self.seen.add(user["id"])
                self.stats["look like avatars you disliked"] += 1
            else:
                # Check for near duplicates of already cataloged avatars for diversity
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
                print(f"  + {user['name']} [{verdict['score']}/10]{theme_str}{colors_str} - {verdict.get('reason', '')}")
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

                    # Early bail check: save Gemini quota if the group is barren
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

            # Record stats for this group run
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
