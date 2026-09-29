#!/usr/bin/env python3
"""Roblox avatar catalog builder.

Run with no options for an interactive menu, or use:

  --groups     pull members of Roblox groups (e.g. outfit/fashion groups) and
               have Gemini judge their avatars in batches; matches are saved.
  --keywords   same, but finds people through Roblox user search (1 search/min).
  --usernames  save the avatars of specific users you picked yourself.
  --gallery    review saved avatars in a grid; your Perfect/Dislike ratings teach the AI.
  --discover   rate fresh avatars from groups with the arrow keys - the fastest way to teach the AI.

Accounts with more than --max-followers followers are always skipped.

Avatars are saved as catalog/avatars/<username>.png. Users that get a final
result are remembered, so reruns only look at new people.
"""
import argparse
import base64
import getpass
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

# Anchored to this file so launching from another folder (e.g. start.bat) still works.
CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
STATE_PATH = CATALOG_DIR / "seen_users.json"
OUTPUT_DIR = CATALOG_DIR / "avatars"
SETTINGS_PATH = CATALOG_DIR / "settings.json"
KEY_PATH = CATALOG_DIR / "gemini_key.txt"
FEEDBACK_PATH = CATALOG_DIR / "feedback.json"
FEEDBACK_DIR = CATALOG_DIR / "feedback"  # liked/ and disliked/ copies used as examples for the AI
PROFILE_PATH = CATALOG_DIR / "taste_profile.txt"  # plain text: you can read and edit it
PROFILE_META_PATH = CATALOG_DIR / "taste_profile.json"
SHEET_SIZE = 24  # avatars per contact sheet when writing the taste profile
PROFILE_MAX_LIKED, PROFILE_MAX_DISLIKED = 72, 144  # rated avatars the AI looks at when writing it
PROFILE_MIN_EACH = 8  # Perfect and Disliked ratings needed before a profile is worth making
PROFILE_REFRESH_AFTER = 25  # new ratings before the profile is rebuilt automatically
DISLIKE_REASONS = ["Too plain", "Messy / random mix", "Not my style"]
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,40}")  # Roblox usernames

DISCOVER_DEFAULT_TERMS = ["outfits", "fashion"]
DEFAULT_SETTINGS = {
    "max_followers": 500,
    "per_group": 300,
    "groups_per_term": 3,
    "per_keyword": 100,
    "batch_size": 10,
    "min_score": 8,
}
MAX_GROUP_PAGES = 50  # stop paging a group after 5,000 members even if most were already seen
WORKERS = 8

ROBLOX_SEARCH_URL = "https://users.roblox.com/v1/users/search"
ROBLOX_USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_FOLLOWERS_URL = "https://friends.roblox.com/v1/users/{}/followers/count"
ROBLOX_THUMB_URL = "https://thumbnails.roblox.com/v1/users/avatar"
ROBLOX_GROUP_URL = "https://groups.roblox.com/v1/groups/{}"
ROBLOX_GROUP_SEARCH_URL = "https://groups.roblox.com/v1/groups/search"
ROBLOX_GROUP_MEMBERS_URL = "https://groups.roblox.com/v1/groups/{}/users"

GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
GEMINI_MIN_INTERVAL = 4.2  # per model: free tier allows 15 requests/min each

JUDGE_PROMPT = """You are helping a Roblox content creator pick avatars to \
feature in recordings by literally copying the look onto themselves. You \
will see {n} avatar renders, each labeled "Avatar N:". Score each one on its \
own (do not compare them to each other) from 1 to 10 for how much this \
creator would want to copy that look:
10 = exactly their taste, 7 = probably, 4 = unlikely, 1 = plain, default or \
clearly not for them. Most random Roblox avatars are plain: score those low.

General guide (use it where nothing below says otherwise). An avatar stands \
out and has personality when it has: a standout hat/headpiece or signature \
accessory; a clear character or theme (costume, job, meme, fantasy, anime - \
famous characters are fine); bold color (saturated colors, rainbow/gradient, \
or a striking monochrome/high-contrast look); or a funny, quirky touch. It \
scores low when it looks plain or default (basic shirt and pants, default \
skin tones and colors, nothing interesting) or is piled with so many \
accessories that the character is hard to read.
{taste}
Return one entry per avatar: its number, the score, and a short reason."""

TASTE_BLOCK = """
THIS CREATOR'S TASTE PROFILE - learned from {liked} avatars they marked \
Perfect and {disliked} they disliked. It overrides the general guide \
wherever they disagree:
{profile}
"""

PROFILE_PROMPT = """A Roblox content creator rated {total} random Roblox avatars to \
teach you their taste in avatars to copy for their videos. Only {pct}% were \
marked PERFECT; the rest were DISLIKED. Below are contact sheets of avatars: \
first some they marked PERFECT, then a random sample of the ones they \
DISLIKED.{reasons}

Write this creator's TASTE PROFILE for another AI that will score new \
avatars from pictures. Plain text, at most 300 words, in these sections:
LOVES: 5-8 concrete, visible traits shared by the PERFECT avatars.
REJECTS: 5-8 concrete, visible traits of the DISLIKED ones. Describe the most \
common rejected looks specifically (for example Roblox's default starter \
avatars, plain shirt-and-pants outfits) so they can be recognised.
TIE-BREAKERS: how to decide close calls between a Perfect and a Disliked look.
CALIBRATION: a Perfect is rare (about {pct}%), so most avatars should score low.

Be specific and visual. Only describe what you can actually see in the \
sheets, do not invent traits, and do not mention usernames."""

VERDICT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "avatar": {"type": "INTEGER"},
            "score": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
        },
        "required": ["avatar", "score", "reason"],
    },
}


class DailyQuotaExceeded(RuntimeError):
    pass


class InvalidApiKey(Exception):
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


def find_groups(session, term):
    """A numeric term is a group ID; anything else searches groups by keyword (best matches first)."""
    if term.isdigit():
        group = roblox(session, "group-info", "GET", ROBLOX_GROUP_URL.format(term)).json()
        return [group]
    data = roblox(session, "group-search", "GET", ROBLOX_GROUP_SEARCH_URL,
                  params={"keyword": term, "limit": 25}).json()
    return data.get("data", [])


