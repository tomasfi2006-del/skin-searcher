"""Command-line interface, interactive terminal menu, and argument parsing."""
import argparse
import sys

from skin_searcher.config import (
    DEFAULT_SETTINGS,
    DISCOVER_DEFAULT_TERMS,
    OUTPUT_DIR,
    SETTING_LABELS,
    get_api_key,
    load_seen,
    load_settings,
    require_api_key,
    save_settings,
    set_api_key,
)
from skin_searcher.eval import run_offline_eval
from skin_searcher.group_stats import GroupTracker
from skin_searcher.judge import InvalidApiKey
from skin_searcher.pipeline import refresh_catalog, reset_catalog, run_auto, run_usernames
from skin_searcher.roblox import make_session
from skin_searcher.server import open_folder, show_discovery, show_gallery
from skin_searcher.taste import TasteModel, feedback_stats


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
        if answer.isdigit() and int(answer) >= 0:
            return int(answer)
        print("  Please type a whole number >= 0, or press Enter to keep it.")


def make_args(settings, **overrides):
    values = {"groups": None, "keywords": None, "usernames": None, "shuffle": False, **settings, **overrides}
    return argparse.Namespace(**values)


def menu_settings(settings):
    while True:
        print("\nSettings")
        keys = list(SETTING_LABELS)
        for i, key in enumerate(keys, 1):
            print(f"  {i}) {SETTING_LABELS[key]}: {settings.get(key, DEFAULT_SETTINGS.get(key))}")
        print("  0) Back")
        choice = input("Which one to change? ").strip()
        if choice in ("", "0"):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(keys):
            print("  Pick a number from the list.")
            continue
        key = keys[int(choice) - 1]
        settings[key] = ask_number(SETTING_LABELS[key], settings.get(key, DEFAULT_SETTINGS.get(key)))
        save_settings(settings)
        print("  Saved.")


def menu_taste_model(api_key, settings):
    stats = feedback_stats()
    print(f"\nYour ratings: {stats['liked']} Perfect, {stats['disliked']} Disliked.")
    if settings["taste_keep"] >= 100:
        print("The taste model is switched off in Settings (taste_keep = 100).")
        return
    model = TasteModel(api_key, settings["taste_keep"])
    try:
        model.train()
    except ValueError as e:
        print(e)
        return
    except ImportError:
        print("This needs numpy: run   python -m pip install numpy   and try again.")
        return
    except RuntimeError as e:
        print(f"Couldn't train it right now ({e}). Try again in a minute.")
        return
    print(f"\nDone. The taste model learned from {model.info['perfect']} Perfect + {model.info['disliked']} Disliked ratings.")
    print(f"In a self-test it keeps {model.info['kept']}% of your Perfects and rejects {model.info['rejected']}% of your Disliked.")
    print("It is retrained automatically before each search, so new ratings are picked up on their own.")


def menu_group_stats():
    tracker = GroupTracker()
    print("\nRoblox Group Yield Statistics")
    print(tracker.summary_table())


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
            menu_taste_model(key, settings)
    elif choice == "7":
        menu_group_stats()
        return True
    elif choice == "8":
        refresh_catalog(session)
        return True
    elif choice == "9":
        run_offline_eval()
        return True
    elif choice == "10":
        confirm = input("Are you sure you want to reset the catalog? (y/N): ").strip().lower()
        if confirm == "y":
            reset_catalog()
        else:
            print("Cancelled.")
        return True
    elif choice == "11":
        open_folder(OUTPUT_DIR)
        return False
    elif choice == "12":
        menu_settings(settings)
        return False
    elif choice == "13":
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
        need_perfect, need_disliked = TasteModel.missing_ratings()
        if need_perfect or need_disliked:
            print(f"  Taste model: off - needs {need_perfect} more Perfect and {need_disliked} more Disliked ratings")
        else:
            print("  Taste model: on (retrained before each search)")
        print("  ── FIND AVATARS ─────────────────────────")
        print("   1) Search Roblox groups        (recommended)")
        print("   2) Search by keyword           (slower)")
        print("   3) Save specific usernames     (direct download)")
        print()
        print("  ── REVIEW & TASTE ───────────────────────")
        print("   4) Review saved catalog        (grid view in browser)")
        print("   5) Discover new avatars        (rate candidates 1-by-1)")
        print("   6) Retrain taste model         (update AI with ratings)")
        print()
        print("  ── CATALOG & ANALYTICS ──────────────────")
        print("   7) Group yield stats           (which groups are best)")
        print("   8) Refresh saved catalog       (sync outfit/name changes)")
        print("   9) Offline evaluation          (test filter accuracy)")
        print("  10) Reset catalog               (wipe local skins & cache)")
        print()
        print("  ── TOOLS & SETTINGS ─────────────────────")
        print("  11) Open avatars folder         (in Windows Explorer)")
        print("  12) Settings")
        print("  13) Change Gemini API key")
        print("   0) Quit")
        try:
            choice = input("\nChoose: ").strip()
            if choice in ("0", "q", "quit", "exit"):
                return
            if menu_action(choice, session, settings):
                input("\nPress Enter to go back to the menu...")
        except KeyboardInterrupt:
            print("\nBack to the menu.")
        except InvalidApiKey:
            print("\nGemini rejected your API key. Choose 13 to enter a new one.")
        except EOFError:
            return


