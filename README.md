# Push-Up Challenge — Slack progress bot

Hourly job that watches a team page on The Push-Up Challenge and cheers
teammates on in Slack whenever their push-up count goes up. 💪

Configure it for **any team** with two env vars — see [Setup](#setup) — so
other teams can run their own copy.

## How it works

1. Fetches your team's fundraiser page (`/fundraisers/<PUSHUP_TEAM>`).
2. Parses each member's push-up count from the page's embedded `teamMembers`
   data (`total_steps` field — no API key needed). The team name is read from
   the page title.
3. Scrapes **today's daily target** + mental-health fact from the date-aware
   `/challenge/daily-facts` page (e.g. 3 Jun 2026 = 100). Half-challenge members
   get a halved target automatically.
4. Compares against the last snapshot in `state-<team>.json`.
5. Posts to Slack for:
   - **Any increase** — an encouraging shout-out showing progress toward today's
     target, e.g.
     > 🔥 *Dave Kempe* just knocked out 25 push-ups! On fire! (*45/100* toward today's target · 45 total)
   - **Hitting today's target** — a one-time celebration per person per day, e.g.
     > 🎊💯 *Dan Gauci* just smashed today's target of 100 push-ups — 100 done today! 🙌
   - **Reaching their overall goal** — an extra 🎉.

"Reps today" = current cumulative count minus a baseline snapshotted at the
start of each day (tracked in the state file). The first run just records a
baseline silently — no spam.

The `--summary` post shows the team total, today's target + fact, a
team-progress bar against the day's goal, and a leaderboard with each person's
"+N today" and how many they have "to go".

## Setup

```bash
cp .env.example .env      # then edit it
```

Set two values in `.env`:

| Variable | What |
|---|---|
| `PUSHUP_TEAM` | Your team's slug — the bit after `/fundraisers/` in the page URL (a full URL works too). |
| `PUSHUP_SLACK_WEBHOOK` | A Slack [incoming webhook](https://api.slack.com/messaging/webhooks) for the channel to post in. |
| `PUSHUP_TEAM_NAME` | *(optional)* Display name override; otherwise read from the page. |

Requires only Python 3 (standard library — nothing to `pip install`).

## Run manually

```bash
python3 pushup_monitor.py                # hourly check: increase + target-hit alerts
python3 pushup_monitor.py --summary      # leaderboard + today's target
python3 pushup_monitor.py --dry-run      # prints messages, posts nothing, leaves state untouched
python3 pushup_monitor.py --summary --dry-run
```

You can also point it at a team for one run without editing `.env`:

```bash
PUSHUP_TEAM=SomeOtherTeam PUSHUP_SLACK_WEBHOOK=https://... python3 pushup_monitor.py --dry-run
```

## Schedule (cron)

```
0 * * * *  /path/to/pushupchallenge/run.sh             # hourly increase + target alerts
0 12 * * * /path/to/pushupchallenge/run.sh --summary   # midday catch-up checkpoint
0 18 * * * /path/to/pushupchallenge/run.sh --summary   # end-of-day recap
```

The summary shows each person's "to go" until they hit the day's target and,
when the team is behind, how many push-ups are left to catch up — so the noon
post nudges people to get moving. Output is appended to `monitor.log`. Delete
the team's `state-<team>.json` to re-seed a fresh baseline.

## Sharing with another team

Each team just needs their own checkout (or copy) with its own `.env`:

```bash
git clone git@github.com:davekempe/pushupchallengebot.git
cd pushupchallengebot
cp .env.example .env   # set PUSHUP_TEAM + PUSHUP_SLACK_WEBHOOK
```

State is namespaced per team (`state-<team>.json`), so one checkout can even
monitor several teams via separate cron lines that each set `PUSHUP_TEAM`.

## Files

- `pushup_monitor.py` — the job
- `run.sh` — cron wrapper (cd + logging)
- `.env` — `PUSHUP_TEAM` + `PUSHUP_SLACK_WEBHOOK` (git-ignored)
- `state-<team>.json` — last-seen counts per team (git-ignored, auto-created)
- `monitor.log` — run history (git-ignored)
