"""Group performance tracking, yield statistics, sliding-window scoring, and early-exit optimization."""
import json
import time

from skin_searcher.config import GROUP_STATS_PATH, atomic_write_json

MAX_RECENT_EVALUATIONS = 250  # Sliding window size for recent yield calculations


class GroupTracker:
    """Tracks candidates evaluated vs saved per Roblox group to optimize Gemini quota.

    Uses a sliding window of recent evaluations to prevent legacy runs from permanently
    biasing group limits when prompts or scoring criteria evolve.
    """

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
            "recent_runs": [],
            "last_updated": 0,
        })
        entry["name"] = name
        entry["evaluated"] += evaluated_add
        entry["saved"] += saved_add

        # Add to sliding window
        runs = entry.get("recent_runs", [])
        runs.append({"eval": evaluated_add, "saved": saved_add, "time": int(time.time())})

        # Trim sliding window to MAX_RECENT_EVALUATIONS
        while len(runs) > 1 and sum(r["eval"] for r in runs) > MAX_RECENT_EVALUATIONS:
            runs.pop(0)
        entry["recent_runs"] = runs

        # Compute lifetime and recent yield
        if entry["evaluated"] > 0:
            entry["yield_pct"] = round(100.0 * entry["saved"] / entry["evaluated"], 1)

        r_eval = sum(r["eval"] for r in runs)
        r_saved = sum(r["saved"] for r in runs)
        entry["recent_yield_pct"] = round(100.0 * r_saved / r_eval, 1) if r_eval > 0 else entry["yield_pct"]

        entry["last_updated"] = int(time.time())
        self.stats[gid] = entry
        self.save()

    def should_early_bail(self, current_eval: int, current_saved: int, min_eval: int = 80, min_yield: float = 0.0):
        """Statistical early bail check: defaults to min 80 evaluated to prevent false bail on 3-5% yield groups."""
        if current_eval < min_eval:
            return False, ""
        current_yield = 100.0 * current_saved / current_eval
        if current_saved == 0:
            return True, f"0 saved out of {current_eval} evaluated (0.0% yield) - bailing early to conserve quota"
        if current_yield < min_yield:
            return True, f"yield {current_yield:.1f}% below minimum {min_yield:.1f}% - bailing early"
        return False, ""

    def get_effective_limit(self, group_id, base_limit: int):
        """Adjusts member budget using recent sliding window yield rather than stale all-time accumulation."""
        gid = str(group_id)
        if gid not in self.stats:
            return base_limit
        entry = self.stats[gid]
        runs = entry.get("recent_runs", [])
        recent_eval = sum(r["eval"] for r in runs) if runs else entry.get("evaluated", 0)
        recent_yield = entry.get("recent_yield_pct", entry.get("yield_pct", 0.0))

        if recent_eval >= 60:
            if recent_yield >= 8.0:
                # High recent yield: allow 1.5x budget
                return int(base_limit * 1.5)
            elif recent_yield <= 1.0:
                # Low recent yield: cap to 50 to avoid wasting quota
                return min(base_limit, 50)
        return base_limit

    def summary_table(self):
        """Returns a formatted leaderboard of groups by yield and recent performance."""
        if not self.stats:
            return "No group statistics recorded yet."
        sorted_groups = sorted(
            self.stats.values(),
            key=lambda x: (x.get("recent_yield_pct", x.get("yield_pct", 0)), x.get("saved", 0)),
            reverse=True,
        )
        lines = [
            f"{'Group Name':<30} {'Evaluated':>10} {'Saved':>7} {'Recent Yield':>13} {'All-Time':>9}",
            "-" * 73,
        ]
        for g in sorted_groups[:25]:
            name = (g.get("name", "Unknown")[:27] + "...") if len(g.get("name", "")) > 30 else g.get("name", "Unknown")
            r_yield = g.get("recent_yield_pct", g.get("yield_pct", 0.0))
            lines.append(
                f"{name:<30} {g.get('evaluated', 0):>10,d} {g.get('saved', 0):>7,d} {r_yield:>12.1f}% {g.get('yield_pct', 0.0):>8.1f}%"
            )
        return "\n".join(lines)
