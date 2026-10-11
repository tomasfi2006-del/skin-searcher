"""Configuration, path management, and atomic state storage for skin_searcher."""
import getpass
import json
import os
import re
from pathlib import Path
import requests

# Root directory of the repository
ROOT_DIR = Path(__file__).resolve().parent.parent

# Catalog directory and persisted artifacts
CATALOG_DIR = ROOT_DIR / "catalog"
STATE_PATH = CATALOG_DIR / "seen_users.json"
OUTPUT_DIR = CATALOG_DIR / "avatars"
SETTINGS_PATH = CATALOG_DIR / "settings.json"
KEY_PATH = CATALOG_DIR / "gemini_key.txt"
FEEDBACK_PATH = CATALOG_DIR / "feedback.json"
FEEDBACK_DIR = CATALOG_DIR / "feedback"
EMBED_PATH = CATALOG_DIR / "taste_embeddings.npz"
LOOKS_CACHE_PATH = CATALOG_DIR / "looks.json"
GROUP_STATS_PATH = CATALOG_DIR / "group_stats.json"
METADATA_PATH = CATALOG_DIR / "avatars_metadata.json"
GALLERY_PATH = CATALOG_DIR / "gallery.html"

# Networking & concurrency
WORKERS = 8
MAX_GROUP_PAGES = 50

# Taste model & lookalike settings
EMBED_DIMS = 768
EMBED_WORKERS = 6
TASTE_MIN_PERFECT, TASTE_MIN_DISLIKED = 15, 50
LOOKALIKE_DISTANCE = 4
LOOKALIKE_MIN_DISLIKES = 20
DUPLICATE_LOOK_DISTANCE = 3  # Near-duplicate threshold against already saved avatars

# Gemini API defaults
GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent"
GEMINI_MIN_INTERVAL = 4.2

# Application constants
DISCOVER_DEFAULT_TERMS = ["streetwear", "y2k", "drip", "outfits", "fashion"]
DISLIKE_REASONS = [
    "Default / Starter",
    "Messy / Cluttered",
    "Troll / Meme",
    "Mismatched / Dull",
]
LIKE_REASONS = [
    "Streetwear / Drip",
    "Clean & Coordinated",
    "Fire Hair / Accessories",
    "Unique / Stylized",
]
OLD_DISLIKE_REASONS = [
    "Too plain",
    "Messy / cluttered",
    "Not my style",
    "Default / generic",
]
OLD_LIKE_REASONS = [
    "Drip / Streetwear",
    "Clean palette",
    "Cool hair / accessories",
    "Unique aesthetic",
]
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,40}")

DEFAULT_SETTINGS = {
    "max_followers": 500,
    "per_group": 300,
    "groups_per_term": 3,
    "per_keyword": 100,
    "batch_size": 10,
    "min_score": 8,
    "taste_keep": 90,
    "group_bail_min_eval": 80,   # Statistical threshold: min 80 evaluations (avoids abandoning 5% yield groups by chance)
    "group_bail_min_yield": 0.0, # Bail if 0 saved after 80 evaluations
    "use_vibrance_filter": 1,    # Pre-filter: 1 = on (skip plain avatars before Gemini), 0 = off
    "vibrance_min": 4,           # Calibrated colourful threshold (retains 95%+ of liked avatars)
}

SETTING_LABELS = {
    "max_followers": "Skip accounts with more followers than",
    "per_group": "New people to check per group",
    "groups_per_term": "Groups to use per group search word",
    "per_keyword": "People to pull per keyword",
    "batch_size": "Avatars per Gemini request",
    "min_score": "Minimum AI match score to save (1-10)",
    "taste_keep": "Taste model: keep this % of your Perfects (100 = off)",
    "group_bail_min_eval": "Group yield: min members checked before early bail",
    "use_vibrance_filter": "Local color pre-filter (1=on, 0=off)",
}


def atomic_write_json(path: Path, data, indent: int = 2):
    """Writes JSON atomically by saving to a temporary file and renaming."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp_{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_PATH.is_file():
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            settings.update({k: int(v) for k, v in saved.items() if k in settings})
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return settings


def save_settings(settings):
    atomic_write_json(SETTINGS_PATH, settings, indent=2)


def load_seen():
    if STATE_PATH.is_file():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()
    return set()


def save_seen(seen):
    atomic_write_json(STATE_PATH, sorted(seen))


def load_metadata():
    if METADATA_PATH.is_file():
        try:
            data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def save_metadata(metadata):
    atomic_write_json(METADATA_PATH, metadata, indent=2)


def update_avatar_metadata(name, meta):
    data = load_metadata()
    data[name] = meta
    save_metadata(data)


def get_api_key():
    """Returns GEMINI_API_KEY env var first, then key saved on disk."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def api_key_works(key):
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELS[0]}",
            headers={"x-goog-api-key": key},
            timeout=20,
        )
        return resp.ok
    except requests.RequestException:
        return False


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
    KEY_PATH.write_text(key, encoding="utf-8")
    print(f"  Key saved on this computer ({KEY_PATH.name}). It's never uploaded to GitHub.")
    return key


def require_api_key():
    return get_api_key() or set_api_key()
