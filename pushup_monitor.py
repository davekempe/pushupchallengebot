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
from pathlib import Path

TEAM_URL = "https://www.thepushupchallenge.com.au/fundraisers/PacketPushupers"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 PushupMonitor/1.0"

# Matches: var teamMembers = '[...]';
MEMBERS_RE = re.compile(r"var teamMembers = '(.*?)';", re.S)


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


def build_summary(members: list[dict]) -> str:
    """A daily team scoreboard, sorted high-to-low."""
    ranked = sorted(members, key=pushups, reverse=True)
    total = sum(pushups(m) for m in members)
    doers = [m for m in ranked if pushups(m) > 0]

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 *Daily Push-Up Update — Packet Push(up)ers!* 💪",
             f"Team total so far: *{total:,}* push-ups across {len(members)} members! 🔥",
             ""]
    if doers:
        lines.append("*Leaderboard:*")
        for i, m in enumerate(doers):
            badge = medals[i] if i < len(medals) else "•"
            target = int(float(m.get("m_target_steps") or 0))
            count = pushups(m)
            line = f"{badge} {nice_name(m)} — {count:,}"
            if target:
                line += f" ({round(count / target * 100)}% of {target:,})"
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

    html = fetch_page()
    members = parse_members(html)

    if summary_mode:
        msg = build_summary(members)
        print(msg)
        if not dry_run:
            post_to_slack(webhook, msg)
        return 0

    state = load_state()
    first_run = not state

    new_state = {}
    messages = []
    for member in members:
        mid = str(member.get("member_id"))
        now = pushups(member)
        new_state[mid] = now
        prev = state.get(mid)
        if not first_run and prev is not None and now > prev:
            messages.append(build_message(member, prev, now))

    if first_run:
        total = sum(new_state.values())
        print(f"First run — seeding baseline for {len(new_state)} members "
              f"({total:,} push-ups so far). No messages sent.")
    else:
        for msg in messages:
            print(msg)
            if not dry_run:
                post_to_slack(webhook, msg)
        if not messages:
            print("No increases since last check.")

    save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
