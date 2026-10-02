#!/usr/bin/env python3
"""BudLight Cup alerts: reads the ESPN fantasy hockey league and pushes alerts via ntfy.

Modes:
  summary  daily recap: matchup score, lineup problems, top free agents, league moves (last 24h)
  lineup   pre-game check: pushes only if something in the active lineup needs fixing
"""
import json
import os
import sys
import time

import requests
from espn_api.hockey import League
from espn_api.hockey.player import Player

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
TEAM_ID = int(os.environ["TEAM_ID"])
YEAR = int(os.environ["YEAR"])
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["ESPN_SWID"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

PROBLEM_STATUS = {"OUT", "INJURY_RESERVE", "DAY_TO_DAY", "SUSPENSION"}
INACTIVE_SLOTS = {"Bench", "IR"}
TOP_SKATERS, TOP_GOALIES = 5, 2


def notify(title, body, priority="default", tags="ice_hockey"):
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body[:3900].encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        timeout=20,
    )
    r.raise_for_status()


def label(status):
    return (status or "").replace("_", " ").title()


def short_team(name):
    return "Utah" if str(name).startswith("Utah") else str(name).split()[-1]


def healthy(p):
    return (p.injuryStatus or "ACTIVE") not in PROBLEM_STATUS


def my_box(league, matchup_total):
    for box in league.box_scores(matchup_total=matchup_total):
        for side in ("home", "away"):
            if getattr(getattr(box, f"{side}_team"), "team_id", None) == TEAM_ID:
                return box, side
    return None, None


def lineup_issues(league):
    """Injured/out players in active slots, and idle starters while bench players have a game."""
    box, side = my_box(league, matchup_total=False)  # today's roster and today's NHL games
    if not box:
        return []
    roster = getattr(box, f"{side}_lineup")
    active = [p for p in roster if p.slot_position not in INACTIVE_SLOTS]
    bench = [p for p in roster if p.slot_position == "Bench"]
    plays_today = lambda p: p.pro_opponent not in ("None", None)

    issues = [
        f"🚑 {p.name} ({p.slot_position}) is {label(p.injuryStatus)} but in your active lineup"
        for p in active if not healthy(p)
    ]
    idle = [p for p in active if healthy(p) and not plays_today(p)]
    available = [p for p in bench if healthy(p) and plays_today(p)]
    if idle and available:
        issues.append(
            "🔁 No game today but in your lineup: " + ", ".join(p.name for p in idle)
            + "\nOn your bench with a game today: " + ", ".join(p.name for p in available)
        )
    return issues


def matchup_summary(league):
    box, side = my_box(league, matchup_total=True)
    if not box:
        return ["No matchup this period."]
    other = "away" if side == "home" else "home"
    opp = getattr(box, f"{other}_team")
    if not getattr(opp, "team_name", None):
        return ["Bye this week."]
    me = getattr(box, f"{side}_team")
    ms, os_ = getattr(box, f"{side}_score"), getattr(box, f"{other}_score")
    mp, op = getattr(box, f"{side}_projected"), getattr(box, f"{other}_projected")
    lines = [f"{me.team_name} {ms:.1f} vs {opp.team_name} {os_:.1f} ({ms - os_:+.1f})"]
    if mp >= 0 and op >= 0:
        lines.append(f"Projected: {mp:.1f} vs {op:.1f} ({mp - op:+.1f})")
    return lines


def top_free_agents(league):
    """Healthy free agents ranked by fantasy points per game under this league's scoring."""
    params = {"view": "kona_player_info", "scoringPeriodId": league.current_week}
    filters = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "limit": 150,
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
    }}
    data = league.espn_request.league_get(
        params=params, headers={"x-fantasy-filter": json.dumps(filters)}
    )
    ranked = []
    for raw in data.get("players", []):
        p = Player(raw)
        if not healthy(p):
            continue
        info = raw.get("player", {})
        splits = {s.get("id"): s for s in info.get("stats", [])}
        avg, basis = splits.get(f"00{YEAR}", {}).get("appliedAverage"), "FP/G"
        if not avg:  # early season: fall back to ESPN's projection
            avg, basis = splits.get(f"10{YEAR}", {}).get("appliedAverage"), "proj FP/G"
        if not avg:
            continue
        last7 = splits.get(f"01{YEAR}", {}).get("appliedAverage")
        owned = info.get("ownership", {}).get("percentOwned", 0) or 0
        ranked.append((avg, p, basis, last7, owned))
    ranked.sort(key=lambda r: r[0], reverse=True)

    def fmt(r):
        avg, p, basis, last7, owned = r
        hot = f", last 7: {last7:.2f}" if last7 else ""
        return f"• {p.name} ({p.position[0]}, {short_team(p.proTeam)}) {avg:.2f} {basis}{hot}, {owned:.0f}% owned"

    skaters = [fmt(r) for r in ranked if r[1].position != "Goalie"][:TOP_SKATERS]
    goalies = [fmt(r) for r in ranked if r[1].position == "Goalie"][:TOP_GOALIES]
    return skaters + goalies


def league_moves(league):
    cutoff_ms = (time.time() - 24 * 3600) * 1000
    lines = []
    for activity in league.recent_activity(size=25):
        if activity.date < cutoff_ms:
            continue
        for team, action, player, *_ in activity.actions:
            lines.append(f"• {getattr(team, 'team_name', team)}: {action} {getattr(player, 'name', player)}")
    return lines


def section(name, fn):
    try:
        return fn()
    except Exception as e:  # one broken section shouldn't kill the whole recap
        return [f"(couldn't load {name}: {type(e).__name__})"]


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "summary").lower()
    try:
        league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
    except Exception as e:
        notify(
            "BudLight alerts: ESPN login failed",
            f"{type(e).__name__}: {e}\n\nIf this repeats, the espn_s2 / SWID cookies have "
            "probably expired. Grab new ones and update the GitHub secrets.",
            "high", "warning",
        )
        raise

    if mode == "lineup":
        try:
            issues = lineup_issues(league)
        except Exception as e:
            notify("BudLight alerts: lineup check failed", f"{type(e).__name__}: {e}", "default", "warning")
            raise
        if issues:
            notify("Fix your lineup before puck drop", "\n\n".join(issues), "high", "rotating_light")
        print(f"Lineup check done: {len(issues)} issue(s).")
        return

    parts = ["🏒 MATCHUP", *section("matchup", lambda: matchup_summary(league))]
    parts += ["", "⚠️ LINEUP", *(section("lineup", lambda: lineup_issues(league)) or ["All good."])]
    parts += ["", "📈 TOP FREE AGENTS", *(section("free agents", lambda: top_free_agents(league)) or ["None found."])]
    parts += ["", "🔄 LEAGUE MOVES (24h)", *(section("league moves", lambda: league_moves(league)) or ["None."])]
    notify("BudLight Cup daily", "\n".join(parts))
    print("Summary sent.")


if __name__ == "__main__":
    main()
