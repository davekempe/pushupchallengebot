#!/usr/bin/env python3
"""Hourly monitor for the Packet Push(up)ers fundraising team.

Fetches the Funraisin team page, parses each member's pushup count
(embedded in the page as a `var teamMembers = '...'` JS variable), compares
against the last saved snapshot, and posts an encouraging message to Slack
whenever someone's count has gone up.

Pure standard library — no pip installs, so it runs anywhere Python 3 does.
"""

import json
import os
import random
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

TEAM_URL = "https://www.thepushupchallenge.com.au/fundraisers/PacketPushupers"
FACTS_URL = "https://www.thepushupchallenge.com.au/challenge/daily-facts"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 PushupMonitor/1.0"

# Matches: var teamMembers = '[...]';
MEMBERS_RE = re.compile(r"var teamMembers = '(.*?)';", re.S)
# The daily-facts page renders TODAY's target inline, e.g.
#   "today's push-up target is 100 - because as little as 10 minutes..."
TARGET_RE = re.compile(r"push-up target is\s*(\d[\d,]*)\s*(?:-|&ndash;)?\s*(because[^<]{5,220})?", re.I)
# m_target_steps == 1654 means the member opted for the half challenge.
HALF_TARGET_STEPS = 1654


def load_webhook() -> str:
    """Slack webhook from env, falling back to a local .env file."""
    url = os.environ.get("PUSHUP_SLACK_WEBHOOK", "").strip()
    if url:
        return url
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("PUSHUP_SLACK_WEBHOOK"):
                _, _, val = line.partition("=")
                return val.strip().strip('"').strip("'")
    raise SystemExit("No Slack webhook found. Set PUSHUP_SLACK_WEBHOOK or add it to .env")


