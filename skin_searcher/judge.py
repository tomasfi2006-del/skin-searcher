"""Gemini API judge, structured evaluation schema, and quota management."""
import base64
import json
import time
import requests

from skin_searcher.config import (
    EMBED_DIMS,
    EMBED_URL,
    GEMINI_MIN_INTERVAL,
    GEMINI_MODELS,
    GEMINI_URL,
)


class DailyQuotaExceeded(RuntimeError):
    pass


class InvalidApiKey(Exception):
    pass


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


JUDGE_PROMPT = """You are evaluating Roblox avatars for a creator with a very specific, curated aesthetic taste. You will see {n} avatar renders, each labeled "Avatar N:". Score each one on its own (do not compare them to each other) from 1 to 10.

AESTHETIC TASTE PROFILE (What they love — score 8-10):
- Y2K, Streetwear, Grunge, Alt / Drip: layered 3D clothing, detailed cargo pants, graphic/band tees, chain necklaces, beanies with glasses/goggles, baggy silhouettes.
- Stylized Anime / Chibi: expressive custom anime/manga faces, layered fluffy/messy hair (curtain bangs, mullets, wolf cuts, colored streaks).
- Alt / Emo / Scene / Dark Coquette / Zombie: dark or high-contrast palettes (black, deep red, slate, neon accents), ushankas, leg warmers, distinct creative silhouettes.
- Cyberpunk / Gaming: dark outfits with sharp intentional neon accents (pink, cyan, purple), floating familiars, techwear.
- Intentional coordination: cohesive 2-3 color palette and distinct UGC accessories.

STRICT DISLIKES (What they hate — score 1-4):
- Too Plain / Zero Effort: default starter items (bacon hair, pal hair, basic free t-shirts like the motorcycle tee, plain blue jeans, flat starter packs, no accessories).
- Messy Clutter / Random Event Spam: mismatched overlapping free event accessories (giant random wings + shoulder animals + food on chin) with no coherent style.
- Generic / Childish: standard blocky R6 with default smiley face and flat solid colors.

Hard criteria flags:
- clutter: boolean, true if overloaded with messy, disorganized, or overlapping accessories that make the avatar noisy.
- funny: boolean, true if it is a humorous, meme, funny novelty or parody look.
- has_standout_accessory: boolean, true if has a prominent eye-catching hat, weapon, wings, or signature piece.

Visual styling:
- dominant_colors: list up to 3 dominant colors (e.g. ["black", "white"], ["red", "black"], ["pink", "pastel blue"]).
- color_story: choose from ["monochrome", "color_blocking", "rainbow", "neon", "pastel", "earthy", "plain"].
- theme: a concise 1-2 word style descriptor (e.g. "streetwear", "y2k", "grunge", "anime", "emo", "cyberpunk", "casual", "meme", "plain").

Return one JSON entry per avatar."""

VERDICT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "avatar": {"type": "INTEGER"},
            "score": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
            "clutter": {"type": "BOOLEAN"},
            "funny": {"type": "BOOLEAN"},
            "has_standout_accessory": {"type": "BOOLEAN"},
            "dominant_colors": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "color_story": {
                "type": "STRING",
                "enum": ["monochrome", "color_blocking", "rainbow", "neon", "pastel", "earthy", "plain"],
            },
            "theme": {"type": "STRING"},
        },
        "required": ["avatar", "score", "reason"],
    },
}


class GeminiJudge:
    """Scores avatars with Gemini in batches, rotating models and parsing rich attributes."""

    def __init__(self, api_key):
        self.headers = {"x-goog-api-key": api_key}
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
        """Returns {index: dict_of_verdict_and_attributes}; missing indexes left out."""
        parts = [{"text": JUDGE_PROMPT.replace("{n}", str(len(images)))}]
        for i, img in enumerate(images, 1):
            parts.append({"text": f"Avatar {i}:"})
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode()}})
        for _ in range(2):
            try:
                verdicts = {}
                for v in json.loads(self._ask(parts, VERDICT_SCHEMA)):
                    if isinstance(v.get("avatar"), int) and 1 <= v["avatar"] <= len(images) and isinstance(v.get("score"), int):
                        idx = v["avatar"] - 1
                        verdicts[idx] = {
                            "score": v["score"],
                            "reason": v.get("reason", ""),
                            "dominant_colors": v.get("dominant_colors", []),
                            "color_story": v.get("color_story", "plain"),
                            "theme": v.get("theme", ""),
                            "has_standout_accessory": bool(v.get("has_standout_accessory", False)),
                            "clutter": bool(v.get("clutter", False)),
                            "funny": bool(v.get("funny", False)),
                        }
                return verdicts
            except (ValueError, AttributeError, TypeError):
                continue
        raise RuntimeError("unreadable reply")


EMBED_DAILY_MESSAGE = ("Google's free limit of 1000 taste-model lookups per day is used up "
                       "(set taste_keep to 100 in Settings to run without the taste model)")


def embed_image(png, api_key):
    """A fingerprint of how an avatar looks, or None if it couldn't be made right now."""
    body = {
        "content": {"parts": [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png).decode()}}]},
        "outputDimensionality": EMBED_DIMS,
    }
    for attempt in range(5):
        try:
            resp = requests.post(EMBED_URL, headers={"x-goog-api-key": api_key}, json=body, timeout=60)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.ok:
            try:
                return resp.json()["embedding"]["values"]
            except (KeyError, ValueError):
                return None
        if resp.status_code in (401, 403) or "API_KEY_INVALID" in resp.text:
            raise InvalidApiKey("Gemini rejected the API key")
        if resp.status_code == 429:
            if "PerDay" in resp.text:
                raise DailyQuotaExceeded(EMBED_DAILY_MESSAGE)
            time.sleep(min(_quota_info(resp)[1] or 5, 30))
            continue
        if resp.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        return None
    return None
