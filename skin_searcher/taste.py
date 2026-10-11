"""Taste modeling, look hashes, diversity deduplication, and user feedback ratings."""
import io
import json
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from skin_searcher.config import (
    CATALOG_DIR,
    DISLIKE_REASONS,
    DUPLICATE_LOOK_DISTANCE,
    EMBED_DIMS,
    EMBED_PATH,
    EMBED_WORKERS,
    FEEDBACK_DIR,
    FEEDBACK_PATH,
    LIKE_REASONS,
    LOOKALIKE_DISTANCE,
    LOOKALIKE_MIN_DISLIKES,
    LOOKS_CACHE_PATH,
    NAME_RE,
    OLD_DISLIKE_REASONS,
    OLD_LIKE_REASONS,
    OUTPUT_DIR,
    TASTE_MIN_DISLIKED,
    TASTE_MIN_PERFECT,
    atomic_write_json,
    load_seen,
    save_seen,
)
from skin_searcher.judge import DailyQuotaExceeded, embed_image
from skin_searcher.roblox import follower_counts

ALL_DISLIKE_REASONS = set(DISLIKE_REASONS) | set(OLD_DISLIKE_REASONS)
ALL_LIKE_REASONS = set(LIKE_REASONS) | set(OLD_LIKE_REASONS)


def vibrance(png_bytes):
    """Measures color vibrance, saturation share, hue diversity, and grey share.

    Returns dict with metrics, or None if image has no readable avatar pixels.
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGBA").resize((140, 140))
        a = np.asarray(im)
        mask = a[..., 3] > 200
        if mask.sum() < 500:
            return None
        rgb = a[..., :3][mask].astype(float)
        hsv = np.asarray(Image.fromarray(a[..., :3]).convert("HSV"))[mask].astype(float)
        h, s, v = hsv[:, 0] * 360 / 255, hsv[:, 1] / 255, hsv[:, 2] / 255
        rg, yb = rgb[:, 0] - rgb[:, 1], 0.5 * (rgb[:, 0] + rgb[:, 1]) - rgb[:, 2]
        colourful = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
        colour_px = (s > 0.45) & (v > 0.35)
        hist = np.bincount((h[colour_px] // 30).astype(int) % 12, minlength=12) / mask.sum()
        return {
            "colourful": colourful,
            "sat_share": float(colour_px.mean()),
            "hues": int((hist > 0.04).sum()),
            "grey_share": float(((s < 0.15) | (v < 0.15)).mean()),
        }
    except Exception:
        return None


def is_plain_avatar(png_bytes, min_colourful: float = 4.0):
    """Calibrated local pre-filter: skips broken/empty renders or lifeless plain avatars.

    Keeps 95%+ of liked avatars while filtering uninteresting starter looks without Gemini quota.
    """
    vib = vibrance(png_bytes)
    if vib is None:
        return True  # Empty/broken render (less than 500 solid pixels)
    # Avatars with very low colourful AND nearly 100% grey/washed-out pixels
    if vib["colourful"] < min_colourful and vib["grey_share"] > 0.98:
        return True
    return False


def look_hash(png):
    """A 144-bit difference hash fingerprint of an avatar's visual layout."""
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
    """Skips avatars that look almost identical to ones you disliked."""

    def __init__(self):
        self.disliked, self.liked = [], []
        try:
            cache = json.loads(LOOKS_CACHE_PATH.read_text(encoding="utf-8"))
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
            if value is None:
                self.disliked = self.liked = []
                return
            fresh[key] = value
            (self.disliked if verdict == "disliked" else self.liked).append(value)
        if fresh != cache:
            try:
                atomic_write_json(LOOKS_CACHE_PATH, fresh)
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
        return min([bin(h ^ l).count("1") for l in self.liked] + [99]) > near


