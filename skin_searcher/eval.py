import os
from skin_searcher.config import FEEDBACK_DIR
from skin_searcher.taste import TasteModel, load_feedback, vibrance


def run_offline_eval(verbose: bool = True):
    """Evaluates filter accuracy, taste model metrics, and vibrance distributions offline."""
    fb = load_feedback()
    liked_dir = FEEDBACK_DIR / "liked"
    disliked_dir = FEEDBACK_DIR / "disliked"

    liked_files = [f for f in os.listdir(liked_dir) if f.endswith(".png")] if liked_dir.is_dir() else []
    disliked_files = [f for f in os.listdir(disliked_dir) if f.endswith(".png")] if disliked_dir.is_dir() else []

    print("\n" + "=" * 60)
    print("        OFFLINE BENCHMARK & EVALUATION")
    print("=" * 60)
    print(f"Dataset: {len(liked_files)} Liked (Perfect) | {len(disliked_files)} Disliked")

    # 1. Taste Model Accuracy
    print("\n--- 1. Taste Model Cross-Validation ---")
    try:
        model = TasteModel("dummy", keep=90)
        model.train(log=lambda msg: None)
        print(f"  Ratings used:     {model.info['perfect']} Perfect, {model.info['disliked']} Disliked")
        print(f"  Recall (Liked):   {model.info['kept']}% of Perfects preserved")
        print(f"  Rejection (Bad):  {model.info['rejected']}% of Disliked rejected offline")
    except Exception as e:
        print(f"  Taste model evaluation unavailable ({e})")

    # 2. Local Color / Vibrance Pre-Filter Performance
    print("\n--- 2. Color Pre-Filter Calibration (vibrance) ---")
    liked_vibs = []
    for f in liked_files:
        try:
            v = vibrance((liked_dir / f).read_bytes())
            if v:
                liked_vibs.append(v)
        except Exception:
            pass

    disliked_vibs = []
    for f in disliked_files:
        try:
            v = vibrance((disliked_dir / f).read_bytes())
            if v:
                disliked_vibs.append(v)
        except Exception:
            pass

    if liked_vibs and disliked_vibs:
        thresholds = [3.0, 4.0, 5.0, 6.0, 8.0]
        print(f"{'Threshold (colourful)':<24} {'Liked Kept':>14} {'Disliked Filtered':>20}")
        print("-" * 60)
        for t in thresholds:
            kept_liked = sum(1 for v in liked_vibs if not (v["colourful"] < t and v["grey_share"] > 0.98))
            filt_disliked = sum(1 for v in disliked_vibs if (v["colourful"] < t and v["grey_share"] > 0.98))
            l_pct = 100.0 * kept_liked / len(liked_vibs)
            d_pct = 100.0 * filt_disliked / len(disliked_vibs)
            print(f"< {t:.1f} and grey > 0.98:     {kept_liked}/{len(liked_vibs)} ({l_pct:5.1f}%)    {filt_disliked}/{len(disliked_vibs)} ({d_pct:5.1f}%)")

    # 3. Dislike Reason Breakdown
    print("\n--- 3. Dislike Reason Distribution ---")
    reasons = {}
    for name, v in fb.items():
        if v.get("verdict") == "disliked":
            r = v.get("reason", "No reason tagged")
            reasons[r] = reasons.get(r, 0) + 1
    for r, count in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
        print(f"  {r:<28}: {count:>4d} ({100.0 * count / max(len(disliked_files), 1):.1f}%)")

    print("\n" + "=" * 60)
