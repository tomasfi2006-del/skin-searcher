#!/usr/bin/env python3
"""Roblox avatar catalog builder.

  --keywords   search Roblox users by keyword, drop well-known accounts, and
               have Gemini judge the rest in batches; matches are saved.
  --usernames  save the avatars of specific users you picked yourself.

Avatars are saved as catalog/avatars/<username>.png. Users that get a final
result are remembered, so reruns only look at new people.
"""
import argparse
import base64
import json
import os
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

STATE_PATH = Path("catalog/seen_users.json")
OUTPUT_DIR = Path("catalog/avatars")

DEFAULT_MAX_FOLLOWERS = 500
DEFAULT_PER_KEYWORD = 100
DEFAULT_BATCH_SIZE = 10
WORKERS = 8

ROBLOX_SEARCH_URL = "https://users.roblox.com/v1/users/search"
ROBLOX_USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_FOLLOWERS_URL = "https://friends.roblox.com/v1/users/{}/followers/count"
ROBLOX_THUMB_URL = "https://thumbnails.roblox.com/v1/users/avatar"

GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
GEMINI_MIN_INTERVAL = 4.2  # per model: free tier allows 15 requests/min each

JUDGE_PROMPT = """You are helping a Roblox content creator pick avatars to \
feature in recordings by literally copying the look onto themselves. You \
will see {n} avatar renders, each labeled "Avatar N:". Judge each one on its \
own (do not compare them to each other). An avatar passes only if ALL of \
these are true:

1. Has a clear, intentional character concept - not a random mismatch of \
default Roblox part colors.
2. Built around one standout hat/headpiece or signature accessory that \
catches the eye - not a bare head with no distinguishing piece.
3. Uses a deliberate, bold color story: either strong saturated color \
blocking, OR a striking monochrome/high-contrast palette (all black, all \
red, black + one bold accent color, etc). Plain, dull, washed-out, or \
default-Roblox-tone colors do NOT count.
4. Clean silhouette - NOT visually cluttered or overloaded with too many \
stacked accessories/items at once.

Costumes/characters (Sonic, superheroes, Santa, anime references, etc) are \
totally fine and often good - do not penalize for that.

Return one entry per avatar with its number, the verdict, and a short reason."""

VERDICT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "avatar": {"type": "INTEGER"},
            "flashy": {"type": "BOOLEAN"},
            "reason": {"type": "STRING"},
        },
        "required": ["avatar", "flashy", "reason"],
    },
}


class DailyQuotaExceeded(RuntimeError):
    pass


# --- Roblox -----------------------------------------------------------------

def _header_int(resp, name):
    try:
        return int(float(resp.headers[name].split(",")[0]))
    except (KeyError, ValueError):
        return None


class RateGate:
    """Follows Roblox's x-ratelimit-* headers so we wait out a window instead of eating 429s."""

    def __init__(self):
        self._lock = threading.Lock()
        self._blocked_until = {}

    def wait(self, bucket):
        while True:
            with self._lock:
                delay = self._blocked_until.get(bucket, 0) - time.monotonic()
            if delay <= 0:
                return
            time.sleep(delay)

    def update(self, bucket, resp):
        if resp.status_code != 429 and _header_int(resp, "x-ratelimit-remaining") != 0:
            return
        # On a 429, Roblox's retry-after is shorter than the real window, so prefer the reset.
        reset = _header_int(resp, "x-ratelimit-reset") or _header_int(resp, "retry-after") or 5
        with self._lock:
            until = time.monotonic() + reset + 0.5
            self._blocked_until[bucket] = max(self._blocked_until.get(bucket, 0), until)


_gate = RateGate()


def roblox(session, bucket, method, url, **kwargs):
    for _ in range(4):
        _gate.wait(bucket)
        resp = session.request(method, url, timeout=15, **kwargs)
        _gate.update(bucket, resp)
        if resp.status_code != 429:
            break
    resp.raise_for_status()
    return resp


def make_session():
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_maxsize=WORKERS * 2))
    return session


