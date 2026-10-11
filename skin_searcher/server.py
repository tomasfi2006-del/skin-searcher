"""Local review and discovery web servers with color-story filtering and metadata badges."""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from skin_searcher.config import (
    CATALOG_DIR,
    DEFAULT_SETTINGS,
    DISLIKE_REASONS,
    GALLERY_PATH,
    LIKE_REASONS,
    OUTPUT_DIR,
    load_metadata,
)
from skin_searcher.pipeline import CandidateFeed
from skin_searcher.taste import feedback_stats, load_feedback, rate_avatar, rate_candidate

GALLERY_TEMPLATE = """<!doctype html>
<html lang="en" class="h-full">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher - Catalog</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'media',
    theme: {
      extend: {
        fontFamily: {
          sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        }
      }
    }
  }
</script>
<style>
  :root { --card-size: 210px; }
  .grid-dynamic { grid-template-columns: repeat(auto-fill, minmax(var(--card-size), 1fr)); }
</style>
</head>
<body class="h-full bg-slate-100 dark:bg-[#11141d] text-slate-900 dark:text-slate-100 font-sans antialiased flex flex-col min-h-screen">

<!-- Sticky Header -->
<header class="sticky top-0 z-30 bg-white/95 dark:bg-[#1c2230]/95 backdrop-blur-md border-b border-slate-200 dark:border-[#2a3441] transition-colors">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3">
    <!-- Brand & Stats -->
    <div class="flex items-center gap-3">
      <div class="flex items-center gap-2">
        <span class="w-8 h-8 rounded-xl bg-indigo-600 text-white flex items-center justify-center font-bold text-sm shadow-sm shadow-indigo-500/20">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/></svg>
        </span>
        <h1 class="text-base sm:text-lg font-bold tracking-tight">Skin Searcher</h1>
      </div>
      <span id="count" class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"></span>
      <span id="stats" class="hidden sm:inline-block px-2.5 py-0.5 text-xs font-medium rounded-full bg-indigo-50 dark:bg-indigo-950/60 text-indigo-700 dark:text-indigo-300 border border-indigo-200/60 dark:border-indigo-800/60"></span>
    </div>

    <!-- Discover Button (if enabled) -->
    <a id="nav" href="#" hidden class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-xl bg-indigo-600 text-white hover:bg-indigo-700 shadow-sm shadow-indigo-600/20 transition">
      <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
      Discover New
    </a>

    <!-- Controls & Filters -->
    <div class="flex flex-wrap items-center gap-2 w-full lg:w-auto">
      <!-- Search Input -->
      <div class="relative flex-1 sm:w-56">
        <svg class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
        <input type="search" id="q" placeholder="Search name, color, theme..." class="w-full pl-9 pr-3 py-1.5 text-xs rounded-xl border border-slate-200 dark:border-[#2a3441] bg-slate-50 dark:bg-[#161b26] text-slate-900 dark:text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 transition">
      </div>

      <!-- Color Story Selector -->
      <select id="story" aria-label="Style" class="py-1.5 px-2.5 text-xs rounded-xl border border-slate-200 dark:border-[#2a3441] bg-slate-50 dark:bg-[#161b26] text-slate-700 dark:text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 cursor-pointer">
        <option value="all">All Styles</option>
        <option value="monochrome">Monochrome</option>
        <option value="color_blocking">Color Blocking</option>
        <option value="rainbow">Rainbow</option>
        <option value="neon">Neon</option>
        <option value="pastel">Pastel</option>
        <option value="earthy">Earthy</option>
        <option value="plain">Plain</option>
      </select>

      <!-- Rating Filter -->
      <select id="show" aria-label="Show" class="py-1.5 px-2.5 text-xs rounded-xl border border-slate-200 dark:border-[#2a3441] bg-slate-50 dark:bg-[#161b26] text-slate-700 dark:text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 cursor-pointer">
        <option value="all">Show All</option>
        <option value="new">Not Rated Yet</option>
        <option value="liked">Perfect Only</option>
      </select>

      <!-- Sort Filter -->
      <select id="sort" aria-label="Sort" class="py-1.5 px-2.5 text-xs rounded-xl border border-slate-200 dark:border-[#2a3441] bg-slate-50 dark:bg-[#161b26] text-slate-700 dark:text-slate-200 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 cursor-pointer">
        <option value="new">Newest First</option>
        <option value="score">Highest Score</option>
        <option value="az">A to Z</option>
      </select>

      <!-- Size Slider -->
      <div class="hidden sm:flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 pl-1">
        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16m-7 6h7"/></svg>
        <input type="range" id="size" min="140" max="360" value="210" class="w-20 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-lg appearance-none cursor-pointer accent-indigo-600">
      </div>
    </div>
  </div>
</header>

<!-- Hint Banner -->
<div id="hint" class="bg-indigo-50/70 dark:bg-indigo-950/30 border-b border-indigo-100 dark:border-indigo-900/40 px-4 sm:px-6 py-2 text-xs text-indigo-900 dark:text-indigo-200">
  <div class="max-w-7xl mx-auto flex items-center gap-2">
    <svg class="w-4 h-4 text-indigo-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
    <span>Click any avatar to copy its username. Use <strong>Perfect</strong> or <strong>Dislike</strong> to teach your taste model.</span>
  </div>
</div>

<!-- Main Gallery Content -->
<main class="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6">
  <div id="grid" class="grid grid-dynamic gap-4 sm:gap-5"></div>
  <div id="empty" hidden class="py-20 text-center">
    <div class="w-12 h-12 rounded-2xl bg-slate-200/60 dark:bg-slate-800 flex items-center justify-center mx-auto mb-3 text-slate-400">
      <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"/></svg>
    </div>
    <p class="text-sm font-medium text-slate-500 dark:text-slate-400">No matching avatars found.</p>
  </div>
</main>

<!-- Floating Toast Notification -->
<div id="bar" role="status" class="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 px-4 py-2.5 rounded-2xl bg-slate-900/95 dark:bg-white/95 text-white dark:text-slate-900 shadow-xl border border-slate-800 dark:border-slate-200 backdrop-blur-md hidden items-center gap-3 text-xs sm:text-sm font-medium transition-all duration-200"></div>

<script>
const REVIEW = __REVIEW__, REASONS = __REASONS__, LIKE_REASONS = __LIKE_REASONS__, DISCOVER = __DISCOVER__;
let AVATARS = __DATA__, STATS = __STATS__;
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id), grid = $("grid"), bar = $("bar");
let barTimer;

function say(text, buttons, ms) {
  bar.textContent = "";
  const t = document.createElement("span");
  t.textContent = text;
  bar.appendChild(t);
  for (const [label, fn] of buttons || []) {
    const b = document.createElement("button");
    b.className = "px-2.5 py-1 text-xs font-semibold rounded-lg bg-white/20 dark:bg-slate-900/20 hover:bg-white/30 dark:hover:bg-slate-900/30 transition cursor-pointer";
    b.textContent = label;
    b.onclick = fn;
    bar.appendChild(b);
  }
  bar.classList.remove("hidden");
  bar.classList.add("flex");
  clearTimeout(barTimer);
  barTimer = setTimeout(() => {
    bar.classList.remove("flex");
    bar.classList.add("hidden");
  }, ms || 2500);
}

function copy(name) {
  const done = () => say("Copied @" + name + " to clipboard");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(name).then(done, () => fallback(name, done));
  } else {
    fallback(name, done);
  }
}

function fallback(name, done) {
  const t = document.createElement("textarea");
  t.value = name;
  t.style.position = "fixed";
  t.style.opacity = "0";
  document.body.appendChild(t);
  t.select();
  try { document.execCommand("copy"); done(); }
  catch (e) { say("Couldn't copy: " + name); }
  t.remove();
}

async function rate(name, action, reason) {
  const r = await fetch("/api/rate", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    body: JSON.stringify({ name, action, reason })
  });
  if (!r.ok) throw new Error(await r.text());
  STATS = (await r.json()).stats;
  showStats();
}

function showStats() {
  const total = STATS.liked + STATS.disliked;
  $("stats").textContent = total
    ? ("Rated " + total + ": " + STATS.liked + " perfect, " + STATS.disliked + " disliked (" + Math.round(100 * STATS.liked / total) + "% kept)")
    : "";
}

async function love(a, reason) {
  try {
    const action = (a.liked && !reason) ? "clear" : "like";
    await rate(a.name, action, reason);
    if (action === "clear") {
      a.liked = false;
      a.reason = "";
      say("Rating removed for @" + a.name);
    } else {
      a.liked = true;
      if (reason) a.reason = reason;
      say(reason ? "@" + a.name + " marked Perfect! (" + reason + ")" : "@" + a.name + " marked Perfect!");
    }
    render();
  } catch (e) {
    say("Couldn't save rating", [], 3000);
  }
}

async function dislike(a, reason, cardEl) {
  try {
    await rate(a.name, "dislike", reason);
  } catch (e) {
    say("Couldn't save rating", [], 3000);
    return;
  }
  if (cardEl) {
    cardEl.classList.add("opacity-0", "scale-95", "transition-all", "duration-200");
  }
  setTimeout(() => {
    AVATARS = AVATARS.filter(x => x !== a);
    render();
  }, 180);

  const undoBtn = [["Undo", async () => {
    try {
      await rate(a.name, "clear");
      a.liked = false;
      AVATARS.push(a);
      render();
      say("Restored @" + a.name);
    } catch (e) {
      say("Couldn't undo", [], 3000);
    }
  }]];

  const reasonMsg = reason ? " (" + reason + ")" : "";
  say("Disliked @" + a.name + reasonMsg, undoBtn, 6000);
}

function render() {
  const q = $("q").value.trim().toLowerCase(), sort = $("sort").value, show = $("show").value, story = $("story").value;
  const list = AVATARS.filter(a => {
    const matchQ = !q || a.name.toLowerCase().includes(q) || (a.theme && a.theme.toLowerCase().includes(q)) || (a.colors && a.colors.some(c => c.toLowerCase().includes(q)));
    const matchShow = (show === "all" || (show === "liked" ? a.liked : !a.liked));
    const matchStory = (story === "all" || (a.story && a.story.toLowerCase() === story.toLowerCase()));
    return matchQ && matchShow && matchStory;
  });

  if (sort === "az") list.sort((a, b) => a.name.localeCompare(b.name));
  else if (sort === "score") list.sort((a, b) => (b.score || 0) - (a.score || 0));
  else list.sort((a, b) => b.time - a.time);

  grid.textContent = "";
  for (const a of list) {
    const c = document.createElement("div");
    c.className = "group relative flex flex-col bg-white dark:bg-[#1c2230] border border-slate-200 dark:border-[#2a3441] rounded-2xl overflow-hidden shadow-sm hover:shadow-md transition-all duration-200" + (a.liked ? " ring-2 ring-emerald-500/90 dark:ring-emerald-400 border-transparent" : "");

    // Copy username button wrapper
    const b = document.createElement("button");
    b.className = "w-full text-left bg-transparent border-0 p-0 cursor-pointer block focus:outline-none";
    b.title = "Click to copy @" + a.name;

    // Image Stage
    const s = document.createElement("div");
    s.className = "relative aspect-square w-full bg-slate-50 dark:bg-[#161b26] flex items-center justify-center p-2.5 overflow-hidden";

    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = a.name;
    img.src = a.file;
    img.className = "w-full h-full object-contain transform group-hover:scale-105 transition-transform duration-200";
    s.appendChild(img);

    // Badges Container
    const badges = document.createElement("div");
    badges.className = "absolute top-2 left-2 right-2 flex flex-wrap gap-1 pointer-events-none";

    if (a.score) {
      const sb = document.createElement("span");
      sb.className = "px-2 py-0.5 text-[10px] font-bold rounded-lg bg-indigo-600 text-white shadow-sm";
      sb.textContent = a.score + "/10";
      badges.appendChild(sb);
    }
    if (a.liked && a.reason) {
      const lr = document.createElement("span");
      lr.className = "px-1.5 py-0.5 text-[10px] font-semibold rounded-md bg-emerald-600 text-white shadow-sm truncate max-w-[120px]";
      lr.textContent = "★ " + a.reason;
      badges.appendChild(lr);
    }
    if (a.story && a.story !== "plain") {
      const st = document.createElement("span");
      st.className = "px-1.5 py-0.5 text-[10px] font-semibold rounded-md bg-black/60 text-white backdrop-blur-sm capitalize";
      st.textContent = a.story.replace("_", " ");
      badges.appendChild(st);
    }
    if (a.theme) {
      const tb = document.createElement("span");
      tb.className = "px-1.5 py-0.5 text-[10px] font-medium rounded-md bg-slate-800/70 text-slate-200 backdrop-blur-sm truncate max-w-[110px]";
      tb.textContent = a.theme;
      badges.appendChild(tb);
    }
    s.appendChild(badges);

    // Copy hover indicator
    const copyBadge = document.createElement("div");
    copyBadge.className = "absolute bottom-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity bg-black/70 text-white text-[10px] font-medium px-2 py-0.5 rounded-md backdrop-blur-sm flex items-center gap-1 pointer-events-none";
    copyBadge.innerHTML = `<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>Copy`;
    s.appendChild(copyBadge);

    // Info area
    const info = document.createElement("div");
    info.className = "p-3 pb-2";

    const nameEl = document.createElement("div");
    nameEl.className = "font-semibold text-xs sm:text-sm text-slate-800 dark:text-slate-100 truncate group-hover:text-indigo-600 dark:group-hover:text-indigo-400 transition-colors";
    nameEl.textContent = a.name;

    const metaEl = document.createElement("div");
    metaEl.className = "text-[11px] text-slate-500 dark:text-slate-400 mt-0.5 truncate";
    metaEl.textContent = a.colors && a.colors.length ? a.colors.join(", ") : "\u00a0";

    info.append(nameEl, metaEl);
    b.append(s, info);
    b.addEventListener("click", () => copy(a.name));
    c.appendChild(b);

    // Actions & Reasons (only when REVIEW is active)
    if (REVIEW) {
      const actionsWrap = document.createElement("div");
      actionsWrap.className = "p-2.5 pt-0 mt-auto border-t border-slate-100 dark:border-[#2a3441]/60";

      // 1. Primary Action Buttons
      const mainActions = document.createElement("div");
      mainActions.className = "flex gap-1.5 pt-2";

      // Perfect Button Group
      const lWrap = document.createElement("div");
      lWrap.className = "flex-1 flex gap-1";

      const l = document.createElement("button");
      l.type = "button";
      l.className = a.liked
        ? "flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-2 text-xs font-semibold rounded-xl bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm transition"
        : "flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-2 text-xs font-medium rounded-xl bg-slate-50 dark:bg-slate-800/80 text-slate-700 dark:text-slate-200 hover:bg-emerald-50 hover:text-emerald-700 hover:border-emerald-300 dark:hover:bg-emerald-950/40 dark:hover:text-emerald-300 border border-slate-200 dark:border-slate-700 transition";

      l.innerHTML = a.liked
        ? `<svg class="w-3.5 h-3.5 fill-current shrink-0" viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg><span class="truncate">Perfect</span>`
        : `<svg class="w-3.5 h-3.5 stroke-current fill-none shrink-0" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z"/></svg><span class="truncate">Perfect</span>`;
      l.onclick = () => love(a);

      const lTag = document.createElement("button");
      lTag.type = "button";
      lTag.className = "px-2 py-1.5 text-xs font-semibold rounded-xl bg-slate-50 dark:bg-slate-800/80 text-slate-700 dark:text-slate-200 hover:bg-emerald-50 hover:text-emerald-700 hover:border-emerald-300 dark:hover:bg-emerald-950/40 dark:hover:text-emerald-300 border border-slate-200 dark:border-slate-700 transition" + (a.liked && a.reason ? " text-emerald-600 dark:text-emerald-400 font-bold" : "");
      lTag.title = "Select reason why you like this avatar";
      lTag.textContent = "★";

      lWrap.append(l, lTag);

      // Dislike Button (opens reason drawer)
      const d = document.createElement("button");
      d.type = "button";
      d.className = "flex-1 inline-flex items-center justify-center gap-1 py-1.5 px-2 text-xs font-medium rounded-xl bg-slate-50 dark:bg-slate-800/80 text-slate-700 dark:text-slate-200 hover:bg-rose-50 hover:text-rose-700 hover:border-rose-300 dark:hover:bg-rose-950/40 dark:hover:text-rose-300 border border-slate-200 dark:border-slate-700 transition";
      d.innerHTML = `<svg class="w-3.5 h-3.5 stroke-current fill-none shrink-0" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14H5.236a2 2 0 01-1.789-2.894l3.5-7A2 2 0 018.736 3h7.528a2 2 0 011.789 1.106l1.5 3A2 2 0 0119.553 9H16v5m-6 0v7m0-7l4 4m2-11h4a2 2 0 012 2v5a2 2 0 01-2 2h-4"/></svg><span class="truncate">Dislike</span>`;

      // 2. Like Reason Drawer
      const likeBox = document.createElement("div");
      likeBox.className = "hidden pt-2 flex flex-col gap-1.5";

      const likeHeader = document.createElement("div");
      likeHeader.className = "flex items-center justify-between text-[11px] font-semibold text-emerald-700 dark:text-emerald-400 mb-0.5";
      likeHeader.innerHTML = `<span>Why do you like it?</span>`;

      const likeCloseBtn = document.createElement("button");
      likeCloseBtn.type = "button";
      likeCloseBtn.className = "text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xs px-1";
      likeCloseBtn.textContent = "✕";
      likeCloseBtn.onclick = () => {
        likeBox.classList.add("hidden");
        mainActions.classList.remove("hidden");
      };
      likeHeader.appendChild(likeCloseBtn);
      likeBox.appendChild(likeHeader);

      const likeReasonsGrid = document.createElement("div");
      likeReasonsGrid.className = "grid grid-cols-2 gap-1";

      for (let i = 0; i < LIKE_REASONS.length; i++) {
        const rText = LIKE_REASONS[i];
        const rb = document.createElement("button");
        rb.type = "button";
        rb.className = "text-left text-[11px] py-1 px-1.5 rounded-lg bg-emerald-50/70 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-300 border border-emerald-200/70 dark:border-emerald-900/60 hover:bg-emerald-100 dark:hover:bg-emerald-900/60 transition truncate flex items-center gap-1.5 cursor-pointer" + (a.reason === rText ? " ring-1 ring-emerald-500 font-bold" : "");
        rb.title = rText;
        rb.innerHTML = `<span class="w-3.5 h-3.5 rounded bg-emerald-200/80 dark:bg-emerald-900/80 text-emerald-800 dark:text-emerald-200 text-[9px] font-bold flex items-center justify-center shrink-0">${i + 1}</span><span class="truncate">${rText}</span>`;
        rb.onclick = () => love(a, rText);
        likeReasonsGrid.appendChild(rb);
      }
      const customLikeBtn = document.createElement("button");
      customLikeBtn.type = "button";
      customLikeBtn.className = "col-span-2 text-left text-[11px] py-1 px-1.5 rounded-lg bg-emerald-50/70 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-300 border border-dashed border-emerald-300/70 dark:border-emerald-900/60 hover:bg-emerald-100 dark:hover:bg-emerald-900/60 transition truncate flex items-center gap-1.5 cursor-pointer";
      customLikeBtn.innerHTML = `<span class="w-3.5 h-3.5 rounded bg-emerald-200/80 dark:bg-emerald-900/80 text-emerald-800 dark:text-emerald-200 text-[9px] font-bold flex items-center justify-center shrink-0">5</span><span class="truncate">Custom note...</span>`;
      customLikeBtn.onclick = () => {
        const note = prompt("Enter custom note why you like @" + a.name + ":");
        if (note !== null && note.trim()) love(a, note.trim());
      };
      likeReasonsGrid.appendChild(customLikeBtn);
      likeBox.appendChild(likeReasonsGrid);

      const likeSkipBtn = document.createElement("button");
      likeSkipBtn.type = "button";
      likeSkipBtn.className = "text-center text-[10px] text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 pt-0.5";
      likeSkipBtn.textContent = "Mark Perfect without reason";
      likeSkipBtn.onclick = () => love(a, null);
      likeBox.appendChild(likeSkipBtn);

      // 3. Dislike Reason Drawer
      const reasonBox = document.createElement("div");
      reasonBox.className = "hidden pt-2 flex flex-col gap-1.5";

      const reasonHeader = document.createElement("div");
      reasonHeader.className = "flex items-center justify-between text-[11px] font-semibold text-rose-700 dark:text-rose-400 mb-0.5";
      reasonHeader.innerHTML = `<span>Why dislike?</span>`;

      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xs px-1";
      closeBtn.textContent = "✕";
      closeBtn.onclick = () => {
        reasonBox.classList.add("hidden");
        mainActions.classList.remove("hidden");
      };
      reasonHeader.appendChild(closeBtn);
      reasonBox.appendChild(reasonHeader);

      const reasonsGrid = document.createElement("div");
      reasonsGrid.className = "grid grid-cols-2 gap-1";

      for (let i = 0; i < REASONS.length; i++) {
        const rText = REASONS[i];
        const rb = document.createElement("button");
        rb.type = "button";
        rb.className = "text-left text-[11px] py-1 px-1.5 rounded-lg bg-rose-50/70 dark:bg-rose-950/40 text-rose-800 dark:text-rose-300 border border-rose-200/70 dark:border-rose-900/60 hover:bg-rose-100 dark:hover:bg-rose-900/60 transition truncate flex items-center gap-1.5 cursor-pointer";
        rb.title = rText;
        rb.innerHTML = `<span class="w-3.5 h-3.5 rounded bg-rose-200/80 dark:bg-rose-900/80 text-rose-800 dark:text-rose-200 text-[9px] font-bold flex items-center justify-center shrink-0">${i + 1}</span><span class="truncate">${rText}</span>`;
        rb.onclick = () => dislike(a, rText, c);
        reasonsGrid.appendChild(rb);
      }
      const customDisBtn = document.createElement("button");
      customDisBtn.type = "button";
      customDisBtn.className = "col-span-2 text-left text-[11px] py-1 px-1.5 rounded-lg bg-rose-50/70 dark:bg-rose-950/40 text-rose-800 dark:text-rose-300 border border-dashed border-rose-300/70 dark:border-rose-900/60 hover:bg-rose-100 dark:hover:bg-rose-900/60 transition truncate flex items-center gap-1.5 cursor-pointer";
      customDisBtn.innerHTML = `<span class="w-3.5 h-3.5 rounded bg-rose-200/80 dark:bg-rose-900/80 text-rose-800 dark:text-rose-200 text-[9px] font-bold flex items-center justify-center shrink-0">5</span><span class="truncate">Custom note...</span>`;
      customDisBtn.onclick = () => {
        const note = prompt("Enter custom dislike reason for @" + a.name + ":");
        if (note !== null && note.trim()) dislike(a, note.trim(), c);
      };
      reasonsGrid.appendChild(customDisBtn);
      reasonBox.appendChild(reasonsGrid);

      const skipBtn = document.createElement("button");
      skipBtn.type = "button";
      skipBtn.className = "text-center text-[10px] text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 pt-0.5";
      skipBtn.textContent = "Skip reason / Dislike anyway";
      skipBtn.onclick = () => dislike(a, null, c);
      reasonBox.appendChild(skipBtn);

      lTag.onclick = () => {
        mainActions.classList.add("hidden");
        likeBox.classList.remove("hidden");
      };

      d.onclick = () => {
        mainActions.classList.add("hidden");
        reasonBox.classList.remove("hidden");
      };

      mainActions.append(lWrap, d);
      actionsWrap.append(mainActions, likeBox, reasonBox);
      c.appendChild(actionsWrap);
    }

    grid.appendChild(c);
  }

  $("count").textContent = list.length === AVATARS.length
    ? AVATARS.length + " avatars"
    : list.length + " of " + AVATARS.length + " avatars";
  $("empty").hidden = list.length > 0;
}

$("nav").hidden = !DISCOVER;
$("nav").href = "/discover?t=" + encodeURIComponent(TOKEN);
$("hint").hidden = !REVIEW;

for (const id of ["q", "sort", "show", "story"]) {
  $(id).addEventListener(id === "q" ? "input" : "change", render);
}

$("size").addEventListener("input", e => {
  document.documentElement.style.setProperty("--card-size", e.target.value + "px");
});

showStats();
render();
</script>
</body>
</html>
"""

