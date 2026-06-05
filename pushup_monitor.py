#!/usr/bin/env python3
"""Hourly monitor for a Push-Up Challenge fundraising team.

Fetches the Funraisin team page, parses each member's pushup count
(embedded in the page as a `var teamMembers = '...'` JS variable), compares
against the last saved snapshot, and posts an encouraging message to Slack
whenever someone's count has gone up.

The team and Slack webhook are configured via environment / .env:
    PUSHUP_TEAM           team slug or full fundraiser URL (e.g. PacketPushupers)
    PUSHUP_SLACK_WEBHOOK  Slack incoming-webhook URL
    PUSHUP_TEAM_NAME      optional display-name override (else read from the page)

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

BASE_URL = "https://www.thepushupchallenge.com.au"
FACTS_URL = f"{BASE_URL}/daily-facts"
DEFAULT_TEAM = "PacketPushupers"  # used if PUSHUP_TEAM is unset
HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 PushupMonitor/1.0"

# Matches: var teamMembers = '[...]';
MEMBERS_RE = re.compile(r"var teamMembers = '(.*?)';", re.S)
# Page <title> is "The Push-Up Challenge - <Team Name>".
TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)
# The daily-facts page reveals one block per day, each starting with the date
# ("Day 2 - Thursday 4th June") and containing "...push-up target is <N>...".
TARGET_NUM_RE = re.compile(r"push-up target is\s*(\d[\d,]*)", re.I)
# The explanatory clause varies day to day: "because ..." or "for the 72% ...".
FACT_RE = re.compile(r"\b(because|for the\b|for those\b|for\b)[^<.]{5,200}", re.I)
# The daily-facts page renders TODAY's target inline, e.g.
#   "today's push-up target is 100 - because as little as 10 minutes..."
TARGET_RE = re.compile(r"push-up target is\s*(\d[\d,]*)\s*(?:-|&ndash;)?\s*(because[^<]{5,220})?", re.I)
# m_target_steps == 1654 means the member opted for the half challenge.
HALF_TARGET_STEPS = 1654


def load_config() -> dict:
    """Read PUSHUP_* settings from the process env, falling back to a local
    .env file (process env wins). Lets each team run their own copy by just
    setting PUSHUP_TEAM and PUSHUP_SLACK_WEBHOOK."""
    cfg = {}
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("PUSHUP_")})

    # Accept either a bare slug or a full fundraiser URL for PUSHUP_TEAM.
    slug = (cfg.get("PUSHUP_TEAM") or DEFAULT_TEAM).strip().rstrip("/").split("/")[-1]
    return {
        "slug": slug,
        "team_url": f"{BASE_URL}/fundraisers/{slug}",
        "team_name": (cfg.get("PUSHUP_TEAM_NAME") or "").strip() or None,
        "webhook": (cfg.get("PUSHUP_SLACK_WEBHOOK") or "").strip() or None,
        "state_file": HERE / f"state-{slug}.json",
    }


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_team_name(html: str, fallback: str) -> str:
    """Pull the team's display name from the page <title>."""
    m = TITLE_RE.search(html)
    if m and " - " in m.group(1):
        return m.group(1).split(" - ", 1)[1].strip()
    return fallback


def parse_members(html: str) -> list[dict]:
    m = MEMBERS_RE.search(html)
    if not m:
        raise RuntimeError("Could not find teamMembers data in page — site layout may have changed")
    raw = m.group(1).encode().decode("unicode_escape")
    return json.loads(raw)


def date_label(d: date) -> str:
    """Format a date the way the page labels its day blocks, e.g.
    'Thursday 4th June'."""
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{d.strftime('%A')} {n}{suffix} {d.strftime('%B')}"


