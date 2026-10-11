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
    OUTPUT_DIR,
    load_metadata,
)
from skin_searcher.pipeline import CandidateFeed
from skin_searcher.taste import feedback_stats, load_feedback, rate_avatar, rate_candidate

GALLERY_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher</title>
<style>
:root{--bg:#f1f3f6;--card:#ffffff;--ink:#0f172a;--muted:#475569;--line:#e2e8f0;--accent:#4f46e5;--stage:#f8fafc;--good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--tag-bg:#e2e8f0;--tag-ink:#334155;--size:210px}
@media (prefers-color-scheme:dark){:root{--bg:#11141d;--card:#1c2230;--ink:#f8fafc;--muted:#94a3b8;--line:#2a3441;--accent:#818cf8;--stage:#161b26;--good:#4ade80;--good-bg:#064e3b;--bad:#f87171;--bad-bg:#7f1d1d;--tag-bg:#262f40;--tag-ink:#cbd5e1}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:17px;font-weight:700;margin:0;letter-spacing:-0.01em}
.count,.stats{color:var(--muted);font-size:13px}
.tools{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin-left:auto}
input[type=search],select{font:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);outline:none}
input[type=search]:focus,select:focus{border-color:var(--accent)}
input[type=search]{width:190px}
label{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:13px}
.hint{padding:8px 20px;color:var(--muted);font-size:13px;background:var(--card);border-bottom:1px solid var(--line)}
main{padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--size),1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;transition:transform 0.1s ease,box-shadow 0.1s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 20px rgba(0,0,0,0.06)}
.card.liked{border-color:var(--good);box-shadow:0 0 0 1.5px var(--good)}
.copy{display:block;width:100%;border:0;background:none;padding:0;text-align:left;color:inherit;font:inherit;cursor:pointer}
.copy:hover .name{color:var(--accent)}
.stage{background:var(--stage);aspect-ratio:1/1;position:relative}
.stage img{width:100%;height:100%;object-fit:contain;display:block}
.badges{position:absolute;top:6px;left:6px;right:6px;display:flex;gap:4px;flex-wrap:wrap;pointer-events:none}
.badge{font-size:10px;font-weight:600;padding:2px 6px;border-radius:6px;background:var(--card);color:var(--ink);border:1px solid var(--line);backdrop-filter:blur(6px);opacity:0.92;text-transform:capitalize}
.badge.score{background:var(--accent);color:#fff;border:none}
.name{padding:8px 10px 4px;font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta-info{padding:0 10px 6px;color:var(--muted);font-size:11px;display:flex;gap:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.actions{display:flex;gap:6px;padding:0 8px 8px;margin-top:auto}
.act{flex:1;font:inherit;font-size:12px;font-weight:500;padding:6px 4px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);cursor:pointer}
.act.love:hover,.card.liked .act.love{background:var(--good-bg);border-color:var(--good);color:var(--good)}
.act.dis:hover{background:var(--bad-bg);border-color:var(--bad);color:var(--bad)}
.empty{color:var(--muted);text-align:center;padding:60px 20px;font-size:15px}
#bar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);max-width:calc(100vw - 24px);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:14px;display:none;gap:8px 10px;align-items:center;flex-wrap:wrap;justify-content:center;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.25);z-index:100}
#bar.show{display:flex}
#bar button{font:inherit;font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid currentColor;background:transparent;color:inherit;cursor:pointer}
#bar button:hover{background:var(--bg);color:var(--ink)}
</style></head><body>
<header>
<h1>Skin Searcher</h1><span class="count" id="count"></span><span class="stats" id="stats"></span>
<a id="nav" href="#" hidden style="color:var(--accent);text-decoration:none;font-size:13px">Discover new avatars</a>
<div class="tools">
<input type="search" id="q" placeholder="Search name, color, theme" aria-label="Search">
<select id="story" aria-label="Style"><option value="all">All Styles</option><option value="monochrome">Monochrome</option><option value="color_blocking">Color Blocking</option><option value="rainbow">Rainbow</option><option value="neon">Neon</option><option value="pastel">Pastel</option><option value="earthy">Earthy</option><option value="plain">Plain</option></select>
<select id="show" aria-label="Show"><option value="all">Show all</option><option value="new">Not rated yet</option><option value="liked">Perfect only</option></select>
<select id="sort" aria-label="Sort"><option value="new">Newest first</option><option value="score">Score</option><option value="az">A to Z</option></select>
<label>Size <input type="range" id="size" min="120" max="380" value="210"></label>
</div></header>
<div class="hint" id="hint"></div>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden></div></main>
<div id="bar" role="status"></div>
<script>
const REVIEW=__REVIEW__, REASONS=__REASONS__, DISCOVER=__DISCOVER__;
let AVATARS=__DATA__, STATS=__STATS__;
const TOKEN=new URLSearchParams(location.search).get("t")||"";
const $=id=>document.getElementById(id), grid=$("grid"), bar=$("bar");
let barTimer;
function say(text,buttons,ms){
  bar.textContent="";const t=document.createElement("span");t.textContent=text;bar.appendChild(t);
  for(const [label,fn] of buttons||[]){const b=document.createElement("button");b.textContent=label;b.onclick=fn;bar.appendChild(b)}
  bar.classList.add("show");clearTimeout(barTimer);barTimer=setTimeout(()=>bar.classList.remove("show"),ms||2000)}
function copy(name){
  const done=()=>say("Copied "+name);
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(name).then(done,()=>fallback(name,done))}else fallback(name,done)}
function fallback(name,done){const t=document.createElement("textarea");t.value=name;t.style.position="fixed";t.style.opacity="0";document.body.appendChild(t);t.select();
  try{document.execCommand("copy");done()}catch(e){say("Couldn't copy - select it by hand: "+name)}t.remove()}
async function rate(name,action,reason){
  const r=await fetch("/api/rate",{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify({name,action,reason})});
  if(!r.ok)throw new Error(await r.text());
  STATS=(await r.json()).stats;showStats()}
function showStats(){
  const total=STATS.liked+STATS.disliked;
  $("stats").textContent=total?("Rated "+total+": "+STATS.liked+" perfect, "+STATS.disliked+" disliked ("+Math.round(100*STATS.liked/total)+"% kept)"):""}
async function love(a){
  try{await rate(a.name,a.liked?"clear":"like");a.liked=!a.liked;render();say(a.liked?a.name+" marked perfect - AI learned!":"Rating removed")}
  catch(e){say("Couldn't save - is window open?",[],3000)}}
async function dislike(a){
  try{await rate(a.name,"dislike")}catch(e){say("Couldn't save",[],3000);return}
  AVATARS=AVATARS.filter(x=>x!==a);render();
  const buttons=[["Undo",async()=>{try{await rate(a.name,"clear");a.liked=false;AVATARS.push(a);render();say("Restored "+a.name)}catch(e){say("Couldn't undo",[],3000)}}]];
  for(const reason of REASONS)buttons.push([reason,async()=>{try{await rate(a.name,"reason",reason);say("Noted: "+reason)}catch(e){}}]);
  say("Removed "+a.name+". Why?",buttons,8000)}
function render(){
  const q=$("q").value.trim().toLowerCase(),sort=$("sort").value,show=$("show").value,story=$("story").value;
  const list=AVATARS.filter(a=>{
    const matchQ=!q||a.name.toLowerCase().includes(q)||(a.theme&&a.theme.toLowerCase().includes(q))||(a.colors&&a.colors.some(c=>c.toLowerCase().includes(q)));
    const matchShow=(show==="all"||(show==="liked"?a.liked:!a.liked));
    const matchStory=(story==="all"||(a.story&&a.story.toLowerCase()===story.toLowerCase()));
    return matchQ&&matchShow&&matchStory;
  });
  if(sort==="az")list.sort((a,b)=>a.name.localeCompare(b.name));
  else if(sort==="score")list.sort((a,b)=>(b.score||0)-(a.score||0));
  else list.sort((a,b)=>b.time-a.time);
  grid.textContent="";
  for(const a of list){
    const c=document.createElement("div");c.className="card"+(a.liked?" liked":"");
    const b=document.createElement("button");b.className="copy";b.title="Click to copy username";
    const s=document.createElement("div");s.className="stage";
    const i=document.createElement("img");i.loading="lazy";i.alt=a.name;i.src=a.file;s.appendChild(i);
    const badges=document.createElement("div");badges.className="badges";
    if(a.score) { const sb=document.createElement("span");sb.className="badge score";sb.textContent=a.score+"/10";badges.appendChild(sb); }
    if(a.story&&a.story!=="plain") { const st=document.createElement("span");st.className="badge";st.textContent=a.story.replace("_"," ");badges.appendChild(st); }
    if(a.theme) { const tb=document.createElement("span");tb.className="badge";tb.textContent=a.theme;badges.appendChild(tb); }
    s.appendChild(badges);
    const n=document.createElement("div");n.className="name";n.textContent=a.name;
    const mi=document.createElement("div");mi.className="meta-info";
    if(a.colors&&a.colors.length) mi.textContent=a.colors.join(", ");
    b.append(s,n,mi);b.addEventListener("click",()=>copy(a.name));c.appendChild(b);
    if(REVIEW){
      const r=document.createElement("div");r.className="actions";
      const l=document.createElement("button");l.className="act love";l.textContent=a.liked?"Perfect \\u2713":"Perfect";l.onclick=()=>love(a);
      const d=document.createElement("button");d.className="act dis";d.textContent="Dislike";d.onclick=()=>dislike(a);
      r.append(l,d);c.appendChild(r)}
    grid.appendChild(c)}
  $("count").textContent=list.length===AVATARS.length?AVATARS.length+" avatars":list.length+" of "+AVATARS.length+" avatars";
  $("empty").hidden=list.length>0;
  $("empty").textContent=AVATARS.length?"No matching avatars.":"No avatars yet. Run a search to populate.";}
$("nav").hidden=!DISCOVER;$("nav").href="/discover?t="+encodeURIComponent(TOKEN);
$("hint").hidden=!REVIEW;
$("hint").textContent="Click an avatar to copy its username. Use filters above to sort by color story (Monochrome, Neon, etc.) or theme.";
for(const id of ["q","sort","show","story"])$(id).addEventListener(id==="q"?"input":"change",render);
$("size").addEventListener("input",e=>document.documentElement.style.setProperty("--size",e.target.value+"px"));
showStats();render();
</script></body></html>
"""

DISCOVER_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skin Searcher - Discover</title>
<style>
:root{--bg:#f1f3f6;--card:#ffffff;--ink:#0f172a;--muted:#475569;--line:#e2e8f0;--accent:#4f46e5;--stage:#f8fafc;--good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2}
@media (prefers-color-scheme:dark){:root{--bg:#11141d;--card:#1c2230;--ink:#f8fafc;--muted:#94a3b8;--line:#2a3441;--accent:#818cf8;--stage:#161b26;--good:#4ade80;--good-bg:#064e3b;--bad:#f87171;--bad-bg:#7f1d1d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:17px;font-weight:700;margin:0}.muted{color:var(--muted);font-size:13px}
header a{margin-left:auto;color:var(--accent);text-decoration:none;font-size:13px;font-weight:500}
main{max-width:480px;margin:20px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:0 6px 18px rgba(0,0,0,0.04)}
.stage{background:var(--stage);aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;color:var(--muted)}
.stage img{width:100%;height:100%;object-fit:contain;display:block}
.name{padding:12px 14px;font-weight:600;font-size:16px;text-align:center}
.row{display:flex;gap:10px;margin-top:14px}
.btn{flex:1;font:inherit;font-weight:500;padding:12px 8px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);cursor:pointer;font-size:14px}
.btn small{display:block;color:var(--muted);font-size:11px;font-weight:normal;margin-top:2px}
.btn.dis:hover{background:var(--bad-bg);border-color:var(--bad);color:var(--bad)}.btn.love:hover{background:var(--good-bg);border-color:var(--good);color:var(--good)}
.btn:hover small{color:inherit}.btn.skip{flex:.6}
.hint{color:var(--muted);font-size:12.5px;text-align:center;margin-top:14px;line-height:1.5}
#bar{margin-top:12px;min-height:32px;text-align:center;font-size:13px}
#bar button{font:inherit;font-size:12px;margin:2px 3px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
</style></head><body>
<header><h1>Discover</h1><span class="muted" id="stats"></span><a id="nav" href="#">Review saved avatars</a></header>
<main>
<div class="card"><div class="stage" id="stage">Finding avatars...</div><div class="name" id="name">&nbsp;</div></div>
<div class="row">
<button class="btn dis" id="dis">Dislike<small>Left arrow</small></button>
<button class="btn skip" id="skip">Skip<small>Down arrow</small></button>
<button class="btn love" id="love">Perfect<small>Right arrow</small></button>
</div>
<div id="bar" role="status"></div>
<div class="hint">Keys: left = dislike, right = perfect, down = skip, Z = undo, 1-3 = why you disliked it.<br>Perfect ones are saved to your catalog. Everything you rate teaches the AI.</div>
</main>
<script>
const REASONS=__REASONS__;let STATS=__STATS__;
const TOKEN=new URLSearchParams(location.search).get("t")||"";
const $=id=>document.getElementById(id);
let queue=[],current=null,last=null,exhausted=false,fetching=false,busy=false,me={liked:0,disliked:0},barTimer;
$("nav").href="/?t="+encodeURIComponent(TOKEN);
async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw new Error(await r.text());return r.json()}
function bar(text,buttons,ms){
  const b=$("bar");b.textContent=text+" ";
  for(const [label,fn] of buttons||[]){const x=document.createElement("button");x.textContent=label;x.onclick=fn;b.appendChild(x)}
  clearTimeout(barTimer);if(ms)barTimer=setTimeout(()=>b.textContent="",ms)}
function showStats(){
  const t=STATS.liked+STATS.disliked;
  $("stats").textContent="This session: "+(me.liked+me.disliked)+" rated"+(t?" | All time: "+STATS.liked+" perfect, "+STATS.disliked+" disliked ("+Math.round(100*STATS.liked/t)+"% kept)":"")}
async function refill(){
  if(fetching||exhausted||queue.length>=6)return;fetching=true;let got=0;
  try{const r=await api("/api/next?n=6");queue.push(...r.names);got=r.names.length;exhausted=r.done;if(!current)advance()}
  catch(e){bar("Lost connection - is window open?")}
  fetching=false;if(!exhausted&&queue.length<6)setTimeout(refill,got?150:1200)}
function advance(){
  current=queue.shift()||null;render();
  for(const n of queue.slice(0,3)){const i=new Image();i.src="/candidate/"+n+".png"}
  if(queue.length<4)refill()}
function render(){
  const stage=$("stage");stage.textContent="";
  if(current){const i=document.createElement("img");i.src="/candidate/"+current+".png";i.alt=current;stage.appendChild(i);$("name").textContent=current}
  else{stage.textContent=exhausted?"No more new avatars from these groups.":"Finding avatars...";$("name").innerHTML="&nbsp;"}
  showStats()}
async function rate(action){
  if(!current||busy)return;busy=true;const name=current;
  try{
    const r=await api("/api/discover-rate",{name,action});STATS=r.stats;last={name,action};
    if(action==="like"){me.liked++;bar(r.info.saved?"Saved "+name+" to catalog.":"Noted - "+name+" has "+(r.info.followers==null?"unknown":r.info.followers.toLocaleString())+" followers, so wasn't saved.",[],3500)}
    else{me.disliked++;bar("Why?",REASONS.map((x,i)=>[(i+1)+" "+x,()=>tag(i)]),6000)}
    advance()}
  catch(e){bar("Couldn't save - is window open?")}
  busy=false}
async function tag(i){
  if(!last||last.action!=="dislike")return;
  try{await api("/api/discover-rate",{name:last.name,action:"reason",reason:REASONS[i]});bar("Noted: "+REASONS[i],[],1800)}catch(e){}}
function skip(){if(current){last=null;advance()}}
async function undo(){
  if(!last||busy)return;busy=true;const l=last;
  try{const r=await api("/api/discover-rate",{name:l.name,action:"undo"});STATS=r.stats;
    if(l.action==="like")me.liked--;else me.disliked--;
    if(current)queue.unshift(current);current=l.name;last=null;render();bar("Undone.",[],1200)}
  catch(e){bar("Couldn't undo")}
  busy=false}
$("dis").onclick=()=>rate("dislike");$("love").onclick=()=>rate("like");$("skip").onclick=skip;
document.addEventListener("keydown",e=>{
  if(e.metaKey||e.ctrlKey||e.altKey)return;const k=e.key.toLowerCase();
  if(k==="arrowleft"||k==="a")rate("dislike");else if(k==="arrowright"||k==="d")rate("like");
  else if(k==="arrowdown"||k==="s"||k===" "){e.preventDefault();skip()}
  else if(k==="z"||k==="backspace")undo();else if(k>="1"&&k<="3")tag(Number(k)-1)});
showStats();refill();
</script></body></html>
"""


def gallery_html(review, prefix="", discover=False):
    feedback = load_feedback()
    metadata = load_metadata()
    avatars = []
    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.glob("*.png"):
            m = metadata.get(path.stem, {})
            avatars.append({
                "name": path.stem,
                "file": prefix + "avatars/" + quote(path.name),
                "time": int(path.stat().st_mtime),
                "liked": feedback.get(path.stem, {}).get("verdict") == "liked",
                "score": m.get("score"),
                "story": m.get("color_story", ""),
                "colors": m.get("dominant_colors", []),
                "theme": m.get("theme", ""),
            })

    def js(value):
        return json.dumps(value).replace("</", "<\\/")

    return (GALLERY_TEMPLATE.replace("__REASONS__", js(DISLIKE_REASONS))
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