DISCOVER_TEMPLATE = """<!doctype html>
<html lang="en" class="h-full">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher - Discover</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'media',
    theme: {
      extend: {
        fontFamily: {
          sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        }
      }
    }
  }
</script>
</head>
<body class="h-full bg-slate-100 dark:bg-[#11141d] text-slate-900 dark:text-slate-100 font-sans antialiased flex flex-col min-h-screen">

<!-- Sticky Header -->
<header class="sticky top-0 z-30 bg-white/95 dark:bg-[#1c2230]/95 backdrop-blur-md border-b border-slate-200 dark:border-[#2a3441] transition-colors">
  <div class="max-w-4xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3">
    <div class="flex items-center gap-3">
      <div class="flex items-center gap-2">
        <span class="w-8 h-8 rounded-xl bg-indigo-600 text-white flex items-center justify-center font-bold text-sm shadow-sm shadow-indigo-500/20">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        </span>
        <h1 class="text-base sm:text-lg font-bold tracking-tight">Discover</h1>
      </div>
      <span id="stats" class="text-xs text-slate-500 dark:text-slate-400"></span>
    </div>
    <a id="nav" href="#" class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-xl bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700 transition">
      <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
      Saved Catalog
    </a>
  </div>
</header>

<!-- Main Stage Container -->
<main class="flex-1 max-w-md w-full mx-auto px-4 py-6 flex flex-col justify-center">
  <!-- Candidate Card -->
  <div class="bg-white dark:bg-[#1c2230] border border-slate-200 dark:border-[#2a3441] rounded-3xl overflow-hidden shadow-sm">
    <div id="stage" class="aspect-square w-full bg-slate-50 dark:bg-[#161b26] flex items-center justify-center p-6 text-sm text-slate-400 font-medium">
      Finding avatars...
    </div>
    <div class="p-3 text-center border-t border-slate-100 dark:border-[#2a3441]/60">
      <div id="name" class="font-bold text-base sm:text-lg text-slate-800 dark:text-slate-100 tracking-tight">&nbsp;</div>
    </div>
  </div>

  <!-- Primary Action Controls: 1 Dislike | 2 Skip | 3 Like -->
  <div class="grid grid-cols-3 gap-2.5 mt-4">
    <!-- 1: Dislike -->
    <button id="dis" class="relative flex flex-col items-center justify-center py-2.5 px-2 rounded-2xl bg-rose-50 text-rose-700 hover:bg-rose-100 border border-rose-200 dark:bg-rose-950/40 dark:text-rose-300 dark:border-rose-900 transition cursor-pointer group shadow-xs active:scale-95">
      <div class="flex items-center gap-1.5 font-semibold text-sm">
        <span class="w-4 h-4 rounded-md bg-rose-200/80 dark:bg-rose-900/80 text-rose-800 dark:text-rose-200 text-[10px] font-bold flex items-center justify-center shrink-0">1</span>
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14H5.236a2 2 0 01-1.789-2.894l3.5-7A2 2 0 018.736 3h7.528a2 2 0 011.789 1.106l1.5 3A2 2 0 0119.553 9H16v5m-6 0v7m0-7l4 4m2-11h4a2 2 0 012 2v5a2 2 0 01-2 2h-4"/></svg>
        <span>Dislike</span>
      </div>
      <span class="text-[10px] text-rose-500/80 dark:text-rose-400/80 font-normal mt-0.5">Key 1 (or ← / A)</span>
    </button>

    <!-- 2: Skip -->
    <button id="skip" class="relative flex flex-col items-center justify-center py-2.5 px-2 rounded-2xl bg-slate-100 text-slate-700 hover:bg-slate-200 border border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700 transition cursor-pointer group shadow-xs active:scale-95">
      <div class="flex items-center gap-1.5 font-semibold text-sm">
        <span class="w-4 h-4 rounded-md bg-slate-200/80 dark:bg-slate-700 text-slate-700 dark:text-slate-300 text-[10px] font-bold flex items-center justify-center shrink-0">2</span>
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 5l7 7-7 7M5 5l7 7-7 7"/></svg>
        <span>Skip</span>
      </div>
      <span class="text-[10px] text-slate-500 dark:text-slate-400 font-normal mt-0.5">Key 2 (or ↓ / S)</span>
    </button>

    <!-- 3: Perfect / Like -->
    <button id="love" class="relative flex flex-col items-center justify-center py-2.5 px-2 rounded-2xl bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm shadow-emerald-600/20 transition cursor-pointer group active:scale-95">
      <div class="flex items-center gap-1.5 font-semibold text-sm">
        <span class="w-4 h-4 rounded-md bg-emerald-700/80 text-white text-[10px] font-bold flex items-center justify-center shrink-0">3</span>
        <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
        <span>Like</span>
      </div>
      <span class="text-[10px] text-emerald-100 font-normal mt-0.5">Key 3 (or → / D)</span>
    </button>
  </div>

  <!-- Dynamic Reason Drawer (Appears when Dislike [1] or Like [3] is selected) -->
  <div id="reason-panel" class="hidden mt-3 p-3.5 rounded-2xl bg-white dark:bg-[#1c2230] border shadow-xs transition-all duration-150">
    <div class="flex items-center justify-between mb-2">
      <div class="flex items-center gap-2">
        <span id="reason-title" class="text-xs font-bold tracking-tight"></span>
        <span class="text-[10px] text-slate-400 dark:text-slate-500">Pick 1–4, 5 for custom note, Space to skip</span>
      </div>
      <button id="reason-cancel-btn" type="button" class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xs px-1 cursor-pointer" title="Cancel (Esc)">✕</button>
    </div>
    <div id="reason-buttons" class="grid grid-cols-2 gap-1.5"></div>
    <form id="custom-form" class="hidden mt-2.5 flex gap-1.5" onsubmit="event.preventDefault(); submitCustomNote();">
      <input id="custom-input" type="text" maxlength="80" placeholder="Type custom note and press Enter..." class="flex-1 px-3 py-1.5 text-xs rounded-xl bg-white dark:bg-[#161b26] border border-slate-300 dark:border-[#2a3441] text-slate-800 dark:text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500" />
      <button type="submit" class="px-3 py-1.5 text-xs font-semibold rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white transition cursor-pointer">Save</button>
      <button id="custom-cancel" type="button" class="px-2.5 py-1.5 text-xs font-medium rounded-xl bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700 transition cursor-pointer">Cancel</button>
    </form>
    <div class="flex items-center justify-between gap-2 mt-2.5 pt-2 border-t border-slate-100 dark:border-[#2a3441]/60">
      <button id="reason-skip-btn" type="button" class="text-xs font-medium text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100 hover:underline cursor-pointer flex items-center gap-1">
        <span>Continue without reason</span>
        <span class="text-[10px] px-1 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400">Space</span>
      </button>
      <button id="reason-close-text" type="button" class="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 cursor-pointer">Cancel (Esc)</button>
    </div>
  </div>

  <!-- Status & Feedback Area -->
  <div id="bar" role="status" class="mt-3 min-h-[36px] flex items-center justify-center gap-2 text-xs text-center text-slate-600 dark:text-slate-300"></div>

  <!-- Shortcuts Bar -->
  <div class="mt-2 text-center text-[11px] text-slate-400 dark:text-slate-500">
    <span>Keys: <strong>1</strong> Dislike &bull; <strong>2</strong> Skip &bull; <strong>3</strong> Like &bull; Then <strong>1-4</strong> or <strong>5</strong> Custom &bull; <strong>Z</strong> Undo</span>
  </div>
</main>

<script>
const REASONS = __REASONS__, LIKE_REASONS = __LIKE_REASONS__;
let STATS = __STATS__;
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id);
let queue = [], current = null, last = null, exhausted = false, fetching = false, busy = false, me = { liked: 0, disliked: 0 }, barTimer;
let reasonMode = null; // null | "dislike" | "like"

$("nav").href = "/?t=" + encodeURIComponent(TOKEN);

function openCustomInput() {
  const form = $("custom-form");
  const input = $("custom-input");
  form.classList.remove("hidden");
  input.value = "";
  input.focus();
}

function closeCustomInput() {
  const form = $("custom-form");
  if (form) form.classList.add("hidden");
  const input = $("custom-input");
  if (input) input.value = "";
}

function submitCustomNote() {
  const input = $("custom-input");
  const note = input ? input.value.trim() : "";
  if (reasonMode) {
    submitRating(reasonMode, note || null);
  }
  closeCustomInput();
}

function openReasonDrawer(mode) {
  if (!current || busy) return;
  reasonMode = mode;
  closeCustomInput();
  const panel = $("reason-panel");
  const title = $("reason-title");
  const grid = $("reason-buttons");
  const skipBtn = $("reason-skip-btn");
  grid.textContent = "";

  if (mode === "dislike") {
    panel.className = "mt-3 p-3.5 rounded-2xl bg-rose-50/40 dark:bg-rose-950/20 border border-rose-200 dark:border-rose-900/60 shadow-xs transition-all duration-150";
    title.className = "text-xs font-bold text-rose-700 dark:text-rose-400";
    title.textContent = "Why Dislike?";
    skipBtn.innerHTML = `<span>Dislike without reason</span><span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-100 dark:bg-rose-900/50 text-rose-700 dark:text-rose-300 font-semibold">Space</span>`;

    for (let i = 0; i < REASONS.length; i++) {
      const rText = REASONS[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "inline-flex items-center gap-1.5 py-2 px-2.5 text-xs font-medium rounded-xl bg-white dark:bg-[#161b26] hover:bg-rose-50 dark:hover:bg-rose-950/40 text-slate-800 dark:text-slate-200 hover:text-rose-700 dark:hover:text-rose-300 border border-rose-100 dark:border-rose-900/40 hover:border-rose-300 dark:hover:border-rose-800 transition group text-left cursor-pointer shadow-xs";
      btn.innerHTML = `<span class="w-4 h-4 rounded-md bg-rose-100 dark:bg-rose-900/80 group-hover:bg-rose-200 text-rose-800 dark:text-rose-200 text-[10px] font-bold flex items-center justify-center shrink-0">${i + 1}</span><span class="truncate">${rText}</span>`;
      btn.onclick = () => submitRating("dislike", rText);
      grid.appendChild(btn);
    }
    const customBtn = document.createElement("button");
    customBtn.type = "button";
    customBtn.className = "col-span-2 inline-flex items-center gap-1.5 py-2 px-2.5 text-xs font-medium rounded-xl bg-white dark:bg-[#161b26] hover:bg-rose-50 dark:hover:bg-rose-950/40 text-slate-800 dark:text-slate-200 hover:text-rose-700 dark:hover:text-rose-300 border border-dashed border-rose-300/80 dark:border-rose-900/60 transition group text-left cursor-pointer shadow-xs";
    customBtn.innerHTML = `<span class="w-4 h-4 rounded-md bg-rose-100 dark:bg-rose-900/80 text-rose-800 dark:text-rose-200 text-[10px] font-bold flex items-center justify-center shrink-0">5</span><span class="truncate">Custom note...</span>`;
    customBtn.onclick = openCustomInput;
    grid.appendChild(customBtn);
  } else {
    panel.className = "mt-3 p-3.5 rounded-2xl bg-emerald-50/40 dark:bg-emerald-950/20 border border-emerald-200 dark:border-emerald-900/60 shadow-xs transition-all duration-150";
    title.className = "text-xs font-bold text-emerald-700 dark:text-emerald-400";
    title.textContent = "Why Like?";
    skipBtn.innerHTML = `<span>Like without reason</span><span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300 font-semibold">Space</span>`;

    for (let i = 0; i < LIKE_REASONS.length; i++) {
      const rText = LIKE_REASONS[i];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "inline-flex items-center gap-1.5 py-2 px-2.5 text-xs font-medium rounded-xl bg-white dark:bg-[#161b26] hover:bg-emerald-50 dark:hover:bg-emerald-950/40 text-slate-800 dark:text-slate-200 hover:text-emerald-700 dark:hover:text-emerald-300 border border-emerald-100 dark:border-emerald-900/40 hover:border-emerald-300 dark:hover:border-emerald-800 transition group text-left cursor-pointer shadow-xs";
      btn.innerHTML = `<span class="w-4 h-4 rounded-md bg-emerald-100 dark:bg-emerald-900/80 group-hover:bg-emerald-200 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold flex items-center justify-center shrink-0">${i + 1}</span><span class="truncate">${rText}</span>`;
      btn.onclick = () => submitRating("like", rText);
      grid.appendChild(btn);
    }
    const customBtn = document.createElement("button");
    customBtn.type = "button";
    customBtn.className = "col-span-2 inline-flex items-center gap-1.5 py-2 px-2.5 text-xs font-medium rounded-xl bg-white dark:bg-[#161b26] hover:bg-emerald-50 dark:hover:bg-emerald-950/40 text-slate-800 dark:text-slate-200 hover:text-emerald-700 dark:hover:text-emerald-300 border border-dashed border-emerald-300/80 dark:border-emerald-900/60 transition group text-left cursor-pointer shadow-xs";
    customBtn.innerHTML = `<span class="w-4 h-4 rounded-md bg-emerald-100 dark:bg-emerald-900/80 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold flex items-center justify-center shrink-0">5</span><span class="truncate">Custom note...</span>`;
    customBtn.onclick = openCustomInput;
    grid.appendChild(customBtn);
  }
  panel.classList.remove("hidden");
}

function closeReasonDrawer() {
  reasonMode = null;
  closeCustomInput();
  const panel = $("reason-panel");
  if (panel) panel.classList.add("hidden");
}

$("reason-cancel-btn").onclick = closeReasonDrawer;
$("reason-close-text").onclick = closeReasonDrawer;
$("custom-cancel").onclick = closeCustomInput;
$("reason-skip-btn").onclick = () => {
  if (reasonMode) submitRating(reasonMode, null);
};

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function bar(text, buttons, ms) {
  const b = $("bar");
  b.textContent = text + " ";
  for (const [label, fn] of buttons || []) {
    const x = document.createElement("button");
    x.className = "ml-1.5 px-2.5 py-1 text-xs font-semibold rounded-lg bg-slate-200 dark:bg-slate-700 text-slate-800 dark:text-slate-100 hover:bg-slate-300 dark:hover:bg-slate-600 transition cursor-pointer";
    x.textContent = label;
    x.onclick = fn;
    b.appendChild(x);
  }
  clearTimeout(barTimer);
  if (ms) barTimer = setTimeout(() => b.textContent = "", ms);
}

function showStats() {
  const t = STATS.liked + STATS.disliked;
  $("stats").textContent = "Session: " + (me.liked + me.disliked) + " rated" + (t ? " \u2022 All time: " + STATS.liked + " perfect, " + STATS.disliked + " disliked (" + Math.round(100 * STATS.liked / t) + "% kept)" : "");
}

async function refill() {
  if (fetching || exhausted || queue.length >= 6) return;
  fetching = true;
  let got = 0;
  try {
    const r = await api("/api/next?n=6");
    queue.push(...r.names);
    got = r.names.length;
    exhausted = r.done;
    if (!current) advance();
  } catch (e) {
    bar("Lost connection - is window open?");
  }
  fetching = false;
  if (!exhausted && queue.length < 6) setTimeout(refill, got ? 150 : 1200);
}

function advance() {
  closeReasonDrawer();
  current = queue.shift() || null;
  render();
  for (const n of queue.slice(0, 3)) {
    const i = new Image();
    i.src = "/candidate/" + n + ".png";
  }
  if (queue.length < 4) refill();
}

function render() {
  const stage = $("stage");
  stage.textContent = "";
  if (current) {
    const i = document.createElement("img");
    i.src = "/candidate/" + current + ".png";
    i.alt = current;
    i.className = "w-full h-full object-contain";
    stage.appendChild(i);
    $("name").textContent = "@" + current;
  } else {
    stage.textContent = exhausted ? "No more new avatars from these groups." : "Finding avatars...";
    $("name").innerHTML = "&nbsp;";
    closeReasonDrawer();
  }
  showStats();
}

async function submitRating(action, reason) {
  if (!current || busy) return;
  busy = true;
  const name = current;
  closeReasonDrawer();
  try {
    const r = await api("/api/discover-rate", { name, action, reason });
    STATS = r.stats;
    last = { name, action, reason, time: Date.now() };
    const reasonTag = reason ? " (" + reason + ")" : "";
    if (action === "like") {
      me.liked++;
      const msg = r.info.saved
        ? "Saved @" + name + " to catalog!" + reasonTag
        : "@" + name + " (" + (r.info.followers == null ? "unknown" : r.info.followers.toLocaleString()) + " followers)" + reasonTag;
      bar(msg, [["Undo (Z)", undo]], 4000);
    } else {
      me.disliked++;
      bar("Disliked @" + name + reasonTag, [["Undo (Z)", undo]], 5000);
    }
    advance();
  } catch (e) {
    bar("Couldn't save rating");
  }
  busy = false;
}

function skip() {
  closeReasonDrawer();
  if (current) {
    last = null;
    advance();
  }
}

async function undo() {
  if (!last || busy) return;
  busy = true;
  closeReasonDrawer();
  const l = last;
  try {
    const r = await api("/api/discover-rate", { name: l.name, action: "undo" });
    STATS = r.stats;
    if (l.action === "like") me.liked--;
    else me.disliked--;
    if (current) queue.unshift(current);
    current = l.name;
    last = null;
    render();
    bar("Undone.", [], 1500);
  } catch (e) {
    bar("Couldn't undo");
  }
  busy = false;
}

$("dis").onclick = () => openReasonDrawer("dislike");
$("love").onclick = () => openReasonDrawer("like");
$("skip").onclick = skip;

document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();

  // If currently typing in custom note input, allow typing and handle Esc:
  if (e.target && e.target.tagName === "INPUT") {
    if (e.key === "Escape") {
      closeCustomInput();
    }
    return;
  }

  // Undo is always accessible
  if (k === "z" || (k === "backspace" && reasonMode === null)) {
    undo();
    return;
  }

  // When Reason Drawer is open:
  if (reasonMode) {
    if (k === "escape") {
      closeReasonDrawer();
      return;
    }
    if (k === " " || k === "enter") {
      e.preventDefault();
      submitRating(reasonMode, null);
      return;
    }
    if (k >= "1" && k <= "4") {
      e.preventDefault();
      const idx = Number(k) - 1;
      const list = reasonMode === "dislike" ? REASONS : LIKE_REASONS;
      if (idx < list.length) {
        submitRating(reasonMode, list[idx]);
      }
      return;
    }
    if (k === "5") {
      e.preventDefault();
      openCustomInput();
      return;
    }
    return;
  }

  // In Normal Ready Mode:
  if (k === "1" || k === "arrowleft" || k === "a") {
    openReasonDrawer("dislike");
  } else if (k === "2" || k === "arrowdown" || k === "s" || k === " ") {
    e.preventDefault();
    skip();
  } else if (k === "3" || k === "arrowright" || k === "d") {
    openReasonDrawer("like");
  } else if (k === "backspace") {
    undo();
  }
});

showStats();
refill();
</script>
</body>
</html>
"""