def fetch_daily_target(today: date) -> tuple[str, int | None, str | None]:
    """Scrape *today's* push-up target + fact from the daily-facts page.

    The page reveals one block per challenge day (in date order) and reuses the
    same "push-up target is N" wording in each, so we anchor on today's date
    label and read the target from that block. Returns a (status, target, fact)
    tuple where status is:
      "ok"      — target found (target/fact populated)
      "rest"    — today's block is a rest day
      "unknown" — page unavailable, or today's block not published yet
    Distinguishing "unknown" from "rest" avoids falsely announcing a rest day
    when the scrape simply failed."""
    try:
        req = urllib.request.Request(FACTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return "unknown", None, None

    html = (html.replace("&ndash;", "-").replace("&rsquo;", "'")
                .replace("&nbsp;", " ").replace("&amp;", "&"))
    idx = html.find(date_label(today))
    if idx == -1:
        return "unknown", None, None  # today's block not published — don't guess

    block = html[idx:idx + 4000]  # today is the last block, so this is its content
    if re.search(r"rest day|\bREST\b", block, re.I) and not TARGET_NUM_RE.search(block):
        return "rest", None, None

    m = TARGET_NUM_RE.search(block)
    if not m:
        return "unknown", None, None
    target = int(m.group(1).replace(",", ""))
    fm = FACT_RE.search(block, m.end())
    fact = fm.group(0).strip().rstrip(",").rstrip(".") if fm else None
    return "ok", target, fact


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


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


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


def build_message(member: dict, prev: int, now: int,
                  reps_today: int | None = None, mdt: int | None = None) -> str:
    name = nice_name(member)
    gained = now - prev
    goal = int(float(member.get("m_target_steps") or 0))
    rep_word = "push-up" if gained == 1 else "push-ups"
    emoji, cheer = random.choice(CHEERS)

    msg = f"{emoji} *{name}* just knocked out {gained} {rep_word}! {cheer} "
    if mdt:
        # today's reps vs daily target, then lifetime total vs overall goal
        msg += f"(today: {reps_today:,} / {mdt:,}"
        if goal:
            msg += f", total: {now:,} / {goal:,} — {round(now / goal * 100)}%)"
        else:
            msg += f", total: {now:,})"
    elif goal:
        msg += f"(total: {now:,} / {goal:,} — {round(now / goal * 100)}%)"
    else:
        msg += f"(total: {now:,})"

    # Celebrate hitting the overall goal this round.
    if goal and prev < goal <= now:
        party = random.choice(GOAL_CHEERS)
        msg += f"\n{party} *{name}* just reached their overall goal of {goal:,} push-ups! Legend! 🙌"
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


def build_summary(members: list[dict], baseline: dict, status: str,
                  target: int | None, fact: str | None, team_name: str) -> str:
    """Daily team scoreboard: lifetime totals, today's target, and progress
    against it."""
    ranked = sorted(members, key=pushups, reverse=True)
    total = sum(pushups(m) for m in members)
    doers = [m for m in ranked if pushups(m) > 0]

    def reps_today(m):
        return max(0, pushups(m) - baseline.get(str(m.get("member_id")), pushups(m)))

    lines = [f"📊 *Daily Push-Up Update — {team_name}!* 💪",
             f"Team total so far: *{total:,}* push-ups across {len(members)} members! 🔥"]

    if target:
        lines.append(f"🎯 Today's target: *{target:,}* push-ups each.")
        if fact:
            lines.append(f"_{fact[0].upper() + fact[1:]}._")
        team_today = sum(reps_today(m) for m in members)
        team_goal = sum(daily_target_for(m, target) for m in members)
        pct = round(team_today / team_goal * 100) if team_goal else 0
        bar = "🟩" * (pct // 10) + "⬜" * (10 - min(10, pct // 10))
        lines.append(f"Team did *{team_today:,}* of {team_goal:,} today — {pct}% {bar}")
    elif status == "rest":
        lines.append("😌 Rest day — no target today. Recover those arms!")
    # status == "unknown": skip the target section rather than guess a rest day.
    lines.append("")

    medals = ["🥇", "🥈", "🥉"]
    if doers:
        lines.append("*Leaderboard:*")
        for i, m in enumerate(doers):
            badge = medals[i] if i < len(medals) else "•"
            goal = int(float(m.get("m_target_steps") or 0))
            count = pushups(m)
            today = reps_today(m)
            mdt = daily_target_for(m, target)
            line = f"{badge} {nice_name(m)} — {count:,}"
            if goal:
                line += f" ({round(count / goal * 100)}% of {goal:,})"
            if mdt and today >= mdt:
                line += f"  ·  +{today:,} today ✅"
            elif mdt:
                line += f"  ·  +{today:,} today (*{mdt - today:,} to go*)"
            elif today:
                line += f"  ·  +{today:,} today"
            lines.append(line)
    else:
        lines.append("No push-ups logged yet — who's going to get us on the board first? 🏁")
    lines.append("")

    # Closing line nudges the team to catch up when behind today's target.
    if target:
        team_done = sum(reps_today(m) for m in members)
        team_goal = sum(daily_target_for(m, target) for m in members)
        if team_done >= team_goal:
            lines.append("🎉 Team's smashed today's target — anything more is bonus. Awesome work! 🙌💙")
        else:
            to_go = team_goal - team_done
            lines.append(f"⏰ *{to_go:,} to go* to hit today's team target — drop and give us "
                         f"some, there's still time to catch up! 💪🔥")
    else:
        lines.append("Keep pushing, team! Every rep counts for a great cause. 🙌💙")
    return "\n".join(lines)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    summary_mode = "--summary" in sys.argv

    cfg = load_config()
    webhook = cfg["webhook"]
    if not dry_run and not webhook:
        raise SystemExit("No Slack webhook found. Set PUSHUP_SLACK_WEBHOOK or add it to .env")

    today = date.today().isoformat()
    html = fetch_page(cfg["team_url"])
    members = parse_members(html)
    team_name = cfg["team_name"] or parse_team_name(html, cfg["slug"])
    status, target, fact = fetch_daily_target(date.fromisoformat(today))

    state = migrate_state(load_state(cfg["state_file"]))
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
        msg = build_summary(members, baseline, status, target, fact, team_name)
        print(msg)
        if not dry_run:
            post_to_slack(webhook, msg)
            save_state(cfg["state_file"], state)  # persist any rollover that happened
        return 0

    new_counts = {}
    messages = []
    for member in members:
        mid = str(member.get("member_id"))
        now = pushups(member)
        new_counts[mid] = now
        prev = counts.get(mid)
        mdt = daily_target_for(member, target)
        reps_today = now - baseline.get(mid, now)

        # 1) Any increase → an encouraging shout-out (with today's-target context).
        if not first_run and prev is not None and now > prev:
            messages.append(build_message(member, prev, now, reps_today, mdt))

        # 2) Crossing today's daily target → a one-time celebration.
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
        save_state(cfg["state_file"], state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
