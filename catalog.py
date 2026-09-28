#!/usr/bin/env python3
"""Roblox avatar catalog builder.

Searches Roblox usernames for given keywords, downloads each candidate's
current avatar render, and asks Gemini to judge whether it looks
flashy/vibrant/cool. Matches are saved as PNGs named by username; every
user that gets checked is remembered so reruns only look at new people.
"""
import argparse
import base64
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

ROBLOX_SEARCH_URL = "https://users.roblox.com/v1/users/search"
ROBLOX_THUMB_URL = "https://thumbnails.roblox.com/v1/users/avatar"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

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


def search_users(keyword, limit, session):
    users, cursor = [], ""
    while len(users) < limit:
        params = {"keyword": keyword, "limit": min(100, limit)}
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


def judge_avatar(image_bytes, api_key):
    body = {
        "contents": [{
            "parts": [
                {"text": JUDGE_PROMPT},
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode()}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    resp = requests.post(GEMINI_URL, params={"key": api_key}, json=body, timeout=30)
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    verdict = json.loads(text)
    return bool(verdict.get("flashy")), verdict.get("reason", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords", nargs="+", required=True, help="Search terms, e.g. boy yellow red cool")
    parser.add_argument("--per-keyword", type=int, default=25, help="How many users to pull per keyword")
    parser.add_argument("--shuffle", action="store_true", help="Randomize keyword order")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("Set GEMINI_API_KEY (get a free key at https://aistudio.google.com/apikey)")

    session = requests.Session()
    seen = load_seen()
    keywords = list(args.keywords)
    if args.shuffle:
        random.shuffle(keywords)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    added = 0

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
            seen.add(user["id"])
            image_url = thumbs.get(user["id"])
            if not image_url:
                continue
            try:
                img_resp = session.get(image_url, timeout=15)
                img_resp.raise_for_status()
                flashy, reason = judge_avatar(img_resp.content, api_key)
            except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
                print(f"  {user['name']}: skipped ({e})")
                continue

            if flashy:
                out_path = OUTPUT_DIR / f"{user['name']}.png"
                out_path.write_bytes(img_resp.content)
                print(f"  + {user['name']} - {reason}")
                added += 1
            time.sleep(0.5)

        save_seen(seen)

    print(f"\nDone. Added {added} new avatar(s) to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
