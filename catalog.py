#!/usr/bin/env python3
"""Roblox avatar catalog builder.

Searches Roblox usernames for given keywords, downloads each candidate's
current avatar render, and asks Gemini to judge whether it looks
flashy/vibrant/cool. Matches are saved as PNGs named by username; every
user that gets checked is remembered so reruns only look at new people.
"""
import argparse
import base64
import itertools
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

ROBLOX_SEARCH_URL = "https://users.roblox.com/v1/users/search"
ROBLOX_USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_THUMB_URL = "https://thumbnails.roblox.com/v1/users/avatar"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")
GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Both gemini-3.5-flash-lite and gemini-3.1-flash-lite cap the free tier at
# 15 requests/min and 500/day EACH. Pace calls per-model to stay under that,
# and alternate which model goes first per avatar so load (and quota use)
# splits ~evenly across both instead of hammering one.
GEMINI_MIN_INTERVAL = 4.2  # seconds between calls to the SAME model (60/15=4, +margin)
_last_call_at = {}
_model_order = itertools.cycle([
    (GEMINI_MODEL, GEMINI_FALLBACK_MODEL),
    (GEMINI_FALLBACK_MODEL, GEMINI_MODEL),
])


class DailyQuotaExceeded(RuntimeError):
    pass


def _wait_for_model_slot(model):
    now = time.monotonic()
    wait = GEMINI_MIN_INTERVAL - (now - _last_call_at.get(model, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call_at[model] = time.monotonic()


STATE_PATH = Path("catalog/seen_users.json")
OUTPUT_DIR = Path("catalog/avatars")

JUDGE_PROMPT = """You are helping a Roblox content creator pick avatars to \
feature in recordings. Look at this Roblox avatar render and judge if it \
looks FLASHY, VIBRANT, and COOL - eye-catching for a video thumbnail or \
clip (bold/saturated colors, striking outfit or accessories, stands out \
visually). Boring, plain, default, or dull-colored avatars do NOT count.

Reply with ONLY compact JSON: {"flashy": true or false, "reason": "one short phrase"}"""


def load_seen():
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text()))
    return set()


def save_seen(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sorted(seen)))


ROBLOX_SEARCH_PAGE_SIZES = (10, 25, 50, 100)


