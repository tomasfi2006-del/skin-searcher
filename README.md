# skin-searcher

Basically in my content creator job I have like a command where I can copy people's avatar, and I just picked random avatars that looks flashy, vibrant for my recordings

Could you possibly help me with that? and make like a catalog of users that would be updating?

Like what i usually do I just go on roblox search user and type random words like, boy, yellow, red, etc and look for flashy vibrant cool ones, i could pick some out so u would have an idea on what I mean by these characteristics

## What this does

`catalog.py` has two modes. Either way, matches get saved as
`catalog/avatars/<username>.png` — grab the username straight from the
filename for your copy command. Accounts with more than 500 followers are
always skipped (change with `--max-followers`), so you never end up copying
a well-known player's look.

**Auto mode** (`--keywords`) does the Roblox search for you and has Gemini
judge each avatar against what "flashy/vibrant/cool" turned out to mean
after reviewing 20 real picks: a clear intentional character concept, built
around one standout hat/accessory, with a deliberate bold color story
(saturated color blocking OR a striking monochrome/high-contrast look) — not
cluttered, and not a random mismatch of default Roblox colors.
Costume/character references (Sonic, superheroes, etc) are fine.

**Manual mode** (`--usernames`) is for ones you spotted yourself — hand it
the usernames (with or without the `@`) and it saves their avatars straight
away. No Gemini, no API key needed.

Every user that gets a final answer is remembered in
`catalog/seen_users.json`, so reruns only look at new people. Users that
couldn't be checked because of a temporary error are *not* remembered, so
they get another chance next run.

## Setup

```
pip install -r requirements.txt
```

For `--keywords` mode, get a free Gemini API key at
https://aistudio.google.com/apikey and set it:

```
export GEMINI_API_KEY=your-key-here
```

## Usage

```
python catalog.py --keywords boy yellow red cool
python catalog.py --usernames Cone_phonky Shark_Vibess RedGuy1011
```

- `--keywords` — search terms, same as what you'd type into Roblox's user search
- `--usernames` — specific usernames you already picked
- `--per-keyword` — users to pull per keyword (default 100)
- `--max-followers` — skip accounts with more followers than this (default 500)
- `--batch-size` — avatars sent to Gemini per request (default 10)
- `--shuffle` — randomize keyword order

Ctrl+C anytime — progress is saved.

## How fast is it?

The limits that matter, and how the script works around them:

- **Roblox user search: 1 call per minute** without logging in. This is the
  real bottleneck, so every search asks for the maximum 100 results, and the
  script filters and judges one keyword's results while it waits for the
  next search slot. Expect roughly 100 candidates per keyword per minute.
- **Gemini: 15 requests/min and 500/day per model.** Those limits count
  requests, not images, so each request judges 10 avatars at once. The
  script alternates between `gemini-3.5-flash-lite` and
  `gemini-3.1-flash-lite`, which gives about 10,000 judged avatars a day.
  If one model runs out for the day it keeps going on the other, and only
  stops when both are out. Override the models with `GEMINI_MODEL` /
  `GEMINI_FALLBACK_MODEL` if their names ever change.
- Follower checks and avatar downloads run in parallel and follow Roblox's
  rate-limit headers, so they wait out the limit instead of failing.
