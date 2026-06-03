# Packet Push(up)ers — Slack progress bot

Hourly job that watches our team page on The Push-Up Challenge and cheers
teammates on in Slack whenever their push-up count goes up. 💪

## How it works

1. Fetches https://www.thepushupchallenge.com.au/fundraisers/PacketPushupers
2. Parses each member's push-up count from the page's embedded `teamMembers`
   data (`total_steps` field — no API key needed).
3. Scrapes **today's daily target** + mental-health fact from the date-aware
   `/challenge/daily-facts` page (e.g. 3 Jun 2026 = 100). Half-challenge members
   get a halved target automatically.
4. Compares against the last snapshot in `state.json`.
5. Posts to Slack for:
   - **Any increase** — an encouraging shout-out showing progress toward today's
     target, e.g.
     > 🔥 *Dave Kempe* just knocked out 25 push-ups! On fire! (*45/100* toward today's target · 45 total)
   - **Hitting today's target** — a one-time celebration per person per day, e.g.
     > 🎊💯 *Dan Gauci* just smashed today's target of 100 push-ups — 100 done today! 🙌
   - **Reaching their overall goal** — an extra 🎉.

"Reps today" = current cumulative count minus a baseline snapshotted at the
start of each day (tracked in `state.json`). The first run just records a
baseline silently — no spam.

The daily `--summary` post shows the team total, today's target + fact, a
team-progress bar against the day's goal, and a leaderboard with each person's
"+N today".

## Setup

```bash
cp .env.example .env      # then paste the real Slack webhook URL
```

Requires only Python 3 (standard library — nothing to `pip install`).

## Run manually

```bash
python3 pushup_monitor.py                # hourly check: increase + target-hit alerts
python3 pushup_monitor.py --summary      # daily leaderboard + today's target
python3 pushup_monitor.py --dry-run      # prints messages, posts nothing, leaves state untouched
python3 pushup_monitor.py --summary --dry-run
```

## Hourly schedule (cron)

```
0 * * * * /home/dave/src/pushupchallenge/run.sh
```

Output is appended to `monitor.log`. State lives in `state.json` — delete it to
re-seed a fresh baseline.

## Files

- `pushup_monitor.py` — the job
- `run.sh` — cron wrapper (cd + logging)
- `.env` — `PUSHUP_SLACK_WEBHOOK=...` (git-ignored)
- `state.json` — last-seen counts (git-ignored, auto-created)
- `monitor.log` — run history (git-ignored)