def search_users(session, keyword, limit):
    # Search allows 1 call/min, so always ask for the largest page (100).
    users, cursor = [], None
    while len(users) < limit:
        params = {"keyword": keyword, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = roblox(session, "search", "GET", ROBLOX_SEARCH_URL, params=params).json()
        users.extend(data.get("data", []))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
    return users[:limit]


def resolve_usernames(session, names):
    found = []
    for i in range(0, len(names), 100):
        resp = roblox(session, "usernames", "POST", ROBLOX_USERNAMES_URL,
                      json={"usernames": names[i:i + 100], "excludeBannedUsers": True})
        found.extend(resp.json().get("data", []))
    resolved = {u["requestedUsername"].lower() for u in found}
    for name in names:
        if name.lower() not in resolved:
            print(f"  {name}: not found (typo, banned, or renamed)")
    return found


def follower_counts(session, users):
    """{user_id: count}; count is None when the lookup failed."""
    def one(user):
        try:
            resp = roblox(session, "followers", "GET", ROBLOX_FOLLOWERS_URL.format(user["id"]))
            return user["id"], resp.json()["count"]
        except (requests.RequestException, KeyError, ValueError):
            return user["id"], None

    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(pool.map(one, users))


def fetch_avatar_images(session, user_ids):
    """{user_id: png bytes} for every user whose full-body render is available."""
    urls, pending = {}, list(user_ids)
    for attempt in range(2):
        if attempt:
            time.sleep(2)  # renders that were still "Pending" are usually ready a moment later
        still_pending = []
        for i in range(0, len(pending), 100):
            params = {"userIds": ",".join(map(str, pending[i:i + 100])),
                      "size": "420x420", "format": "Png", "isCircular": "false"}
            try:
                data = roblox(session, "thumbnails", "GET", ROBLOX_THUMB_URL, params=params).json()["data"]
            except (requests.RequestException, KeyError, ValueError):
                continue
            for entry in data:
                if entry.get("state") == "Completed" and entry.get("imageUrl"):
                    urls[entry["targetId"]] = entry["imageUrl"]
                elif entry.get("state") == "Pending":
                    still_pending.append(entry["targetId"])
        pending = still_pending
        if not pending:
            break

    def download(item):
        user_id, url = item
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            return user_id, resp.content
        except requests.RequestException:
            return user_id, None

    with ThreadPoolExecutor(WORKERS) as pool:
        return {uid: img for uid, img in pool.map(download, urls.items()) if img}


# --- Gemini -----------------------------------------------------------------

def _quota_info(resp):
    """(is_daily_limit, retry_delay_seconds) from a Gemini 429."""
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError, AttributeError):
        details = []
    daily, delay = False, None
    for detail in details:
        for violation in detail.get("violations", []):
            if "PerDay" in violation.get("quotaId", ""):
                daily = True
        if "retryDelay" in detail:
            try:
                delay = float(detail["retryDelay"].rstrip("s"))
            except ValueError:
                pass
    if not details and "day" in resp.text.lower():
        daily = True
    return daily, delay


class GeminiJudge:
    """Judges avatars in batches (quotas count requests, not images), rotating models."""

    def __init__(self, api_key):
        self.headers = {"x-goog-api-key": api_key}  # header, not URL, so errors never print the key
        self.models = list(GEMINI_MODELS)
        self.turn = 0
        self.last_call = {}

    def _pace(self, model):
        delay = GEMINI_MIN_INTERVAL - (time.monotonic() - self.last_call.get(model, 0))
        if delay > 0:
            time.sleep(delay)
        self.last_call[model] = time.monotonic()

    def _drop(self, model, why):
        print(f"  {model} {why}" + (", using the other model" if len(self.models) > 1 else ""))
        self.models.remove(model)

    def judge(self, images):
        """Returns {index: (flashy, reason)}; indexes missing from the reply are left out."""
        parts = [{"text": JUDGE_PROMPT.format(n=len(images))}]
        for i, img in enumerate(images, 1):
            parts.append({"text": f"Avatar {i}:"})
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}})
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": VERDICT_SCHEMA},
        }

        start = self.turn % max(len(self.models), 1)
        self.turn += 1
        last_error = "no model available"
        for model in self.models[start:] + self.models[:start]:
            for attempt in range(2):
                self._pace(model)
                try:
                    resp = requests.post(GEMINI_URL.format(model), headers=self.headers, json=body, timeout=60)
                except requests.RequestException as e:
                    last_error = f"{model}: {type(e).__name__}"
                    continue
                if resp.ok:
                    try:
                        parts = resp.json()["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                        verdicts = {}
                        for v in json.loads(text):
                            if isinstance(v.get("avatar"), int) and 1 <= v["avatar"] <= len(images):
                                verdicts[v["avatar"] - 1] = (bool(v.get("flashy")), v.get("reason", ""))
                        return verdicts
                    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
                        last_error = f"{model}: unreadable reply"
                        continue
                if resp.status_code in (401, 403) or "API_KEY_INVALID" in resp.text:
                    sys.exit("Gemini rejected the API key - check GEMINI_API_KEY")
                if resp.status_code == 404:
                    self._drop(model, "isn't available (renamed or retired?)")
                    break
                if resp.status_code == 429:
                    daily, delay = _quota_info(resp)
                    if daily:
                        self._drop(model, "hit its daily quota")
                        break
                    last_error = f"{model}: per-minute limit"
                    time.sleep(delay or GEMINI_MIN_INTERVAL)
                    continue
                last_error = f"{model}: HTTP {resp.status_code}"
                if resp.status_code < 500:
                    break
                time.sleep(2 ** attempt)
        if not self.models:
            raise DailyQuotaExceeded("both Gemini models are out of quota for today")
        raise RuntimeError(last_error)


# --- State ------------------------------------------------------------------

def load_seen():
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text()))
    return set()