def search_users(keyword, limit, session):
    page_size = next((p for p in ROBLOX_SEARCH_PAGE_SIZES if p >= limit), 100)
    users, cursor = [], ""
    while len(users) < limit:
        params = {"keyword": keyword, "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(ROBLOX_SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        users.extend(data.get("data", []))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(0.3)
    return users[:limit]


def resolve_usernames(usernames, session):
    """Look up userId + canonical name for a list of usernames (handles case)."""
    found = []
    for i in range(0, len(usernames), 100):
        batch = usernames[i:i + 100]
        resp = session.post(
            ROBLOX_USERNAMES_URL,
            json={"usernames": batch, "excludeBannedUsers": True},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        resolved_lower = {u["requestedUsername"].lower() for u in data}
        for name in batch:
            if name.lower() not in resolved_lower:
                print(f"  {name}: not found (typo, banned, or renamed)")
        found.extend(data)
        time.sleep(0.3)
    return found


def fetch_thumbnails(user_ids, session):
    thumbs = {}
    for i in range(0, len(user_ids), 100):
        batch = user_ids[i:i + 100]
        params = {
            "userIds": ",".join(str(u) for u in batch),
            "size": "420x420",
            "format": "Png",
            "isCircular": "false",
        }
        resp = session.get(ROBLOX_THUMB_URL, params=params, timeout=15)
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            if entry.get("state") == "Completed" and entry.get("imageUrl"):
                thumbs[entry["targetId"]] = entry["imageUrl"]
        time.sleep(0.3)
    return thumbs


def judge_avatar(image_bytes, api_key, retries_per_model=2):
    models = next(_model_order)  # alternates which model goes first each call
    body = {
        "contents": [{
            "parts": [
                {"text": JUDGE_PROMPT},
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode()}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    last_error = None
    for model in models:
        url = GEMINI_URL_TEMPLATE.format(model=model)
        for attempt in range(retries_per_model):
            _wait_for_model_slot(model)
            try:
                resp = requests.post(url, params={"key": api_key}, json=body, timeout=30)
            except requests.Timeout:
                last_error = "timeout"
                continue

            if resp.status_code == 429:
                detail = resp.text.lower()
                if "day" in detail or "daily" in detail:
                    raise DailyQuotaExceeded(f"{model} daily quota exhausted")
                last_error = "429 per-minute limit"
                time.sleep(GEMINI_MIN_INTERVAL)  # our pacing should prevent this; back off once more if it slips through
                continue
            if resp.status_code == 503:
                last_error = "503 overloaded"
                if attempt < retries_per_model - 1:
                    time.sleep(2 ** attempt)
                continue

            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            verdict = json.loads(text)
            return bool(verdict.get("flashy")), verdict.get("reason", "")
    raise RuntimeError(f"all Gemini models exhausted ({last_error})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keywords", nargs="+", help="Search terms, e.g. boy yellow red cool (auto-judged by Gemini)")
    mode.add_argument("--usernames", nargs="+", help="Specific usernames you already picked (saved directly, no Gemini)")
    parser.add_argument("--per-keyword", type=int, default=25, help="How many users to pull per keyword (--keywords mode)")
    parser.add_argument("--shuffle", action="store_true", help="Randomize keyword order (--keywords mode)")
    args = parser.parse_args()

    session = requests.Session()
    seen = load_seen()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    added = 0

    if args.usernames:
        names = [n.lstrip("@") for n in args.usernames]
        users = resolve_usernames(names, session)
        thumbs = fetch_thumbnails([u["id"] for u in users], session)
        for user in users:
            image_url = thumbs.get(user["id"])
            if not image_url:
                print(f"  {user['name']}: no avatar render available, skipped")
                continue
            try:
                img_resp = session.get(image_url, timeout=15)
                img_resp.raise_for_status()
            except requests.RequestException as e:
                print(f"  {user['name']}: skipped ({e})")
                continue
            seen.add(user["id"])
            out_path = OUTPUT_DIR / f"{user['name']}.png"
            out_path.write_bytes(img_resp.content)
            print(f"  + {user['name']}")
            added += 1
        save_seen(seen)
        print(f"\nDone. Added {added} new avatar(s) to {OUTPUT_DIR}/")
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Set GEMINI_API_KEY (get a free key at https://aistudio.google.com/apikey)")

    keywords = list(args.keywords)
    if args.shuffle:
        random.shuffle(keywords)

    for kw in keywords:
        print(f"Searching '{kw}'...")
        try:
            users = search_users(kw, args.per_keyword, session)
        except requests.RequestException as e:
            print(f"  search failed: {e}")
            continue

        candidates = [u for u in users if u["id"] not in seen]
        if not candidates:
            print("  nothing new")
            continue

        thumbs = fetch_thumbnails([u["id"] for u in candidates], session)

        for user in candidates:
            image_url = thumbs.get(user["id"])
            if not image_url:
                seen.add(user["id"])
                continue
            try:
                img_resp = session.get(image_url, timeout=15)
                img_resp.raise_for_status()
                flashy, reason = judge_avatar(img_resp.content, api_key)
            except DailyQuotaExceeded as e:
                print(f"\n{e} - stopping here, resume tomorrow once quota resets.")
                save_seen(seen)
                print(f"\nDone. Added {added} new avatar(s) to {OUTPUT_DIR}/")
                return
            except (requests.RequestException, KeyError, json.JSONDecodeError, RuntimeError) as e:
                print(f"  {user['name']}: skipped ({e})")
                seen.add(user["id"])
                continue

            seen.add(user["id"])
            if flashy:
                out_path = OUTPUT_DIR / f"{user['name']}.png"
                out_path.write_bytes(img_resp.content)
                print(f"  + {user['name']} - {reason}")
                added += 1

        save_seen(seen)

    print(f"\nDone. Added {added} new avatar(s) to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
