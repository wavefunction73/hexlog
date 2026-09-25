# rustlog

Pulls BattleMetrics data for one Rust server and builds a timeline of when your clan was online. Python 3.8+ only, nothing to install.

## Setup (once)

1. Get a free BattleMetrics API token: log in at battlemetrics.com, open **Account → Developers / API tokens**, and create a personal token (default read scopes are enough). Player session history generally requires a token.
2. In this folder run `python rustlog.py init`, then open `config.json`:
   - `api_token`: paste your token
   - `server_id`: already set to 20839213
   - `wipe_start`: `"auto"` uses the server's last wipe time, or set an ISO time like `"2026-09-18T18:00:00Z"`
   - `clan`: your 8 members' names
3. Find each member's BattleMetrics ID: `python rustlog.py find "Name1" "Name2"` and copy the right `id` into `bm_id`. (Members with an exact name match on the wipe leaderboard are matched automatically, but IDs are safer since names change.)

## Use

    python rustlog.py timeline      # the main one
    python rustlog.py server        # server status, last/next wipe
    python rustlog.py leaderboard --top 30
    python rustlog.py demo          # fake data, to preview the timeline

Add `--since 2026-09-16T18:00:00Z --until 2026-09-25T06:00:00Z` to any of the window commands to pick a custom period.

`timeline` writes to `output/`:
- `timeline.html` open in a browser. Hover anywhere to see who was on at that moment.
- `summary.txt` compact text summary. Paste this to Claude.
- `sessions.csv` every session, for spreadsheets.
- `sessions.json` raw data.

All times are shown in your computer's local timezone.

## Run it hourly and share it with the clan (GitHub)

1. Create a free GitHub account and a new repository (e.g. `rustlog`).
2. **Remove your token from `config.json`** (set `"api_token": ""`), then upload everything in this folder, including the hidden `.github` folder. (Easiest: install GitHub Desktop, add this folder as a repository, publish it.)
3. In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Name `BM_TOKEN`, value your BattleMetrics token.
4. **Settings → Pages → Source: GitHub Actions.**
5. **Actions tab → Update clan timeline → Run workflow** to do the first run.
6. Share the link shown in the run (and under Settings → Pages), e.g. `https://yourname.github.io/rustlog/`. The summary is at `/summary.txt`.

It then updates every hour. To change the clan list, edit `config.json` on GitHub. On a new wipe nothing needs changing: `"wipe_start": "auto"` follows the server.

Notes
- The page is public to anyone who has the link (on free GitHub, even if the repo is private).
- Only the page and summary are published, not the raw CSV/JSON.
- GitHub pauses scheduled runs if a repo has no commits for 60 days; re-enable from the Actions tab.