class SavedLooks:
    """Prevents catalog duplication by checking similarity against already saved avatars."""

    def __init__(self):
        self.hashes = {}
        self._load()

    def _load(self):
        if not OUTPUT_DIR.exists():
            return
        for path in OUTPUT_DIR.glob("*.png"):
            h = look_hash(path.read_bytes())
            if h is not None:
                self.hashes[path.stem] = h

    def add(self, name, png):
        h = look_hash(png)
        if h is not None:
            self.hashes[name] = h

    def is_duplicate(self, png, max_dist=DUPLICATE_LOOK_DISTANCE):
        if not self.hashes:
            return False, None
        h = look_hash(png)
        if h is None:
            return False, None
        for name, saved_h in self.hashes.items():
            dist = bin(h ^ saved_h).count("1")
            if dist <= max_dist:
                return True, name
        return False, None


def load_feedback():
    try:
        if FEEDBACK_PATH.is_file():
            data = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def save_feedback(feedback):
    atomic_write_json(FEEDBACK_PATH, feedback, indent=1)


def feedback_stats(feedback=None):
    fb = load_feedback() if feedback is None else feedback
    liked = sum(1 for v in fb.values() if v.get("verdict") == "liked")
    disliked = sum(1 for v in fb.values() if v.get("verdict") == "disliked")
    liked_reasons = Counter(v.get("reason") for v in fb.values() if v.get("verdict") == "liked" and v.get("reason"))
    disliked_reasons = Counter(v.get("reason") for v in fb.values() if v.get("verdict") == "disliked" and v.get("reason"))
    return {
        "liked": liked,
        "disliked": disliked,
        "liked_reasons": dict(liked_reasons),
        "disliked_reasons": dict(disliked_reasons),
    }


def dislike_reason_counts():
    counts = Counter(v.get("reason") for v in load_feedback().values()
                     if v.get("verdict") == "disliked" and v.get("reason"))
    return counts.most_common()


def rate_avatar(name, action, reason=None):
    """Records a rating. action: like | dislike | reason | clear."""
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
        entry = {"verdict": "liked", "time": now}
        if reason and isinstance(reason, str) and reason.strip():
            entry["reason"] = reason.strip()[:100]
        feedback[name] = entry
    elif action == "dislike":
        if avatar.exists():
            disliked_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(avatar), str(disliked_file))
        elif not disliked_file.exists():
            raise ValueError("no such avatar")
        liked_copy.unlink(missing_ok=True)
        entry = {"verdict": "disliked", "time": now}
        if reason and isinstance(reason, str) and reason.strip():
            entry["reason"] = reason.strip()[:100]
        feedback[name] = entry
    elif action == "reason":
        entry = feedback.get(name, {})
        verdict = entry.get("verdict")
        if verdict in ("disliked", "liked") and reason and isinstance(reason, str) and reason.strip():
            feedback[name]["reason"] = reason.strip()[:100]
        else:
            raise ValueError("nothing to tag")
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
    """Records a rating for an avatar shown in discovery."""
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
            (OUTPUT_DIR / f"{cand['name']}.png").write_bytes(cand["image"])
    if reason and isinstance(reason, str) and reason.strip():
        entry["reason"] = reason.strip()[:100]
    feedback = load_feedback()
    feedback[name] = entry
    save_feedback(feedback)
    seen = load_seen()
    seen.add(cand["id"])
    save_seen(seen)
    return feedback_stats(feedback), info


def _fit(np, X, y, lam=1.0):
    """Class-balanced ridge classifier in dual form (numpy only)."""
    t = np.where(y == 1, 1.0, -1.0)
    w = np.where(t > 0, 0.5 / (t > 0).sum(), 0.5 / (t < 0).sum()) * len(t)
    alpha = np.linalg.solve(X @ X.T + lam * np.diag(1 / w), t)
    return X.T @ alpha


