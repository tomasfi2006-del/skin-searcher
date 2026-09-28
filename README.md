# skin-searcher

Basically in my content creator job I have like a command where I can copy people's avatar, and I just picked random avatars that looks flashy, vibrant for my recordings

Could you possibly help me with that? and make like a catalog of users that would be updating?

Like what i usually do I just go on roblox search user and type random words like, boy, yellow, red, etc and look for flashy vibrant cool ones, i could pick some out so u would have an idea on what I mean by these characteristics

## What this does

`catalog.py` automates the manual search: it searches Roblox usernames for
whatever keywords you give it (`boy`, `yellow`, `red`, etc), downloads each
candidate's current avatar render, and asks Gemini to judge if it looks
flashy/vibrant/cool. Anything that passes gets saved as `catalog/avatars/<username>.png`
so you can grab the username straight from the filename for your copy command.

Every user it checks (pass or fail) is remembered in `catalog/seen_users.json`,
so running it again later only looks at new people instead of re-scoring the
same accounts.

## Setup

```
pip install -r requirements.txt
```

Get a free Gemini API key at https://aistudio.google.com/apikey and set it:

```
export GEMINI_API_KEY=your-key-here
```

## Usage

```
python catalog.py --keywords boy yellow red cool --per-keyword 25
```

- `--keywords` — search terms, same as what you'd type into Roblox's user search
- `--per-keyword` — how many users to pull per keyword (default 25)
- `--shuffle` — randomize the order keywords are processed in

Run it again anytime with the same or new keywords to add more avatars to the
catalog — already-checked users are skipped automatically.

Uses `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`, alternating between
them for every avatar. Each is capped on the free tier at 15 requests/min and
500/day, so alternating roughly doubles real throughput (~26-28 avatars/min)
instead of hammering one model. The script paces its own calls to stay under
each model's per-minute cap, retries on transient "high demand" errors, and
if a model's *daily* cap is hit it stops the whole run cleanly (saving
progress) rather than erroring out avatar by avatar — just rerun tomorrow.
Override either model with `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL` if a
model name ever changes.