def group_member_pages(session, group_id):
    """Yields pages of up to 100 members, newest first (more likely to be active)."""
    cursor = None
    while True:
        params = {"limit": 100, "sortOrder": "Desc"}
        if cursor:
            params["cursor"] = cursor
        data = roblox(session, "group-members", "GET", ROBLOX_GROUP_MEMBERS_URL.format(group_id),
                      params=params).json()
        yield [{"id": m["user"]["userId"], "name": m["user"]["username"],
                "hasVerifiedBadge": m["user"].get("hasVerifiedBadge", False)}
               for m in data.get("data", []) if "user" in m]
        cursor = data.get("nextPageCursor")
        if not cursor:
            return


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
    """Talks to Gemini: scores avatars in batches (quotas count requests, not images) and writes the taste profile."""

    def __init__(self, api_key, profile=None):
        self.headers = {"x-goog-api-key": api_key}  # header, not URL, so errors never print the key
        self.profile = profile  # {"text": ..., "liked": n, "disliked": n} or None
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

    def _ask(self, parts, schema=None, timeout=60):
        """Sends one request (rotating models, pacing, retries) and returns the reply text."""
        config = {"responseMimeType": "application/json", "responseSchema": schema} if schema else {}
        body = {"contents": [{"parts": parts}], "generationConfig": config}
        start = self.turn % max(len(self.models), 1)
        self.turn += 1
        last_error = "no model available"
        for model in self.models[start:] + self.models[:start]:
            for attempt in range(2):
                self._pace(model)
                try:
                    resp = requests.post(GEMINI_URL.format(model), headers=self.headers, json=body, timeout=timeout)
                except requests.RequestException as e:
                    last_error = f"{model}: {type(e).__name__}"
                    continue
                if resp.ok:
                    try:
                        reply = resp.json()["candidates"][0]["content"]["parts"]
                        return "".join(p.get("text", "") for p in reply if not p.get("thought"))
                    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
                        last_error = f"{model}: unreadable reply"
                        continue
                if resp.status_code in (401, 403) or "API_KEY_INVALID" in resp.text:
                    raise InvalidApiKey("Gemini rejected the API key")
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

    def judge(self, images):
        """Returns {index: (score 1-10, reason)}; indexes missing from the reply are left out."""
        taste = ""
        if self.profile:
            taste = (TASTE_BLOCK.replace("{liked}", str(self.profile["liked"]))
                     .replace("{disliked}", str(self.profile["disliked"])).replace("{profile}", self.profile["text"]))
        parts = [{"text": JUDGE_PROMPT.replace("{n}", str(len(images))).replace("{taste}", taste)}]
        for i, img in enumerate(images, 1):
            parts.append({"text": f"Avatar {i}:"})
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}})
        for _ in range(2):
            try:
                verdicts = {}
                for v in json.loads(self._ask(parts, VERDICT_SCHEMA)):
                    if isinstance(v.get("avatar"), int) and 1 <= v["avatar"] <= len(images) and isinstance(v.get("score"), int):
                        verdicts[v["avatar"] - 1] = (v["score"], v.get("reason", ""))
                return verdicts
            except (ValueError, AttributeError, TypeError):
                continue  # the reply wasn't valid JSON: ask once more
        raise RuntimeError("unreadable reply")

    def write_profile(self, liked_images, disliked_images, total_liked, total_disliked, reasons):
        """Asks Gemini to describe the creator's taste from contact sheets of their rated avatars."""
        total = total_liked + total_disliked
        pct = max(1, round(100 * total_liked / total))
        why = ""
        if reasons:
            why = " Reasons they gave for dislikes: " + ", ".join(f"{r} ({n}x)" for r, n in reasons) + "."
        parts = [{"text": PROFILE_PROMPT.replace("{total}", str(total)).replace("{pct}", str(pct)).replace("{reasons}", why)}]
        for label, images in (("PERFECT", liked_images), ("DISLIKED", disliked_images)):
            sheets = [images[i:i + SHEET_SIZE] for i in range(0, len(images), SHEET_SIZE)]
            for n, group in enumerate(sheets, 1):
                parts.append({"text": f"{label} avatars, sheet {n} of {len(sheets)}:"})
                parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(contact_sheet(group)).decode()}})
        text = self._ask(parts, timeout=120).strip()
        if len(text) < 80:
            raise RuntimeError("the profile came back empty")
        return text


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


# --- Ratings ("teach it your taste") ------------------------------------------