def gallery_html(review, prefix="", discover=False):
    feedback = load_feedback()
    metadata = load_metadata()
    avatars = []
    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.glob("*.png"):
            m = metadata.get(path.stem, {})
            fb_entry = feedback.get(path.stem, {})
            avatars.append({
                "name": path.stem,
                "file": prefix + "avatars/" + quote(path.name),
                "time": int(path.stat().st_mtime),
                "liked": fb_entry.get("verdict") == "liked",
                "reason": fb_entry.get("reason", ""),
                "score": m.get("score"),
                "story": m.get("color_story", ""),
                "colors": m.get("dominant_colors", []),
                "clutter": m.get("clutter", False),
                "funny": m.get("funny", False),
            })

    def js(value):
        return json.dumps(value).replace("</", "<\\/")

    return (GALLERY_TEMPLATE.replace("__REASONS__", js(DISLIKE_REASONS))
            .replace("__LIKE_REASONS__", js(LIKE_REASONS))
            .replace("__STATS__", js(feedback_stats(feedback)))
            .replace("__REVIEW__", "true" if review else "false")
            .replace("__DISCOVER__", "true" if discover else "false")
            .replace("__DATA__", js(avatars)))


def build_gallery():
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    html = gallery_html(review=False)
    GALLERY_PATH.write_text(html, encoding="utf-8")
    return len(list(OUTPUT_DIR.glob("*.png"))) if OUTPUT_DIR.exists() else 0


