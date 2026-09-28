# skin-searcher

Basically in my content creator job I have like a command where I can copy people's avatar, and I just picked random avatars that looks flashy, vibrant for my recordings

Could you possibly help me with that? and make like a catalog of users that would be updating?

Like what i usually do I just go on roblox search user and type random words like, boy, yellow, red, etc and look for flashy vibrant cool ones, i could pick some out so u would have an idea on what I mean by these characteristics

## What this does

`catalog.py` has two modes. Either way, matches get saved as
`catalog/avatars/<username>.png` — grab the username straight from the
filename for your copy command.

**Auto mode** (`--keywords`) automates the manual search: it searches Roblox
usernames for whatever keywords you give it (`boy`, `yellow`, `red`, etc),
downloads each candidate's current avatar render, and asks Gemini to judge
it against what "flashy/vibrant/cool" actually turned out to mean after
reviewing 20 real examples: a clear intentional character concept, built
around one standout hat/accessory, with a deliberate bold color story
(saturated color blocking OR a striking monochrome/high-contrast look) —
not a cluttered mess or a random mismatch of default Roblox colors.
Costume/character references (Sonic, superheroes, etc) are fine.

It also skips accounts with more than `--max-followers` followers (default
5,000) before even spending a Gemini call on them — avoids the catalog
suggesting you copy some well-known player's specific look.

**Manual mode** (`--usernames`) is for when you've already spotted good ones
yourself by browsing Roblox — just hand it the usernames (with or without
the `@`) and it resolves each one, downloads the avatar, and saves it
straight away. No Gemini call, no quota used. It still checks follower
count, but since you picked these yourself it just adds a heads-up next to
the name instead of skipping it.

In `--keywords` mode, every user it checks (pass or fail) is remembered in
`catalog/seen_users.json`, so running it again later only looks at new
people instead of re-scoring the same accounts.

## Setup

```
pip install -r requirements.txt
```

For `--keywords` mode, get a free Gemini API key at
https://aistudio.google.com/apikey and set it:

```
export GEMINI_API_KEY=your-key-here
```

`--usernames` mode needs no API key at all.

## Usage

```
python catalog.py --keywords boy yellow red cool --per-keyword 25
python catalog.py --usernames Cone_phonky Shark_Vibess RedGuy1011
```

- `--keywords` — search terms, same as what you'd type into Roblox's user search (auto-judged by Gemini)
- `--per-keyword` — how many users to pull per keyword (default 25)
- `--shuffle` — randomize the order keywords are processed in
- `--usernames` — specific usernames you already picked; saved directly, no Gemini involved
- `--max-followers` — skip/flag accounts more followed than this (default 5,000)

Run it again anytime with new keywords or usernames to add more avatars to
the catalog — in `--keywords` mode, already-checked users are skipped
automatically.

Uses `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`, alternating between
them for every avatar. Each is capped on the free tier at 15 requests/min and
500/day, so alternating roughly doubles real throughput (~26-28 avatars/min)
instead of hammering one model. The script paces its own calls to stay under
each model's per-minute cap, retries on transient "high demand" errors, and
if a model's *daily* cap is hit it stops the whole run cleanly (saving
progress) rather than erroring out avatar by avatar — just rerun tomorrow.
Override either model with `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL` if a
model name ever changes.
