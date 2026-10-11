"""Roblox API interaction, rate gating, and asset downloading."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from requests.adapters import HTTPAdapter

from skin_searcher.config import WORKERS

ROBLOX_SEARCH_URL = "https://users.roblox.com/v1/users/search"
ROBLOX_USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_FOLLOWERS_URL = "https://friends.roblox.com/v1/users/{}/followers/count"
ROBLOX_THUMB_URL = "https://thumbnails.roblox.com/v1/users/avatar"
ROBLOX_GROUP_URL = "https://groups.roblox.com/v1/groups/{}"
ROBLOX_GROUP_SEARCH_URL = "https://groups.roblox.com/v1/groups/search"
ROBLOX_GROUP_MEMBERS_URL = "https://groups.roblox.com/v1/groups/{}/users"


def _header_int(resp, name):
    try:
        return int(float(resp.headers[name].split(",")[0]))
    except (KeyError, ValueError, AttributeError):
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
    """A numeric term is a group ID; anything else searches groups by keyword."""
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
            time.sleep(2)
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