def discover_html():
    return (DISCOVER_TEMPLATE.replace("__REASONS__", json.dumps(DISLIKE_REASONS))
            .replace("__LIKE_REASONS__", json.dumps(LIKE_REASONS))
            .replace("__STATS__", json.dumps(feedback_stats())))


def make_review_server(feed=None, session=None, max_followers=DEFAULT_SETTINGS["max_followers"]):
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


def open_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    if not open_path(path):
        print(f"Couldn't open it automatically. Your avatars are in:\n  {path}")


def show_gallery(start="/", feed=None, session=None, max_followers=DEFAULT_SETTINGS["max_followers"]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server, token = make_review_server(feed, session, max_followers)
    except OSError:
        count = build_gallery()
        print(f"Gallery updated: {count} avatars (view only - couldn't start review server).")
        if not open_path(GALLERY_PATH):
            print(f"Open this file in your browser:\n  {GALLERY_PATH}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}{start}?t={token}"
    if feed is not None:
        feed.start()
    print("Opening the page in your browser...")
    if not webbrowser.open(url):
        print(f"Couldn't open browser automatically. Open this URL:\n  {url}")
    if start == "/discover":
        print("\nRate avatars with arrow keys: left = dislike, right = perfect, down = skip, Z = undo.")
    else:
        print("\nOn the page: click an avatar to copy its username, mark Perfect,")
        print("or Dislike to teach the AI. Use color story and theme filters to explore.")
    try:
        input("\nPress Enter here when finished (this closes the page server)... ")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if feed is not None:
            feed.stop.set()
        server.shutdown()
        server.server_close()


def show_discovery(terms, session, max_followers):
    print("Loading fresh avatars from: " + ", ".join(terms))
    show_gallery("/discover", CandidateFeed(session, terms), session, max_followers)