def fetch_page() -> str:
    req = urllib.request.Request(TEAM_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_members(html: str) -> list[dict]:
    m = MEMBERS_RE.search(html)
    if not m:
        raise RuntimeError("Could not find teamMembers data in page — site layout may have changed")
    raw = m.group(1).encode().decode("unicode_escape")
    return json.loads(raw)


def fetch_daily_target() -> tuple[int | None, str | None]:
    """Scrape today's push-up target (and its mental-health fact) from the
    date-aware daily-facts page. Returns (None, None) on a rest day or any
    failure — callers must treat the target as optional."""
    try:
        req = urllib.request.Request(FACTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        html = html.replace("&ndash;", "-").replace("&rsquo;", "'").replace("&nbsp;", " ")
        m = TARGET_RE.search(html)
        if not m:
            return None, None
        target = int(m.group(1).replace(",", ""))
        fact = (m.group(2) or "").strip().rstrip(".") or None
        return target, fact
    except Exception:
        return None, None


def daily_target_for(member: dict, base_target: int | None) -> int:
    """A member's target for today — halved if they're on the half challenge."""
    if not base_target:
        return 0
    full_goal = int(float(member.get("m_target_steps") or 0))
    if full_goal == HALF_TARGET_STEPS:
        return round(base_target / 2)
    return base_target


def pushups(member: dict) -> int:
    """total_steps is the pushup tally; it comes as a float string like '40.00'."""
    try:
        return int(float(member.get("total_steps") or 0))
    except (TypeError, ValueError):
        return 0


def nice_name(member: dict) -> str:
    return (member.get("name") or member.get("m_username") or "Someone").strip().title()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post_to_slack(webhook: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# Encouraging opener emoji + a matching cheer, picked at random for variety.
CHEERS = [
    ("💪", "Beast mode!"),
    ("🔥", "On fire!"),
    ("🚀", "To the moon!"),
    ("⚡", "Unstoppable!"),
    ("🏆", "Champion effort!"),
    ("🙌", "Keep it up!"),
    ("👏", "Smashing it!"),
    ("💥", "Boom!"),
    ("🦾", "Pure power!"),
    ("🌟", "Superstar!"),
]
# Extra celebration when someone crosses their goal.
GOAL_CHEERS = ["🎉🎯", "🏅🎉", "🥳🏆", "🎊💯"]


def build_message(member: dict, prev: int, now: int) -> str:
    name = nice_name(member)
    gained = now - prev
    target = int(float(member.get("m_target_steps") or 0))
    rep_word = "push-up" if gained == 1 else "push-ups"
    emoji, cheer = random.choice(CHEERS)

    msg = f"{emoji} *{name}* just knocked out {gained} {rep_word}! {cheer} (total: {now:,}"
    if target:
        pct = round(now / target * 100)
        msg += f" / {target:,} — {pct}%"
    msg += ")"

    # Celebrate hitting the goal this round.
    if target and prev < target <= now:
        party = random.choice(GOAL_CHEERS)
        msg += f"\n{party} *{name}* just reached their goal of {target:,} push-ups! Legend! 🙌"
    return msg


def build_target_message(member: dict, reps_today: int, mdt: int, half: bool) -> str:
    party = random.choice(GOAL_CHEERS)
    extra = " (half challenge)" if half else ""
    return (f"{party} *{nice_name(member)}* just smashed today's target of "
            f"{mdt:,} push-ups{extra} — {reps_today:,} done today! 🙌")


def migrate_state(state: dict) -> dict:
    """Bring forward old flat {id: count} snapshots to the structured format."""
    if "counts" in state:
        return state
    counts = {k: v for k, v in state.items() if str(k).isdigit()}
    return {"day": None, "counts": counts, "day_baseline": {}, "target_hit": []}


def build_summary(members: list[dict], baseline: dict, target: int | None,
                  fact: str | None) -> str:
    """Daily team scoreboard: lifetime totals, today's target, and progress
    against it."""
    ranked = sorted(members, key=pushups, reverse=True)
    total = sum(pushups(m) for m in members)
    doers = [m for m in ranked if pushups(m) > 0]

    def reps_today(m):
        return max(0, pushups(m) - baseline.get(str(m.get("member_id")), pushups(m)))

    lines = ["📊 *Daily Push-Up Update — Packet Push(up)ers!* 💪",
             f"Team total so far: *{total:,}* push-ups across {len(members)} members! 🔥"]

    if target:
        lines.append(f"🎯 Today's target: *{target:,}* push-ups each.")
        if fact:
            lines.append(f"_{fact.capitalize()}._")
        team_today = sum(reps_today(m) for m in members)
        team_goal = sum(daily_target_for(m, target) for m in members)
        pct = round(team_today / team_goal * 100) if team_goal else 0
        bar = "🟩" * (pct // 10) + "⬜" * (10 - min(10, pct // 10))
        lines.append(f"Team did *{team_today:,}* of {team_goal:,} today — {pct}% {bar}")
    else:
        lines.append("😌 Rest day — no target today. Recover those arms!")
    lines.append("")

    medals = ["🥇", "🥈", "🥉"]
    if doers:
        lines.append("*Leaderboard:*")
        for i, m in enumerate(doers):
            badge = medals[i] if i < len(medals) else "•"
            goal = int(float(m.get("m_target_steps") or 0))
            count = pushups(m)
            today = reps_today(m)
            line = f"{badge} {nice_name(m)} — {count:,}"
            if goal:
                line += f" ({round(count / goal * 100)}% of {goal:,})"
            if today:
                hit = " ✅" if target and today >= daily_target_for(m, target) else ""
                line += f"  ·  +{today:,} today{hit}"
            lines.append(line)
    else:
        lines.append("No push-ups logged yet — who's going to get us on the board first? 🏁")
    lines.append("")
    lines.append("Keep pushing, team! Every rep counts for a great cause. 🙌💙")
    return "\n".join(lines)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    summary_mode = "--summary" in sys.argv
    webhook = None if dry_run else load_webhook()

    today = date.today().isoformat()
    members = parse_members(fetch_page())
    target, fact = fetch_daily_target()

    state = migrate_state(load_state())
    counts = state.get("counts", {})
    first_run = not counts

    # Day rollover: snapshot today's starting point and reset the target winners.
    if state.get("day") != today:
        state["day"] = today
        state["day_baseline"] = dict(counts)  # counts carried from yesterday
        state["target_hit"] = []
    baseline = state["day_baseline"]
    target_hit = state["target_hit"]

    if summary_mode:
        msg = build_summary(members, baseline, target, fact)
        print(msg)
        if not dry_run:
            post_to_slack(webhook, msg)
            save_state(state)  # persist any rollover that happened
        return 0

    new_counts = {}
    messages = []
    for member in members:
        mid = str(member.get("member_id"))
        now = pushups(member)
        new_counts[mid] = now
        prev = counts.get(mid)

        # 1) Any increase → an encouraging shout-out.
        if not first_run and prev is not None and now > prev:
            messages.append(build_message(member, prev, now))

        # 2) Crossing today's daily target → a one-time celebration.
        mdt = daily_target_for(member, target)
        reps_today = now - baseline.get(mid, now)
        if not first_run and mdt and mid not in target_hit and reps_today >= mdt:
            half = int(float(member.get("m_target_steps") or 0)) == HALF_TARGET_STEPS
            messages.append(build_target_message(member, reps_today, mdt, half))
            target_hit.append(mid)

    if first_run:
        total = sum(new_counts.values())
        print(f"First run — seeding baseline for {len(new_counts)} members "
              f"({total:,} push-ups so far). No messages sent.")
        state["day_baseline"] = dict(new_counts)
    else:
        for msg in messages:
            print(msg)
            if not dry_run:
                post_to_slack(webhook, msg)
        if not messages:
            print("No increases since last check.")

    state["counts"] = new_counts
    if not dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