class TasteModel:
    """Learns your taste from your Perfect and Disliked ratings."""

    def __init__(self, api_key, keep=90):
        self.api_key = api_key
        self.keep = min(max(int(keep), 50), 99)
        self.beta = self.bar = None
        self.info = {}

    @staticmethod
    def missing_ratings():
        stats = feedback_stats()
        return max(TASTE_MIN_PERFECT - stats["liked"], 0), max(TASTE_MIN_DISLIKED - stats["disliked"], 0)

    def train(self, log=print):
        try:
            import numpy as np
        except ImportError:
            raise ImportError("numpy") from None
        need_perfect, need_disliked = self.missing_ratings()
        if need_perfect or need_disliked:
            raise ValueError(f"The taste model switches on once you have {TASTE_MIN_PERFECT} Perfect and "
                             f"{TASTE_MIN_DISLIKED} Disliked ratings (about {need_perfect} more Perfect and "
                             f"{need_disliked} more Disliked). Menu option 5 (Discover) is the fastest way.")
        feedback = load_feedback()
        rated = {n: v["verdict"] for n, v in feedback.items()
                 if v.get("verdict") in ("liked", "disliked") and (FEEDBACK_DIR / v["verdict"] / f"{n}.png").is_file()}

        cache = {}
        try:
            with np.load(EMBED_PATH) as z:
                if z["vecs"].shape[1] == EMBED_DIMS:
                    cache = dict(zip([str(n) for n in z["names"]], z["vecs"].astype(np.float64)))
        except (OSError, ValueError, KeyError):
            pass
        todo = [n for n in rated if n not in cache]
        if todo:
            log(f"Learning your taste: reading {len(todo)} rated avatars (one time only, roughly "
                f"{max(1, round(len(todo) / (EMBED_WORKERS * 0.3) / 60))} min; uses {len(todo)} of Google's "
                f"1000 free daily lookups)...")

            stop = threading.Event()

            def one(name):
                if stop.is_set():
                    return name, None
                try:
                    return name, embed_image((FEEDBACK_DIR / rated[name] / f"{name}.png").read_bytes(), self.api_key)
                except DailyQuotaExceeded:
                    stop.set()
                    return name, None

            def save():
                CATALOG_DIR.mkdir(parents=True, exist_ok=True)
                keep_names = [n for n in cache if n in rated]
                np.savez_compressed(EMBED_PATH, names=np.array(keep_names),
                                    vecs=np.array([cache[n] for n in keep_names], dtype=np.float16))

            try:
                with ThreadPoolExecutor(EMBED_WORKERS) as pool:
                    for i, (name, vec) in enumerate(pool.map(one, todo), 1):
                        if vec is not None and len(vec) == EMBED_DIMS:
                            cache[name] = np.array(vec, dtype=np.float64)
                        if i % 100 == 0:
                            log(f"  {i}/{len(todo)}")
                            save()
            finally:
                save()
            if stop.is_set():
                left = len([n for n in rated if n not in cache])
                log(f"Google's free daily limit for this was reached: {left} of your ratings will be read tomorrow.")

        names = [n for n in rated if n in cache]
        X = np.array([cache[n] for n in names])
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        y = np.array([1 if rated[n] == "liked" else 0 for n in names])
        if y.sum() < TASTE_MIN_PERFECT or (1 - y).sum() < TASTE_MIN_DISLIKED:
            raise RuntimeError("couldn't read enough of your rated avatars right now - try again in a minute")

        rng = np.random.RandomState(0)
        fold = np.zeros(len(y), dtype=int)
        for label in (0, 1):
            members = rng.permutation(np.where(y == label)[0])
            fold[members] = np.arange(len(members)) % 5
        held_out = np.zeros(len(y))
        for f in range(5):
            train, test = fold != f, fold == f
            held_out[test] = X[test] @ _fit(np, X[train], y[train])
        self.bar = float(np.quantile(held_out[y == 1], 1 - self.keep / 100))
        self.beta = _fit(np, X, y)
        self.info = {
            "perfect": int(y.sum()),
            "disliked": int((1 - y).sum()),
            "kept": round(100 * float((held_out[y == 1] >= self.bar).mean())),
            "rejected": round(100 * float((held_out[y == 0] < self.bar).mean())),
        }

    def accepts(self, vec):
        import numpy as np
        v = np.array(vec, dtype=np.float64)
        return float((v / np.linalg.norm(v)) @ self.beta) >= self.bar
