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
# "Day 3 - Friday 5th June" → captures the day number and month name.
DAY_BLOCK_RE = re.compile(r"Day\s+\d{1,2}\s*-\s*[A-Za-z]+day\s+(\d{1,2})\w*\s+([A-Za-z]+)", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
# Cached per-date target schedule, shared across teams (challenge-wide data).
# Used for the mental-health fact text and as a fallback for unknown dates.
SCHEDULE_FILE = HERE / "daily_targets.json"

# Official 2026 schedule (Day 1 = 3 Jun), transcribed from the app's Progress
# screen. 0 == rest day. Verified: the non-rest targets sum to 3,307.
DAILY_TARGETS_2026 = {
    "2026-06-03": 100, "2026-06-04": 72,  "2026-06-05": 120, "2026-06-06": 150,
    "2026-06-07": 0,   "2026-06-08": 140, "2026-06-09": 170, "2026-06-10": 130,
    "2026-06-11": 160, "2026-06-12": 167, "2026-06-13": 191, "2026-06-14": 0,
    "2026-06-15": 120, "2026-06-16": 220, "2026-06-17": 160, "2026-06-18": 190,
    "2026-06-19": 170, "2026-06-20": 208, "2026-06-21": 0,   "2026-06-22": 120,
    "2026-06-23": 180, "2026-06-24": 229, "2026-06-25": 160, "2026-06-26": 150,
}
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


def load_schedule() -> dict:
    """The known per-date targets: {"2026-06-03": {"target": 100, "fact": "..."}}."""
    if SCHEDULE_FILE.exists():
        try:
            return json.loads(SCHEDULE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_schedule(schedule: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2, sort_keys=True))


def extract_targets_from_page(html: str, year: int) -> dict:
    """Pull {date_iso: {"target", "fact"}} for every day block that has its
    target rendered as text. Past/revealed days are reliable; the *current*
    day is usually just an image, so it won't appear until it's past — which
    is why we cache what we can scrape."""
    html = (html.replace("&ndash;", "-").replace("&rsquo;", "'")
                .replace("&nbsp;", " ").replace("&amp;", "&"))
    labels = [(m.start(), int(m.group(1)), m.group(2))
              for m in DAY_BLOCK_RE.finditer(html)]
    found = {}
    for m in TARGET_NUM_RE.finditer(html):
        preceding = [lbl for lbl in labels if lbl[0] < m.start()]
        if not preceding:
            continue
        _, day_num, month_name = preceding[-1]
        month = MONTHS.get(month_name.capitalize())
        if not month:
            continue
        try:
            iso = date(year, month, day_num).isoformat()
        except ValueError:
            continue
        fm = FACT_RE.search(html, m.end())
        fact = (fm.group(0).strip().rstrip(",").rstrip(".")
                if fm and fm.start() - m.end() < 260 else None)
        found[iso] = {"target": int(m.group(1).replace(",", "")), "fact": fact}
    return found


def fetch_daily_target(today: date) -> tuple[str, int | None, str | None]:
    """Resolve today's push-up target, returning (status, target, fact) where
    status is "ok" / "rest" / "unknown".

    Targets come from the hardcoded official 2026 schedule (DAILY_TARGETS_2026).
    We still scrape the daily-facts page to harvest the mental-health *fact*
    text into the cache (it's nice in the summary) and as a fallback for dates
    outside the hardcoded table."""
    iso = today.isoformat()
    schedule = load_schedule()

    # Best-effort scrape — only to enrich the fact cache; never required.
    html = None
    try:
        req = urllib.request.Request(FACTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        html = None
    if html:
        changed = False
        for d_iso, entry in extract_targets_from_page(html, today.year).items():
            if entry["target"] and schedule.get(d_iso, {}).get("target") != entry["target"]:
                schedule[d_iso] = entry
                changed = True
        if changed:
            save_schedule(schedule)

    # Hardcoded official schedule is authoritative for the target.
    if iso in DAILY_TARGETS_2026:
        target = DAILY_TARGETS_2026[iso]
        if target == 0:
            return "rest", None, None
        return "ok", target, schedule.get(iso, {}).get("fact")

    # Outside the hardcoded table — fall back to whatever we've scraped.
    entry = schedule.get(iso)
    if entry and entry.get("target"):
        return "ok", entry["target"], entry.get("fact")
    return "unknown", None, None


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


def set_target_cli() -> int:
    """`--set-target N [--date YYYY-MM-DD]` — manually record a day's target
    (e.g. read off the app) into the shared schedule. Defaults to today."""
    args = sys.argv
    value = int(args[args.index("--set-target") + 1])
    iso = (args[args.index("--date") + 1] if "--date" in args
           else date.today().isoformat())
    date.fromisoformat(iso)  # validate
    schedule = load_schedule()
    fact = schedule.get(iso, {}).get("fact")
    schedule[iso] = {"target": value, "fact": fact}
    save_schedule(schedule)
    print(f"Set target for {iso}: {value} push-ups")
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    summary_mode = "--summary" in sys.argv

    if "--set-target" in sys.argv:
        return set_target_cli()

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