def main():
    if len(sys.argv) == 1:
        menu()
        return

    settings = load_settings()
    parser = argparse.ArgumentParser(
        description="Roblox avatar catalog builder with AI evaluation and taste modeling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--groups", nargs="+",
                      help="group search terms or group IDs, e.g. outfits fashion 5215595 (auto-judged by Gemini)")
    mode.add_argument("--keywords", nargs="+", help="user search terms, e.g. boy yellow red (auto-judged by Gemini)")
    mode.add_argument("--usernames", nargs="+", help="usernames you picked yourself (saved directly, no Gemini)")
    mode.add_argument("--gallery", action="store_true", help="review saved avatars in a grid: copy names, mark Perfect or Dislike")
    mode.add_argument("--discover", nargs="*", metavar="WORD",
                      help="rate fresh avatars from groups with the arrow keys (default words: outfits fashion)")
    mode.add_argument("--group-stats", action="store_true", help="display performance yield stats for all explored groups")
    mode.add_argument("--eval", action="store_true", help="run offline evaluation of taste model and color filters")
    mode.add_argument("--refresh", action="store_true", help="re-resolve usernames and check for changed avatar looks")
    mode.add_argument("--reset-catalog", action="store_true", help="reset catalog avatars, metadata, and seen cache")

    parser.add_argument("--per-group", type=int, default=settings["per_group"],
                        help="new members to judge per group (default %(default)s)")
    parser.add_argument("--groups-per-term", type=int, default=settings["groups_per_term"],
                        help="top matching groups to use per search term (default %(default)s)")
    parser.add_argument("--per-keyword", type=int, default=settings["per_keyword"],
                        help="users to pull per keyword (default %(default)s)")
    parser.add_argument("--shuffle", action="store_true", help="randomize keyword order")
    parser.add_argument("--max-followers", type=int, default=settings["max_followers"],
                        help="skip accounts with more followers than this (default %(default)s)")
    parser.add_argument("--batch-size", type=int, default=settings["batch_size"],
                        help="avatars per Gemini request (default %(default)s)")
    parser.add_argument("--min-score", type=int, default=settings["min_score"],
                        help="AI match score (1-10) an avatar needs to be saved (default %(default)s)")
    parser.add_argument("--taste-keep", type=int, default=settings["taste_keep"],
                        help="taste model keeps this %% of your Perfects; lower = pickier, 100 = model off (default %(default)s)")
    parser.add_argument("--use-vibrance-filter", type=int, default=settings.get("use_vibrance_filter", 1),
                        help="pre-filter plain avatars before Gemini: 1=on, 0=off (default %(default)s)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()

    if args.eval:
        run_offline_eval()
        return
    if args.refresh:
        refresh_catalog(session)
        return
    if args.reset_catalog:
        reset_catalog()
        return
    if args.group_stats:
        menu_group_stats()
        return
    if args.gallery:
        show_gallery(max_followers=args.max_followers)
        return
    if args.discover is not None:
        show_discovery(args.discover or DISCOVER_DEFAULT_TERMS, session, args.max_followers)
        return

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
        sys.exit("Gemini rejected the API key - set a new one from the menu (option 13)")
