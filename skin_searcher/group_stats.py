"""Group performance tracking, yield statistics, and early-exit optimization."""
import json
import time

from skin_searcher.config import GROUP_STATS_PATH, atomic_write_json


class GroupTracker:
    """Tracks candidates evaluated vs saved per Roblox group to optimize Gemini quota."""

    def __init__(self):
        self.stats = self._load()

    def _load(self):
        if GROUP_STATS_PATH.is_file():
            try:
                data = json.loads(GROUP_STATS_PATH.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                pass
        return {}

    def save(self):
        atomic_write_json(GROUP_STATS_PATH, self.stats, indent=2)

    def record(self, group_id, name, evaluated_add: int, saved_add: int):
        gid = str(group_id)
        entry = self.stats.get(gid, {
            "name": name,
            "evaluated": 0,
            "saved": 0,
            "yield_pct": 0.0,
            "last_updated": 0,
        })
        entry["name"] = name
        entry["evaluated"] += evaluated_add
        entry["saved"] += saved_add
        if entry["evaluated"] > 0:
            entry["yield_pct"] = round(100.0 * entry["saved"] / entry["evaluated"], 1)
        entry["last_updated"] = int(time.time())
        self.stats[gid] = entry
        self.save()

    def should_early_bail(self, current_eval: int, current_saved: int, min_eval: int = 40, min_yield: float = 0.0):
        """Checks if a group run is yielding too poorly to justify continuing."""
        if current_eval < min_eval:
            return False, ""
        current_yield = 100.0 * current_saved / current_eval
        if current_saved == 0:
            return True, f"0 saved out of {current_eval} evaluated (0.0% yield) - bailing early to conserve quota"
        if current_yield < min_yield:
            return True, f"yield {current_yield:.1f}% below minimum {min_yield:.1f}% - bailing early"
        return False, ""

    def get_effective_limit(self, group_id, base_limit: int):
        """Optionally adjusts per-group member budget based on historical performance."""
        gid = str(group_id)
        if gid not in self.stats:
            return base_limit
        entry = self.stats[gid]
        evaluated, yield_pct = entry.get("evaluated", 0), entry.get("yield_pct", 0.0)
        if evaluated >= 80:
            if yield_pct >= 8.0:
                # High-yield group: increase budget by 50%
                return int(base_limit * 1.5)
            elif yield_pct <= 1.0:
                # Poor-yield group: limit budget to avoid wasting quota
                return min(base_limit, 50)
        return base_limit

    def summary_table(self):
        """Returns a formatted leaderboard of groups by yield."""
        if not self.stats:
            return "No group statistics recorded yet."
        sorted_groups = sorted(
            self.stats.values(),
            key=lambda x: (x.get("yield_pct", 0), x.get("saved", 0)),
            reverse=True,
        )
        lines = [
            f"{'Group Name':<32} {'Evaluated':>10} {'Saved':>8} {'Yield':>8}",
            "-" * 62,
        ]
        for g in sorted_groups[:20]:
            name = (g.get("name", "Unknown")[:29] + "...") if len(g.get("name", "")) > 32 else g.get("name", "Unknown")
            lines.append(
                f"{name:<32} {g.get('evaluated', 0):>10,d} {g.get('saved', 0):>8,d} {g.get('yield_pct', 0.0):>7.1f}%"
            )
        return "\n".join(lines)