def load_feedback():
    try:
        data = json.loads(FEEDBACK_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_feedback(feedback):
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PATH.write_text(json.dumps(feedback, indent=1))


def feedback_stats(feedback=None):
    feedback = load_feedback() if feedback is None else feedback
    liked = sum(1 for v in feedback.values() if v.get("verdict") == "liked")
    disliked = sum(1 for v in feedback.values() if v.get("verdict") == "disliked")
    return {"liked": liked, "disliked": disliked}


def rate_avatar(name, action, reason=None):
    """Records a rating. action: like | dislike | reason | clear.

    like     keeps the avatar and remembers a copy as a "good" example.
    dislike  moves the avatar out of the catalog into feedback/disliked (a "bad" example).
    reason   adds a reason to an existing dislike.
    clear    undoes a like or dislike (a disliked avatar returns to the catalog).
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("bad username")
    avatar = OUTPUT_DIR / f"{name}.png"
    liked_copy = FEEDBACK_DIR / "liked" / f"{name}.png"
    disliked_file = FEEDBACK_DIR / "disliked" / f"{name}.png"
    feedback = load_feedback()
    now = int(time.time())

    if action == "like":
        if not avatar.exists():
            raise ValueError("no such avatar")
        liked_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(avatar, liked_copy)
        feedback[name] = {"verdict": "liked", "time": now}
    elif action == "dislike":
        if avatar.exists():
            disliked_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(avatar), str(disliked_file))
        elif not disliked_file.exists():
            raise ValueError("no such avatar")
        liked_copy.unlink(missing_ok=True)
        entry = {"verdict": "disliked", "time": now}
        if reason in DISLIKE_REASONS:
            entry["reason"] = reason
        feedback[name] = entry
    elif action == "reason":
        if feedback.get(name, {}).get("verdict") != "disliked" or reason not in DISLIKE_REASONS:
            raise ValueError("nothing to tag")
        feedback[name]["reason"] = reason
    elif action == "clear":
        if disliked_file.exists() and not avatar.exists():
            shutil.move(str(disliked_file), str(avatar))
        liked_copy.unlink(missing_ok=True)
        feedback.pop(name, None)
    else:
        raise ValueError("bad action")

    save_feedback(feedback)
    return feedback_stats(feedback)


def rate_candidate(cand, action, reason, session, max_followers):
    """Records a rating for an avatar shown in discovery (it isn't in the catalog yet).

    like     saves it to the catalog (if the account isn't too well-known) and remembers it as a good example.
    dislike  remembers it as a bad example.
    reason   adds a reason to a dislike.
    undo     removes everything a discovery rating created.
    Returns (stats, info).
    """
    name = cand["name"]
    if action == "reason":
        return rate_avatar(name, "reason", reason), {}
    if action == "undo":
        feedback = load_feedback()
        entry = feedback.pop(name, {})
        (FEEDBACK_DIR / "liked" / f"{name}.png").unlink(missing_ok=True)
        (FEEDBACK_DIR / "disliked" / f"{name}.png").unlink(missing_ok=True)
        if entry.get("in_catalog"):
            (OUTPUT_DIR / f"{name}.png").unlink(missing_ok=True)
        save_feedback(feedback)
        return feedback_stats(feedback), {}
    if action not in ("like", "dislike"):
        raise ValueError("bad action")

    verdict = "liked" if action == "like" else "disliked"
    entry = {"verdict": verdict, "time": int(time.time()), "source": "discovery"}
    info = {}
    folder = FEEDBACK_DIR / verdict
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.png").write_bytes(cand["image"])
    if action == "like":
        count = follower_counts(session, [{"id": cand["id"]}]).get(cand["id"])
        info["followers"] = count
        entry["in_catalog"] = info["saved"] = count is not None and count <= max_followers
        if entry["in_catalog"]:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            save_avatar(cand, cand["image"])
    elif reason in DISLIKE_REASONS:
        entry["reason"] = reason
    feedback = load_feedback()
    feedback[name] = entry
    save_feedback(feedback)
    seen = load_seen()
    seen.add(cand["id"])
    save_seen(seen)
    return feedback_stats(feedback), info


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
                    first = next(pages)  # groups with hidden member lists fail here
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
                    continue  # this group is used up or failed: drop it
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
        """Hands out up to n avatars. Returns (names, done) - done means nothing more is coming."""
        with self.lock:
            items = [self.ready.popleft() for _ in range(min(n, len(self.ready)))]
            for item in items:
                self.shown[item["name"]] = item
            return [i["name"] for i in items], (self.finished and not self.ready)


def dislike_reason_counts():
    counts = Counter(v.get("reason") for v in load_feedback().values()
                     if v.get("verdict") == "disliked" and v.get("reason"))
    return counts.most_common()


def contact_sheet(images, cols=6, cell=190):
    """Many avatars in one picture, so the AI can look at lots of them cheaply."""
    from PIL import Image  # Pillow: only needed for this and the look-alike filter
    rows = -(-len(images) // cols)
    sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
    for i, data in enumerate(images):
        img = Image.open(io.BytesIO(data)).convert("RGBA").resize((cell, cell), Image.LANCZOS)
        sheet.paste(img, ((i % cols) * cell, (i // cols) * cell), img)
    out = io.BytesIO()
    sheet.save(out, "PNG")
    return out.getvalue()


LOOKALIKE_DISTANCE = 4  # how similar (out of 144 bits) counts as "the same look"; tested to lose ~2% of Perfects
LOOKALIKE_MIN_DISLIKES = 20  # don't trust the filter until there are this many dislikes
LOOKS_CACHE_PATH = CATALOG_DIR / "looks.json"


def look_hash(png):
    """A tiny fingerprint of how an avatar looks; similar looks get similar numbers. None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    size = 12
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    flat = Image.new("RGBA", img.size, "white")
    flat.alpha_composite(img)
    gray = flat.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
    return bits


class KnownLooks:
    """Skips avatars that look almost identical to ones you disliked (mostly Roblox's default starter avatars)."""

    def __init__(self):
        self.disliked, self.liked = [], []
        try:
            cache = json.loads(LOOKS_CACHE_PATH.read_text())
        except (OSError, ValueError):
            cache = {}
        fresh, feedback = {}, load_feedback()
        for name, entry in feedback.items():
            verdict = entry.get("verdict")
            path = FEEDBACK_DIR / verdict / f"{name}.png" if verdict in ("liked", "disliked") else None
            if path is None or not path.is_file():
                continue
            key = f"{verdict}/{name}"
            value = cache.get(key)
            if value is None:
                value = look_hash(path.read_bytes())
            if value is None:  # Pillow isn't installed
                self.disliked = self.liked = []
                return
            fresh[key] = value
            (self.disliked if verdict == "disliked" else self.liked).append(value)
        if fresh != cache:
            try:
                LOOKS_CACHE_PATH.write_text(json.dumps(fresh))
            except OSError:
                pass
        if len(self.disliked) < LOOKALIKE_MIN_DISLIKES:
            self.disliked = []

    def is_junk(self, png):
        if not self.disliked:
            return False
        h = look_hash(png)
        if h is None:
            return False
        near = min(bin(h ^ d).count("1") for d in self.disliked)
        if near > LOOKALIKE_DISTANCE:
            return False
        return min([bin(h ^ l).count("1") for l in self.liked] + [99]) > near  # closer to a Perfect? keep it


def load_profile():
    """The saved taste profile as {'text', 'liked', 'disliked'}, or None."""
    try:
        text = PROFILE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        meta = json.loads(PROFILE_META_PATH.read_text())
    except (OSError, ValueError):
        meta = {}
    return {"text": text, "liked": int(meta.get("liked", 0)), "disliked": int(meta.get("disliked", 0))}


def profile_status():
    """(enough ratings to make a profile, the profile is missing or out of date)."""
    stats = feedback_stats()
    enough = min(stats["liked"], stats["disliked"]) >= PROFILE_MIN_EACH
    profile = load_profile()
    made_from = (profile["liked"] + profile["disliked"]) if profile else 0
    stale = profile is None or (stats["liked"] + stats["disliked"]) - made_from >= PROFILE_REFRESH_AFTER
    return enough, stale


def update_taste_profile(judge):
    """Has Gemini study your ratings and writes catalog/taste_profile.txt. Returns the profile."""
    feedback = load_feedback()
    stats = feedback_stats(feedback)
    if min(stats["liked"], stats["disliked"]) < PROFILE_MIN_EACH:
        raise ValueError(f"Rate at least {PROFILE_MIN_EACH} avatars Perfect and {PROFILE_MIN_EACH} Disliked first "
                         f"(you have {stats['liked']} and {stats['disliked']}).")

    def sample(verdict, limit):
        names = [n for n, v in feedback.items() if v.get("verdict") == verdict
                 and (FEEDBACK_DIR / verdict / f"{n}.png").is_file()]
        random.shuffle(names)
        return [(FEEDBACK_DIR / verdict / f"{n}.png").read_bytes() for n in names[:limit]]

    text = judge.write_profile(sample("liked", PROFILE_MAX_LIKED), sample("disliked", PROFILE_MAX_DISLIKED),
                               stats["liked"], stats["disliked"], dislike_reason_counts())
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    if PROFILE_PATH.exists():
        shutil.copyfile(PROFILE_PATH, PROFILE_PATH.with_name("taste_profile.previous.txt"))
    PROFILE_PATH.write_text(text + "\n", encoding="utf-8")
    PROFILE_META_PATH.write_text(json.dumps({**stats, "time": int(time.time())}))
    return {"text": text, **stats}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_PATH.read_text())
        settings.update({k: int(v) for k, v in saved.items() if k in settings})
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return settings


def save_settings(settings):
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def get_api_key():
    """GEMINI_API_KEY env var first, then the key saved from the menu."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_PATH.read_text().strip() or None
    except OSError:
        return None


def api_key_works(key):
    try:
        resp = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELS[0]}",
                            headers={"x-goog-api-key": key}, timeout=20)
        return resp.ok
    except requests.RequestException:
        return False


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
        rate_avatar(user["name"], "like")  # you picked it yourself, so it teaches the AI what you like
        seen.add(user["id"])
        saved += 1
        print(f"  + {user['name']}")
    save_seen(seen)
    print(f"\nDone. Saved {saved} avatar(s) to {OUTPUT_DIR}/")


class AutoRun:
    """Pipeline shared by --keywords and --groups.

    Gemini judges first and followers are only checked for avatars that pass:
    the follower API (100/min) is the tightest limit left, so it isn't spent
    on avatars that would be rejected anyway.
    """

    def __init__(self, args, session, seen, judge):
        self.args, self.session, self.seen, self.judge = args, session, seen, judge
        self.stats = Counter()
        self.handled = set()
        self.looks = KnownLooks()

    def process(self, users, limit=None):
        """Judges and saves up to `limit` new candidates; returns how many were new."""
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
            if self.looks.is_junk(images[user["id"]]):
                self.seen.add(user["id"])
                self.stats["look like avatars you disliked"] += 1
            else:
                kept.append(user)
        ready = kept

        size = self.args.batch_size
        for i in range(0, len(ready), size):
            self._judge_batch(ready[i:i + size], images)
            save_seen(self.seen)
        return len(fresh)

    def _judge_batch(self, batch, images):
        try:
            verdicts = self.judge.judge([images[u["id"]] for u in batch])
        except DailyQuotaExceeded:
            raise
        except RuntimeError as e:
            print(f"  batch of {len(batch)} not judged ({e}), retried next run")
            self.stats["not judged (retried next run)"] += len(batch)
            return

        passed = []
        for idx, user in enumerate(batch):
            verdict = verdicts.get(idx)
            if verdict is None:
                self.stats["not judged (retried next run)"] += 1
            elif verdict[0] >= self.args.min_score:
                passed.append((user, verdict[1], verdict[0]))
            else:
                self.seen.add(user["id"])
                self.stats["judged, not a match"] += 1
        if not passed:
            return

        counts = follower_counts(self.session, [user for user, _, _ in passed])
        for user, reason, score in passed:
            count = counts.get(user["id"])
            if count is None:
                self.stats["follower lookup failed (retried next run)"] += 1
            elif count > self.args.max_followers:
                self.seen.add(user["id"])
                self.stats[f"matched but over {self.args.max_followers} followers"] += 1
            else:
                save_avatar(user, images[user["id"]])
                self.seen.add(user["id"])
                self.stats["saved"] += 1
                print(f"  + {user['name']} [{score}/10] - {reason}")


def search_keywords(run):
    keywords = list(run.args.keywords)
    if run.args.shuffle:
        random.shuffle(keywords)
    for kw in keywords:
        # Roblox's 1/min search window refills while this keyword's candidates
        # are being judged, so the next search usually doesn't have to wait.
        print(f"Searching '{kw}'...")
        try:
            users = search_users(run.session, kw, run.args.per_keyword)
        except requests.RequestException as e:
            print(f"  search failed: {e}")
            continue
        run.process(users)


def search_groups(run):
    done = set()
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
            print(f"Group '{group['name']}' ({group.get('memberCount', 0):,} members)...")
            new, readable = 0, False
            try:
                # Already-checked members are skipped, so reruns page deeper into the group.
                for page_no, page in enumerate(group_member_pages(run.session, group["id"]), 1):
                    readable = True
                    new += run.process(page, limit=run.args.per_group - new)
                    if new >= run.args.per_group or page_no >= MAX_GROUP_PAGES:
                        break
            except requests.RequestException as e:
                if not readable:
                    print("  member list is hidden without logging in, trying the next group")
                    continue
                print(f"  member list failed partway: {e}")
            used += 1


def run_auto(args, session, seen, api_key):
    judge = GeminiJudge(api_key, load_profile())
    enough, stale = profile_status()
    if enough and stale:
        print("Updating your taste profile from your ratings...")
        try:
            judge.profile = update_taste_profile(judge)
        except (RuntimeError, ImportError, OSError) as e:
            print(f"  couldn't update it ({e}); continuing with the old one")
    if judge.profile:
        print(f"Using your taste profile (learned from {judge.profile['liked']} Perfect + "
              f"{judge.profile['disliked']} Disliked ratings). See catalog/taste_profile.txt\n")
    elif not enough:
        stats = feedback_stats()
        print(f"Tip: rate more avatars (menu option 5) to teach it your taste - it needs {PROFILE_MIN_EACH} Perfect "
              f"and {PROFILE_MIN_EACH} Disliked ({stats['liked']} and {stats['disliked']} so far).\n")
    run = AutoRun(args, session, seen, judge)
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

    print(f"\nDone. Saved {run.stats['saved']} new avatar(s) to {OUTPUT_DIR}/")
    for label, count in run.stats.items():
        if label != "saved":
            print(f"  {label}: {count}")


# --- Gallery / review page ---------------------------------------------------

GALLERY_PATH = CATALOG_DIR / "gallery.html"

GALLERY_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--ink:#1b1f2a;--muted:#5b6272;--line:#d9dce6;--accent:#4f46e5;--stage:#eef0f6;--good:#1f8f4e;--bad:#c2372f;--size:200px}
@media (prefers-color-scheme:dark){:root{--bg:#12141b;--card:#1c2030;--ink:#eceef6;--muted:#9aa1b5;--line:#2c3247;--accent:#8b85ff;--stage:#e9ebf3;--good:#4cc07f;--bad:#ff7a70}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;gap:10px 20px;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}
.count,.stats{color:var(--muted);font-size:14px}
.tools{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-left:auto}
input[type=search],select{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
input[type=search]{width:170px}
label{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:14px}
.hint{padding:10px 20px;color:var(--muted);font-size:14px;border-bottom:1px solid var(--line)}
main{padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--size),1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card.liked{border-color:var(--good);box-shadow:0 0 0 1px var(--good)}
.copy{display:block;width:100%;border:0;background:none;padding:0;text-align:left;color:inherit;font:inherit;cursor:pointer}
.copy:hover .name,.copy:focus-visible .name{color:var(--accent)}
.stage{background:var(--stage);aspect-ratio:1/1}
.stage img{width:100%;height:100%;object-fit:contain;display:block}
.name{padding:9px 11px 6px;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.actions{display:flex;gap:6px;padding:0 8px 8px}
.act{flex:1;font:inherit;font-size:13px;padding:6px 4px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);cursor:pointer}
.act.love:hover,.card.liked .act.love{background:var(--good);border-color:var(--good);color:#fff}
.act.dis:hover{background:var(--bad);border-color:var(--bad);color:#fff}
.empty{color:var(--muted);text-align:center;padding:60px 20px}
#bar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);max-width:calc(100vw - 24px);background:var(--ink);color:var(--bg);padding:10px 14px;border-radius:14px;display:none;gap:8px 10px;align-items:center;flex-wrap:wrap;justify-content:center;font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.25)}
#bar.show{display:flex}
#bar button{font:inherit;font-size:13px;padding:4px 10px;border-radius:999px;border:1px solid currentColor;background:transparent;color:inherit;cursor:pointer}
#bar button:hover{background:var(--bg);color:var(--ink)}
</style></head><body>
<header>
<h1>Skin Searcher</h1><span class="count" id="count"></span><span class="stats" id="stats"></span>
<a id="nav" href="#" hidden style="color:var(--accent);text-decoration:none;font-size:14px">Discover new avatars</a>
<div class="tools">
<input type="search" id="q" placeholder="Search usernames" aria-label="Search usernames">
<select id="show" aria-label="Show"><option value="all">Show all</option><option value="new">Not rated yet</option><option value="liked">Perfect only</option></select>
<select id="sort" aria-label="Sort"><option value="new">Newest first</option><option value="az">A to Z</option></select>
<label>Size <input type="range" id="size" min="110" max="420" value="200"></label>
</div></header>
<div class="hint" id="hint"></div>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden></div></main>
<div id="bar" role="status"></div>
<script>
const REVIEW=__REVIEW__, REASONS=__REASONS__, DISCOVER=__DISCOVER__;
let AVATARS=__DATA__, STATS=__STATS__;
const TOKEN=new URLSearchParams(location.search).get("t")||"";
const $=id=>document.getElementById(id), grid=$("grid"), bar=$("bar");
let barTimer;
function say(text,buttons,ms){
  bar.textContent="";const t=document.createElement("span");t.textContent=text;bar.appendChild(t);
  for(const [label,fn] of buttons||[]){const b=document.createElement("button");b.textContent=label;b.onclick=fn;bar.appendChild(b)}
  bar.classList.add("show");clearTimeout(barTimer);barTimer=setTimeout(()=>bar.classList.remove("show"),ms||1800)}
function copy(name){
  const done=()=>say("Copied "+name);
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(name).then(done,()=>fallback(name,done))}else fallback(name,done)}
function fallback(name,done){const t=document.createElement("textarea");t.value=name;t.style.position="fixed";t.style.opacity="0";document.body.appendChild(t);t.select();
  try{document.execCommand("copy");done()}catch(e){say("Couldn't copy - select it by hand: "+name)}t.remove()}
async function rate(name,action,reason){
  const r=await fetch("/api/rate",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify({name,action,reason})});
  if(!r.ok)throw new Error(await r.text());
  STATS=(await r.json()).stats;showStats()}
function showStats(){
  const total=STATS.liked+STATS.disliked;
  $("stats").textContent=total?("Rated "+total+": "+STATS.liked+" perfect, "+STATS.disliked+" disliked ("+Math.round(100*STATS.liked/total)+"% kept)"):""}
async function love(a){
  try{await rate(a.name,a.liked?"clear":"like");a.liked=!a.liked;render();say(a.liked?a.name+" marked perfect - the AI will learn from it":"Rating removed")}
  catch(e){say("Couldn't save - is the Skin Searcher window still open?",[],4000)}}
async function dislike(a){
  try{await rate(a.name,"dislike")}catch(e){say("Couldn't save - is the Skin Searcher window still open?",[],4000);return}
  AVATARS=AVATARS.filter(x=>x!==a);render();
  const buttons=[["Undo",async()=>{try{await rate(a.name,"clear");a.liked=false;AVATARS.push(a);render();say("Restored "+a.name)}catch(e){say("Couldn't undo",[],3000)}}]];
  for(const reason of REASONS)buttons.push([reason,async()=>{try{await rate(a.name,"reason",reason);say("Thanks - noted: "+reason)}catch(e){say("Couldn't save the reason",[],3000)}}]);
  say("Removed "+a.name+". Why?",buttons,9000)}
function render(){
  const q=$("q").value.trim().toLowerCase(),sort=$("sort").value,show=$("show").value;
  const list=AVATARS.filter(a=>a.name.toLowerCase().includes(q)&&(show==="all"||(show==="liked"?a.liked:!a.liked)));
  list.sort(sort==="az"?(a,b)=>a.name.toLowerCase().localeCompare(b.name.toLowerCase()):(a,b)=>b.time-a.time);
  grid.textContent="";
  for(const a of list){
    const c=document.createElement("div");c.className="card"+(a.liked?" liked":"");
    const b=document.createElement("button");b.className="copy";b.title="Click to copy the username";
    const s=document.createElement("div");s.className="stage";
    const i=document.createElement("img");i.loading="lazy";i.alt=a.name;i.src=a.file;s.appendChild(i);
    const n=document.createElement("div");n.className="name";n.textContent=a.name;
    b.append(s,n);b.addEventListener("click",()=>copy(a.name));c.appendChild(b);
    if(REVIEW){
      const r=document.createElement("div");r.className="actions";
      const l=document.createElement("button");l.className="act love";l.textContent=a.liked?"Perfect \\u2713":"Perfect";l.title="Teach the AI you want more like this";l.onclick=()=>love(a);
      const d=document.createElement("button");d.className="act dis";d.textContent="Dislike";d.title="Remove it and teach the AI to avoid this look";d.onclick=()=>dislike(a);
      r.append(l,d);c.appendChild(r)}
    grid.appendChild(c)}
  $("count").textContent=list.length===AVATARS.length?AVATARS.length+" avatars":list.length+" of "+AVATARS.length+" avatars";
  $("empty").hidden=list.length>0;
  $("empty").textContent=AVATARS.length?"Nothing matches.":"No avatars yet. Run a search from the menu, then open this page again."}
$("nav").hidden=!DISCOVER;$("nav").href="/discover?t="+encodeURIComponent(TOKEN);
$("hint").hidden=!REVIEW;
$("hint").textContent="Click an avatar to copy its username. Mark the ones you love as Perfect and remove the bad ones with Dislike - the AI uses your ratings to pick better next time.";
for(const id of ["q","sort","show"])$(id).addEventListener(id==="q"?"input":"change",render);
$("size").addEventListener("input",e=>document.documentElement.style.setProperty("--size",e.target.value+"px"));
showStats();render();
</script></body></html>
"""


DISCOVER_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher - Discover</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--ink:#1b1f2a;--muted:#5b6272;--line:#d9dce6;--accent:#4f46e5;--stage:#eef0f6;--good:#1f8f4e;--bad:#c2372f}
@media (prefers-color-scheme:dark){:root{--bg:#12141b;--card:#1c2030;--ink:#eceef6;--muted:#9aa1b5;--line:#2c3247;--accent:#8b85ff;--stage:#e9ebf3;--good:#4cc07f;--bad:#ff7a70}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px 20px;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}.muted{color:var(--muted);font-size:14px}
header a{margin-left:auto;color:var(--accent);text-decoration:none;font-size:14px}
main{max-width:520px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.stage{background:var(--stage);aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;color:#5b6272}
.stage img{width:100%;height:100%;object-fit:contain;display:block}
.name{padding:12px 14px;font-weight:600;font-size:17px;text-align:center}
.row{display:flex;gap:10px;margin-top:14px}
.btn{flex:1;font:inherit;padding:12px 8px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);cursor:pointer;font-size:15px}
.btn small{display:block;color:var(--muted);font-size:12px}
.btn.dis:hover{background:var(--bad);border-color:var(--bad);color:#fff}.btn.love:hover{background:var(--good);border-color:var(--good);color:#fff}
.btn:hover small{color:inherit}.btn.skip{flex:.6}
.hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px}
#bar{margin-top:14px;min-height:34px;text-align:center;font-size:14px}
#bar button{font:inherit;font-size:13px;margin:2px 3px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
</style></head><body>
<header><h1>Discover</h1><span class="muted" id="stats"></span><a id="nav" href="#">Review saved avatars</a></header>
<main>
<div class="card"><div class="stage" id="stage">Finding avatars...</div><div class="name" id="name">&nbsp;</div></div>
<div class="row">
<button class="btn dis" id="dis">Dislike<small>Left arrow</small></button>
<button class="btn skip" id="skip">Skip<small>Down arrow</small></button>
<button class="btn love" id="love">Perfect<small>Right arrow</small></button>
</div>
<div id="bar" role="status"></div>
<div class="hint">Keys: left = dislike, right = perfect, down = skip, Z = undo, 1-3 = why you disliked it.<br>Perfect ones are saved to your catalog. Everything you rate teaches the AI.</div>
</main>
<script>
const REASONS=__REASONS__;let STATS=__STATS__;
const TOKEN=new URLSearchParams(location.search).get("t")||"";
const $=id=>document.getElementById(id);
let queue=[],current=null,last=null,exhausted=false,fetching=false,busy=false,me={liked:0,disliked:0},barTimer;
$("nav").href="/?t="+encodeURIComponent(TOKEN);
async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw new Error(await r.text());return r.json()}
function bar(text,buttons,ms){
  const b=$("bar");b.textContent=text+" ";
  for(const [label,fn] of buttons||[]){const x=document.createElement("button");x.textContent=label;x.onclick=fn;b.appendChild(x)}
  clearTimeout(barTimer);if(ms)barTimer=setTimeout(()=>b.textContent="",ms)}
function showStats(){
  const t=STATS.liked+STATS.disliked;
  $("stats").textContent="This session: "+(me.liked+me.disliked)+" rated"+(t?" | All time: "+STATS.liked+" perfect, "+STATS.disliked+" disliked ("+Math.round(100*STATS.liked/t)+"% kept)":"")}
async function refill(){
  if(fetching||exhausted||queue.length>=6)return;fetching=true;let got=0;
  try{const r=await api("/api/next?n=6");queue.push(...r.names);got=r.names.length;exhausted=r.done;if(!current)advance()}
  catch(e){bar("Lost connection - is the Skin Searcher window still open?")}
  fetching=false;if(!exhausted&&queue.length<6)setTimeout(refill,got?150:1200)}
function advance(){
  current=queue.shift()||null;render();
  for(const n of queue.slice(0,3)){const i=new Image();i.src="/candidate/"+n+".png"}
  if(queue.length<4)refill()}
function render(){
  const stage=$("stage");stage.textContent="";
  if(current){const i=document.createElement("img");i.src="/candidate/"+current+".png";i.alt=current;stage.appendChild(i);$("name").textContent=current}
  else{stage.textContent=exhausted?"No more new avatars from these groups.":"Finding avatars...";$("name").innerHTML="&nbsp;"}
  showStats()}
async function rate(action){
  if(!current||busy)return;busy=true;const name=current;
  try{
    const r=await api("/api/discover-rate",{name,action});STATS=r.stats;last={name,action};
    if(action==="like"){me.liked++;bar(r.info.saved?"Saved "+name+" to your catalog.":"Noted - but "+name+" has "+(r.info.followers==null?"an unknown number of":r.info.followers.toLocaleString())+" followers, so it wasn't saved.",[],3500)}
    else{me.disliked++;bar("Why?",REASONS.map((x,i)=>[(i+1)+" "+x,()=>tag(i)]),6000)}
    advance()}
  catch(e){bar("Couldn't save - is the Skin Searcher window still open?")}
  busy=false}
async function tag(i){
  if(!last||last.action!=="dislike")return;
  try{await api("/api/discover-rate",{name:last.name,action:"reason",reason:REASONS[i]});bar("Noted: "+REASONS[i],[],1800)}catch(e){}}
function skip(){if(current){last=null;advance()}}
async function undo(){
  if(!last||busy)return;busy=true;const l=last;
  try{const r=await api("/api/discover-rate",{name:l.name,action:"undo"});STATS=r.stats;
    if(l.action==="like")me.liked--;else me.disliked--;
    if(current)queue.unshift(current);current=l.name;last=null;render();bar("Undone.",[],1200)}
  catch(e){bar("Couldn't undo")}
  busy=false}
$("dis").onclick=()=>rate("dislike");$("love").onclick=()=>rate("like");$("skip").onclick=skip;
document.addEventListener("keydown",e=>{
  if(e.metaKey||e.ctrlKey||e.altKey)return;const k=e.key.toLowerCase();
  if(k==="arrowleft"||k==="a")rate("dislike");else if(k==="arrowright"||k==="d")rate("like");
  else if(k==="arrowdown"||k==="s"||k===" "){e.preventDefault();skip()}
  else if(k==="z"||k==="backspace")undo();else if(k>="1"&&k<="3")tag(Number(k)-1)});
showStats();refill();
</script></body></html>
"""


def gallery_html(review, prefix="", discover=False):
    """The page's HTML. `review` adds the Perfect/Dislike buttons (needs the review server)."""
    feedback = load_feedback()
    avatars = [{"name": path.stem, "file": prefix + "avatars/" + quote(path.name), "time": int(path.stat().st_mtime),
                "liked": feedback.get(path.stem, {}).get("verdict") == "liked"}
               for path in OUTPUT_DIR.glob("*.png")]

    def js(value):
        return json.dumps(value).replace("</", "<\\/")

    return (GALLERY_TEMPLATE.replace("__REASONS__", js(DISLIKE_REASONS))
            .replace("__STATS__", js(feedback_stats(feedback))).replace("__REVIEW__", "true" if review else "false")
            .replace("__DISCOVER__", "true" if discover else "false").replace("__DATA__", js(avatars)))


def build_gallery():
    """Writes a static, view-only catalog/gallery.html; returns how many avatars there are."""
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    html = gallery_html(review=False)
    GALLERY_PATH.write_text(html, encoding="utf-8")
    return len(list(OUTPUT_DIR.glob("*.png")))


def discover_html():
    return (DISCOVER_TEMPLATE.replace("__REASONS__", json.dumps(DISLIKE_REASONS))
            .replace("__STATS__", json.dumps(feedback_stats())))


def make_review_server(feed=None, session=None, max_followers=DEFAULT_SETTINGS["max_followers"]):
    """A small web server on this computer only, so the pages' buttons can save your ratings."""
    token = secrets.token_urlsafe(16)
    holder = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self):
            port = holder["server"].server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden")
            path, _, query = self.path.partition("?")
            if path in ("/", "/discover"):
                if f"t={token}" not in query.split("&"):
                    return self._send(403, b"open this page from the Skin Searcher menu")
                if path == "/discover":
                    if feed is None:
                        return self._send(404, b"not found")
                    return self._send(200, discover_html().encode("utf-8"), "text/html; charset=utf-8")
                return self._send(200, gallery_html(review=True, discover=feed is not None).encode("utf-8"),
                                  "text/html; charset=utf-8")
            if path == "/api/next":
                if feed is None or self.headers.get("X-Token") != token:
                    return self._send(403, b"forbidden")
                match = re.search(r"(?:^|&)n=(\d{1,2})(?:&|$)", query)
                names, done = feed.take(int(match.group(1)) if match else 6)
                return self._send(200, json.dumps({"names": names, "done": done}).encode(), "application/json")
            match = re.fullmatch(r"/candidate/([A-Za-z0-9_]{1,40})\.png", path)
            if match and feed is not None and match.group(1) in feed.shown:
                return self._send(200, feed.shown[match.group(1)]["image"], "image/png")
            if path == "/favicon.ico":
                return self._send(204)
            match = re.fullmatch(r"/avatars/([A-Za-z0-9_]{1,40})\.png", path)
            if match and (OUTPUT_DIR / f"{match.group(1)}.png").is_file():
                return self._send(200, (OUTPUT_DIR / f"{match.group(1)}.png").read_bytes(), "image/png")
            self._send(404, b"not found")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Token") != token:
                return self._send(403, b"forbidden")
            if self.path not in ("/api/rate", "/api/discover-rate") or \
                    "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(404, b"not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length < 4096:
                    raise ValueError("bad size")
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/rate":
                    stats, info = rate_avatar(data.get("name"), data.get("action"), data.get("reason")), {}
                else:
                    cand = feed.shown.get(data.get("name")) if feed is not None else None
                    if cand is None:
                        raise ValueError("unknown avatar")
                    stats, info = rate_candidate(cand, data.get("action"), data.get("reason"), session, max_followers)
            except (ValueError, TypeError, AttributeError) as e:
                return self._send(400, str(e).encode())
            self._send(200, json.dumps({"ok": True, "stats": stats, "info": info}).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder["server"] = server
    return server, token


def open_path(path):
    """Opens a file or folder with the computer's default app."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def show_gallery(start="/", feed=None, session=None, max_followers=DEFAULT_SETTINGS["max_followers"]):
    """Opens the review (or discovery) page in your browser and keeps it working until you press Enter."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server, token = make_review_server(feed, session, max_followers)
    except OSError:
        count = build_gallery()
        print(f"Gallery updated: {count} avatars (view only - couldn't start the review page).")
        if not open_path(GALLERY_PATH):
            print(f"Open this file in your browser:\n  {GALLERY_PATH}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}{start}?t={token}"
    if feed is not None:
        feed.start()
    print("Opening the page in your browser...")
    if not webbrowser.open(url):
        print(f"Couldn't open the browser automatically. Open this address yourself:\n  {url}")
    if start == "/discover":
        print("\nRate avatars with the arrow keys: left = dislike, right = perfect, down = skip, Z = undo.")
        print("Perfect ones are saved to your catalog, and every rating teaches the AI.")
    else:
        print("\nOn the page: click an avatar to copy its username, mark great ones Perfect,")
        print("and Dislike the bad ones so the AI learns your taste.")
    try:
        input("\nPress Enter here when you're finished (this closes the page's connection)... ")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if feed is not None:
            feed.stop.set()
        server.shutdown()
        server.server_close()


def show_discovery(terms, session, max_followers):
    """Discovery: rate fresh, unfiltered avatars from groups to teach the AI faster."""
    print("Loading fresh avatars from: " + ", ".join(terms))
    show_gallery("/discover", CandidateFeed(session, terms), session, max_followers)


# --- Menu -------------------------------------------------------------------

SETTING_LABELS = {
    "max_followers": "Skip accounts with more followers than",
    "per_group": "New people to check per group",
    "groups_per_term": "Groups to use per group search word",
    "per_keyword": "People to pull per keyword",
    "batch_size": "Avatars per Gemini request",
    "min_score": "Minimum AI match score to save (1-10)",
}


def ask_words(prompt, example):
    while True:
        words = input(f"{prompt}\n  (e.g. {example}): ").replace(",", " ").split()
        if words:
            return words
        print("  Type at least one, or press Ctrl+C to go back.")


def ask_number(prompt, default):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and int(answer) > 0:
            return int(answer)
        print("  Please type a whole number above 0, or press Enter to keep it.")


def make_args(settings, **overrides):
    values = {"groups": None, "keywords": None, "usernames": None, "shuffle": False, **settings, **overrides}
    return argparse.Namespace(**values)


def set_api_key():
    print("\nYou need a free Gemini API key: https://aistudio.google.com/apikey")
    key = getpass.getpass("Paste it here (it won't show on screen), then press Enter: ").strip()
    if not key:
        print("  No key entered.")
        return None
    print("  Checking the key...")
    if not api_key_works(key):
        print("  That key didn't work. Check you copied all of it and try again.")
        return None
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(key)
    print(f"  Key saved on this computer ({KEY_PATH.name}). It's never uploaded to GitHub.")
    return key


def require_api_key():
    return get_api_key() or set_api_key()


def open_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    if not open_path(path):
        print(f"Couldn't open it automatically. Your avatars are in:\n  {path}")


def menu_settings(settings):
    while True:
        print("\nSettings")
        keys = list(SETTING_LABELS)
        for i, key in enumerate(keys, 1):
            print(f"  {i}) {SETTING_LABELS[key]}: {settings[key]}")
        print("  0) Back")
        choice = input("Which one to change? ").strip()
        if choice in ("", "0"):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(keys):
            print("  Pick a number from the list.")
            continue
        key = keys[int(choice) - 1]
        settings[key] = ask_number(SETTING_LABELS[key], settings[key])
        save_settings(settings)
        print("  Saved.")


def menu_taste_profile(api_key):
    stats = feedback_stats()
    enough, stale = profile_status()
    profile = load_profile()
    print(f"\nYour ratings: {stats['liked']} Perfect, {stats['disliked']} Disliked.")
    if not enough:
        print(f"Rate at least {PROFILE_MIN_EACH} Perfect and {PROFILE_MIN_EACH} Disliked avatars first "
              f"(menu option 5 is the fastest way), then come back.")
        return
    if profile and not stale:
        print("Your taste profile is up to date. It is used automatically on every search.")
        if input("Rebuild it anyway? [y/N]: ").strip().lower() != "y":
            print("\n" + profile["text"])
            return
    print("The AI is studying your ratings...")
    try:
        profile = update_taste_profile(GeminiJudge(api_key))
    except ValueError as e:
        print(e)
        return
    except ImportError:
        print("This needs the Pillow package: run   python -m pip install pillow   and try again.")
        return
    except RuntimeError as e:
        print(f"Couldn't update it right now ({e}). Try again in a minute.")
        return
    print(f"\nDone. Your taste profile (saved in {PROFILE_PATH.name}, you can edit it):\n\n{profile['text']}")


def menu_action(choice, session, settings):
    seen = load_seen()
    if choice == "1":
        key = require_api_key()
        if key:
            words = ask_words("Group search words or group IDs", "outfits fashion drip")
            per_group = ask_number("New people to check per group", settings["per_group"])
            print("\nWorking... (Ctrl+C stops early and keeps what's found)\n")
            run_auto(make_args(settings, groups=words, per_group=per_group), session, seen, key)
    elif choice == "2":
        key = require_api_key()
        if key:
            print("Note: Roblox allows only 1 user search per minute, so this is slower than groups.")
            words = ask_words("Search words", "boy yellow red neon")
            print("\nWorking... (Ctrl+C stops early and keeps what's found)\n")
            run_auto(make_args(settings, keywords=words), session, seen, key)
    elif choice == "3":
        names = ask_words("Usernames to save", "Cone_phonky @Shark_Vibess")
        print()
        run_usernames(make_args(settings, usernames=names), session, seen)
    elif choice == "4":
        show_gallery(feed=None, session=session, max_followers=settings["max_followers"])
        return False
    elif choice == "5":
        print("Discovery shows fresh avatars from Roblox groups for you to rate quickly.")
        terms = input(f"Group search words or group IDs [{' '.join(DISCOVER_DEFAULT_TERMS)}]: ").replace(",", " ").split()
        show_discovery(terms or DISCOVER_DEFAULT_TERMS, session, settings["max_followers"])
        return False
    elif choice == "6":
        key = require_api_key()
        if key:
            menu_taste_profile(key)
    elif choice == "7":
        open_folder(OUTPUT_DIR)
        return False
    elif choice == "8":
        menu_settings(settings)
        return False
    elif choice == "9":
        set_api_key()
    else:
        print("Pick a number from the menu.")
        return False
    return True


def menu():
    session = make_session()
    settings = load_settings()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        saved = len(list(OUTPUT_DIR.glob("*.png")))
        print("\n==============================")
        print("        SKIN SEARCHER")
        print("==============================")
        print(f"  {saved} avatars saved | {len(load_seen())} people checked")
        rated = feedback_stats()
        if rated["liked"] or rated["disliked"]:
            total = rated["liked"] + rated["disliked"]
            print(f"  Your ratings: {rated['liked']} perfect, {rated['disliked']} disliked "
                  f"({round(100 * rated['liked'] / total)}% kept)")
        enough, stale = profile_status()
        if load_profile():
            print("  Taste profile: " + ("needs an update (option 6)" if stale else "up to date"))
        elif enough:
            print("  Taste profile: not made yet - choose 6")
        print()
        print("  1) Find avatars in groups   (best)")
        print("  2) Find avatars by keyword  (slower)")
        print("  3) Save specific usernames")
        print("  4) Review avatars in a grid  (teach it your taste)")
        print("  5) Discover new avatars      (rate them fast)")
        print("  6) Update my taste profile   (the AI studies your ratings)")
        print("  7) Open the avatars folder")
        print("  8) Settings")
        print("  9) Change Gemini API key")
        print("  0) Quit")
        try:
            choice = input("\nChoose: ").strip()
            if choice in ("0", "q", "quit", "exit"):
                return
            if menu_action(choice, session, settings):
                input("\nPress Enter to go back to the menu...")
        except KeyboardInterrupt:
            print("\nBack to the menu.")
        except InvalidApiKey:
            print("\nGemini rejected your API key. Choose 9 to enter a new one.")
        except EOFError:
            return


def main():
    if len(sys.argv) == 1:
        menu()
        return

    settings = load_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--groups", nargs="+",
                      help="group search terms or group IDs, e.g. outfits fashion 5215595 (auto-judged by Gemini)")
    mode.add_argument("--keywords", nargs="+", help="user search terms, e.g. boy yellow red (auto-judged by Gemini)")
    mode.add_argument("--usernames", nargs="+", help="usernames you picked yourself (saved directly, no Gemini)")
    mode.add_argument("--gallery", action="store_true", help="review saved avatars in a grid: copy names, mark Perfect or Dislike")
    mode.add_argument("--discover", nargs="*", metavar="WORD",
                      help="rate fresh avatars from groups with the arrow keys (default words: outfits fashion)")
    parser.add_argument("--per-group", type=int, default=settings["per_group"],
                        help="new members to judge per group (default %(default)s)")
    parser.add_argument("--groups-per-term", type=int, default=settings["groups_per_term"],
                        help="top matching groups to use per search term (default %(default)s)")
    parser.add_argument("--per-keyword", type=int, default=settings["per_keyword"],
                        help="users to pull per keyword; each 100 costs one Roblox search minute (default %(default)s)")
    parser.add_argument("--shuffle", action="store_true", help="randomize keyword order")
    parser.add_argument("--max-followers", type=int, default=settings["max_followers"],
                        help="skip accounts with more followers than this (default %(default)s)")
    parser.add_argument("--batch-size", type=int, default=settings["batch_size"],
                        help="avatars per Gemini request (default %(default)s)")
    parser.add_argument("--min-score", type=int, default=settings["min_score"],
                        help="AI match score (1-10) an avatar needs to be saved (default %(default)s)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.gallery:
        show_gallery(max_followers=args.max_followers)
        return
    if args.discover is not None:
        show_discovery(args.discover or DISCOVER_DEFAULT_TERMS, make_session(), args.max_followers)
        return
    session = make_session()
    seen = load_seen()
    if args.usernames:
        run_usernames(args, session, seen)
        return
    api_key = get_api_key()
    if not api_key:
        sys.exit("No Gemini API key. Run without options to set one in the menu, "
                 "or set GEMINI_API_KEY (free key: https://aistudio.google.com/apikey)")
    try:
        run_auto(args, session, seen, api_key)
    except InvalidApiKey:
        sys.exit("Gemini rejected the API key - set a new one from the menu (option 9)")


if __name__ == "__main__":
    main()
