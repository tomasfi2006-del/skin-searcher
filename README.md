# skin-searcher

Basically in my content creator job I have like a command where I can copy people's avatar, and I just picked random avatars that looks flashy, vibrant for my recordings

Could you possibly help me with that? and make like a catalog of users that would be updating?

Like what i usually do I just go on roblox search user and type random words like, boy, yellow, red, etc and look for flashy vibrant cool ones, i could pick some out so u would have an idea on what I mean by these characteristics

## Quick start

Double-click **`start.bat`** (Windows) or **`start.command`** (Mac) to open the
menu, then choose **7** to paste your free
[Gemini API key](https://aistudio.google.com/apikey). Full walkthrough with
screenshots-style examples, settings and troubleshooting:
**[Skin-Searcher-Guide.pdf](Skin-Searcher-Guide.pdf)**.

To look through what it found, choose **4**: a page opens in your browser with
every saved avatar in a grid (search, sort, resize, click one to copy its
username). Also available as `python catalog.py --gallery`.

Your key, settings and avatars live in the `catalog/` folder, which is never
uploaded to GitHub.

## What this does

`catalog.py` finds Roblox avatars worth copying and saves them as
`catalog/avatars/<username>.png` — grab the username straight from the
filename for your copy command. Accounts with more than 500 followers are
always skipped (change with `--max-followers`), so you never end up copying
a well-known player's look.

There are three ways to feed it people:

- **`--groups` (recommended)** pulls members of Roblox groups. Point it at
  outfit/fashion/avatar groups and most of the people in them care about
  their look, so far more of them are keepers than random search results.
- **`--keywords`** uses Roblox user search, like typing `boy`, `yellow`,
  `red` into the search bar. Roblox only allows one search per minute
  without logging in, so this is the slow option.
- **`--usernames`** saves specific people you already picked by hand. No
  Gemini, no API key needed.

With `--groups` and `--keywords`, Gemini judges each avatar against what
"flashy/vibrant/cool" turned out to mean after reviewing 20 real picks: a
clear intentional character concept, built around one standout
hat/accessory, with a deliberate bold color story (saturated color blocking
OR a striking monochrome/high-contrast look) — not cluttered, and not a
random mismatch of default Roblox colors. Costume/character references
(Sonic, superheroes, etc) are fine.

Everyone who gets a final answer is remembered in `catalog/seen_users.json`,
so reruns only look at new people — running the same groups again just goes
deeper into their member lists. People who couldn't be checked because of a
temporary error are *not* remembered, so they get another chance next run.

## Setup (command line)

```
pip install -r requirements.txt
```

For `--groups` and `--keywords`, get a free Gemini API key at
https://aistudio.google.com/apikey. Either save it once from the menu
(option 7), or set it for the session:

```
export GEMINI_API_KEY=your-key-here
```

## Usage

Run `python catalog.py` with no options for the menu, or use options:

```
python catalog.py --groups outfits fashion drip
python catalog.py --groups 5215595 906726123
python catalog.py --keywords boy yellow red cool
python catalog.py --usernames Cone_phonky Shark_Vibess RedGuy1011
python catalog.py --gallery
```

- `--groups` — group search terms (uses the top matches) or group IDs from a group's URL
- `--keywords` — user search terms, same as Roblox's user search bar
- `--usernames` — specific usernames you already picked
- `--gallery` — open a page showing every saved avatar in a grid
- `--per-group` — new members to judge per group (default 300)
- `--groups-per-term` — how many matching groups to use per search term (default 3)
- `--per-keyword` — users to pull per keyword (default 100)
- `--max-followers` — skip accounts with more followers than this (default 500)
- `--batch-size` — avatars sent to Gemini per request (default 10)
- `--shuffle` — randomize keyword order

Ctrl+C anytime — progress is saved.

## How fast is it?

- **Gemini is the limit in `--groups` mode**: about 60 avatars judged per
  minute. Each request judges 10 avatars at once, since the free-tier limits
  (15 requests/min and 500/day per model) count requests, not images. The
  script alternates `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`,
  which is roughly 10,000 judged avatars a day. If one model runs out for the
  day it keeps going on the other. Override the models with `GEMINI_MODEL` /
  `GEMINI_FALLBACK_MODEL` if their names ever change.
- **Roblox user search is the limit in `--keywords` mode**: 1 search per
  minute, so every search asks for the maximum 100 results and the script
  judges one keyword's results while it waits for the next search.
- **Follower counts** (100 lookups/min) are only checked for avatars Gemini
  already liked, so they don't slow anything down.
- All Roblox calls follow Roblox's rate-limit headers, so they wait out a
  limit instead of failing.
