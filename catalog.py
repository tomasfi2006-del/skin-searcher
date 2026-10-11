#!/usr/bin/env python3
"""Roblox avatar catalog builder with AI evaluation and taste modeling.

Modular entry point delegating to the skin_searcher package.
"""
from skin_searcher.config import (
    CATALOG_DIR,
    DEFAULT_SETTINGS,
    DISCOVER_DEFAULT_TERMS,
    DISLIKE_REASONS,
    EMBED_DIMS,
    EMBED_PATH,
    EMBED_WORKERS,
    FEEDBACK_DIR,
    FEEDBACK_PATH,
    GALLERY_PATH,
    GROUP_STATS_PATH,
    KEY_PATH,
    LOOKS_CACHE_PATH,
    METADATA_PATH,
    OUTPUT_DIR,
    SETTINGS_PATH,
    STATE_PATH,
    TASTE_MIN_DISLIKED,
    TASTE_MIN_PERFECT,
    api_key_works,
    get_api_key,
    load_metadata,
    load_seen,
    load_settings,
    require_api_key,
    save_metadata,
    save_seen,
    save_settings,
    set_api_key,
)
from skin_searcher.group_stats import GroupTracker
from skin_searcher.judge import (
    DailyQuotaExceeded,
    GeminiJudge,
    InvalidApiKey,
    embed_image,
)
from skin_searcher.eval import run_offline_eval
from skin_searcher.pipeline import (
    AutoRun,
    CandidateFeed,
    refresh_catalog,
    reset_catalog,
    run_auto,
    run_usernames,
    save_avatar,
    search_groups,
    search_keywords,
)
from skin_searcher.roblox import (
    RateGate,
    fetch_avatar_images,
    find_groups,
    follower_counts,
    group_member_pages,
    make_session,
    resolve_usernames,
    roblox,
    search_users,
)
from skin_searcher.server import (
    build_gallery,
    discover_html,
    gallery_html,
    make_review_server,
    open_folder,
    open_path,
    show_discovery,
    show_gallery,
)
from skin_searcher.taste import (
    KnownLooks,
    SavedLooks,
    TasteModel,
    dislike_reason_counts,
    feedback_stats,
    load_feedback,
    look_hash,
    rate_avatar,
    rate_candidate,
    save_feedback,
)
from skin_searcher.ui import main, menu

if __name__ == "__main__":
    main()