def save_seen(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sorted(seen)))


def save_avatar(user, image):
    (OUTPUT_DIR / f"{user['name']}.png").write_bytes(image)


def split_by_followers(session, users, max_followers, stats, seen):
    """Drops accounts over the follower cap; returns the rest."""
    counts = follower_counts(session, users)
    keep = []
    for user in users:
        count = counts.get(user["id"])
        if count is None:
            stats["follower lookup failed (retried next run)"] += 1
        elif count > max_followers:
            stats[f"over {max_followers} followers"] += 1
            seen.add(user["id"])
        else:
            keep.append(user)
    return keep


# --- Modes ------------------------------------------------------------------

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
    for user in keep:
        image = images.get(user["id"])
        if not image:
            print(f"  {user['name']}: no avatar render available, skipped")
            continue
        save_avatar(user, image)
        seen.add(user["id"])
        saved += 1
        print(f"  + {user['name']}")
    save_seen(seen)
    print(f"\nDone. Saved {saved} avatar(s) to {OUTPUT_DIR}/")


def run_keywords(args, session, seen):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Set GEMINI_API_KEY (get a free key at https://aistudio.google.com/apikey)")
    judge = GeminiJudge(api_key)

    keywords = list(args.keywords)
    if args.shuffle:
        random.shuffle(keywords)

    stats = Counter()
    handled = set()
    try:
        for kw in keywords:
            # The next search waits out Roblox's 1/min window while this keyword's
            # candidates get filtered and judged, so that time isn't wasted.
            print(f"Searching '{kw}'...")
            try:
                found = search_users(session, kw, args.per_keyword)
            except requests.RequestException as e:
                print(f"  search failed: {e}")
                continue

            fresh = [u for u in found if u["id"] not in seen and u["id"] not in handled]
            handled.update(u["id"] for u in fresh)
            stats["found"] += len(found)
            stats["already seen"] += len(found) - len(fresh)

            for user in fresh:
                if user.get("hasVerifiedBadge"):
                    stats["verified badge"] += 1
                    seen.add(user["id"])
            fresh = [u for u in fresh if not u.get("hasVerifiedBadge")]

            candidates = split_by_followers(session, fresh, args.max_followers, stats, seen)
            images = fetch_avatar_images(session, [u["id"] for u in candidates])
            ready = [u for u in candidates if u["id"] in images]
            stats["no avatar render (retried next run)"] += len(candidates) - len(ready)

            for i in range(0, len(ready), args.batch_size):
                batch = ready[i:i + args.batch_size]
                try:
                    verdicts = judge.judge([images[u["id"]] for u in batch])
                except DailyQuotaExceeded:
                    raise
                except RuntimeError as e:
                    print(f"  batch of {len(batch)} not judged ({e}), retried next run")
                    stats["not judged (retried next run)"] += len(batch)
                    continue
                for idx, user in enumerate(batch):
                    verdict = verdicts.get(idx)
                    if verdict is None:
                        stats["not judged (retried next run)"] += 1
                        continue
                    seen.add(user["id"])
                    flashy, reason = verdict
                    if flashy:
                        save_avatar(user, images[user["id"]])
                        stats["saved"] += 1
                        print(f"  + {user['name']} - {reason}")
                    else:
                        stats["judged, not a match"] += 1
                save_seen(seen)
    except DailyQuotaExceeded as e:
        print(f"\n{e} - stopping here, rerun tomorrow to continue.")
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        save_seen(seen)

    print(f"\nDone. Saved {stats['saved']} new avatar(s) to {OUTPUT_DIR}/")
    for label, count in stats.items():
        if label != "saved":
            print(f"  {label}: {count}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keywords", nargs="+", help="search terms, e.g. boy yellow red (auto-judged by Gemini)")
    mode.add_argument("--usernames", nargs="+", help="usernames you picked yourself (saved directly, no Gemini)")
    parser.add_argument("--per-keyword", type=int, default=DEFAULT_PER_KEYWORD,
                        help=f"users to pull per keyword; each 100 costs one Roblox search minute (default {DEFAULT_PER_KEYWORD})")
    parser.add_argument("--shuffle", action="store_true", help="randomize keyword order")
    parser.add_argument("--max-followers", type=int, default=DEFAULT_MAX_FOLLOWERS,
                        help=f"skip accounts with more followers than this (default {DEFAULT_MAX_FOLLOWERS})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"avatars per Gemini request (default {DEFAULT_BATCH_SIZE})")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()
    seen = load_seen()
    if args.usernames:
        run_usernames(args, session, seen)
    else:
        run_keywords(args, session, seen)


if __name__ == "__main__":
    main()
