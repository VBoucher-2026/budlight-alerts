ç#!/usr/bin/env python3
"""BudLight Cup alerts: ESPN fantasy hockey (H2H points) -> ntfy push.

Modes:
  summary  daily: matchup pace, lineup fixes, best pickups for this week, trending, league moves
  lineup   pre-game: pushes only if your active lineup needs a change
  steals   hourly: pushes only if another team dropped someone better than your weakest player
"""
import json
import os
import sys
import time

import requests
from espn_api.hockey import League
from espn_api.hockey.constant import PRO_TEAM_MAP
from espn_api.hockey.player import Player

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
TEAM_ID = int(os.environ["TEAM_ID"])
YEAR = int(os.environ["YEAR"])
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["ESPN_SWID"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

HARD_OUT = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
STATUS_TEXT = {"OUT": "out", "INJURY_RESERVE": "on IR", "SUSPENSION": "suspended", "DAY_TO_DAY": "day-to-day"}
POS = {"Center": "C", "Left Wing": "LW", "Right Wing": "RW", "Forward": "F", "Defense": "D", "Goalie": "G"}
PRIOR_GAMES = 5       # blend actual FP/G with ESPN's projection as if it were 5 extra games (fades as games pile up)
GOALIE_SHARE = 0.6    # assume a goalie starts ~60% of his team's games
TOP_SKATERS, TOP_GOALIES, TOP_TRENDING = 5, 2, 3
STEALS_WINDOW_MIN = 65


# ---------- helpers ----------
def notify(title, body, priority="default", tags="ice_hockey"):
    r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body[:3900].encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=20)
    r.raise_for_status()


def short_team(name):
    return "Utah" if str(name).startswith("Utah") else str(name).split()[-1]


def group_of(position):
    return {"Goalie": "G", "Defense": "D"}.get(position, "F")


def section(name, fn):
    try:
        return fn()
    except Exception as e:  # one broken section shouldn't kill the whole recap
        return [f"(couldn't load {name}: {type(e).__name__})"]


NAME_TO_PRO_ID = {v: k for k, v in PRO_TEAM_MAP.items()}


# ---------- data ----------
def fetch_players(league, statuses, limit):
    """Raw ESPN player pool with this league's fantasy-point averages."""
    filters = {"players": {"filterStatus": {"value": statuses}, "limit": limit,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
    data = league.espn_request.league_get(
        params={"view": "kona_player_info", "scoringPeriodId": league.current_week},
        headers={"x-fantasy-filter": json.dumps(filters)})
    out = {}
    for entry in data.get("players", []):
        info = entry.get("player", {})
        p = Player(entry)
        splits = {s.get("id"): s for s in info.get("stats", [])}
        season = splits.get(f"00{YEAR}", {})
        stats = season.get("stats") or {}
        gp = stats.get("34") or stats.get("0") or 0
        actual = season.get("appliedAverage") or 0
        proj = splits.get(f"10{YEAR}", {}).get("appliedAverage") or 0
        avg = (gp * actual + PRIOR_GAMES * proj) / (gp + PRIOR_GAMES) if proj else actual
        own = info.get("ownership", {}) or {}
        pid = info.get("id") or entry.get("id")
        out[pid] = {
            "id": pid, "name": p.name, "group": group_of(p.position), "pos": POS.get(p.position, "?"),
            "team": short_team(p.proTeam), "pro_id": info.get("proTeamId"),
            "status": p.injuryStatus or "ACTIVE", "avg": avg, "gp": gp,
            "last7": splits.get(f"01{YEAR}", {}).get("appliedAverage"),
            "owned": own.get("percentOwned", 0) or 0, "change": own.get("percentChange", 0) or 0,
            "avail": entry.get("status"), "on_team": entry.get("onTeamId"),
        }
    return out


class Schedule:
    """Remaining NHL games in the current fantasy matchup period, plus lineup slot counts."""

    def __init__(self, league):
        settings = league.espn_request.league_get(params={"view": "mSettings"})["settings"]
        periods = settings.get("scheduleSettings", {}).get("matchupPeriods", {})
        days = sorted(int(d) for d in periods.get(str(league.currentMatchupPeriod), []))
        self.today = league.current_week
        source = "settings"
        self.first_day_override = None
        if not days or days[-1] < self.today:
            # fallback: assume the matchup runs through Sunday (ESPN default, one scoring period per day)
            import datetime as _dt
            days_to_sunday = 6 - _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-5))).weekday()
            days = list(range(self.today, self.today + days_to_sunday + 1))
            source = "fallback-to-Sunday"
            self.first_day_override = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-5))).weekday() == 0
        self.first_day = (self.first_day_override if self.first_day_override is not None
                          else bool(days) and self.today == days[0])
        counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
        c = lambda i: int(counts.get(str(i), 0))
        self.slots = {"F": c(0) + c(1) + c(2) + c(3), "D": c(4), "G": c(5), "U": c(6), "IR": c(8)}
        now_ms = time.time() * 1000
        self.playing = {}
        for tid, games in league._get_all_pro_schedule().items():
            for sp in days:
                if sp < self.today:
                    continue
                for g in games.get(str(sp)) or []:
                    if g.get("date", 0) > now_ms:
                        self.playing.setdefault(sp, set()).add(int(tid))
        for sp in days:
            if sp >= self.today:
                self.playing.setdefault(sp, set())
        print(f"[schedule] matchup period {league.currentMatchupPeriod}, today=scoring period {self.today}, "
              f"days={days} ({source}), slots={self.slots}, "
              f"NHL teams still to play per day={ {k: len(v) for k, v in sorted(self.playing.items())} }")

    def games_left(self, pro_id):
        return sum(pro_id in teams for teams in self.playing.values())

    def plays_today(self, pro_id):
        return pro_id in self.playing.get(self.today, set())


def healthy(p):
    return p["status"] not in HARD_OUT


def week_value(p, sched):
    share = GOALIE_SHARE if p["group"] == "G" else 1
    return p["avg"] * sched.games_left(p["pro_id"]) * share


def lineups(league, players):
    """Today's roster + slots for every team, enriched with FP/G."""
    teams = {}
    for box in league.box_scores(matchup_total=False):
        for side in ("home", "away"):
            team = getattr(box, f"{side}_team")
            tid = getattr(team, "team_id", None)
            if tid is None:
                continue
            roster = []
            for bp in getattr(box, f"{side}_lineup"):
                rec = dict(players.get(bp.playerId) or {})
                rec.update({"id": bp.playerId, "name": bp.name, "slot": bp.slot_position,
                            "eligible": bp.eligibleSlots, "status": bp.injuryStatus or "ACTIVE",
                            "pro_id": NAME_TO_PRO_ID.get(bp.proTeam), "team": short_team(bp.proTeam),
                            "group": group_of(bp.position), "pos": POS.get(bp.position, "?")})
                rec.setdefault("avg", 0)
                roster.append(rec)
            teams[tid] = {"team": team, "roster": roster}
    return teams


def remaining_value(roster, sched):
    """Usable games and expected points left this matchup, respecting daily slot limits."""
    games = points = 0
    s = sched.slots
    for teams in sched.playing.values():
        pool = [p for p in roster if p["slot"] != "IR" and healthy(p) and p.get("pro_id") in teams]
        overflow = []
        for g in ("F", "D", "G"):
            grp = sorted((p for p in pool if p["group"] == g), key=lambda p: p["avg"], reverse=True)
            used, extra = grp[:s[g]], grp[s[g]:]
            games += len(used)
            points += sum(p["avg"] for p in used)
            if g != "G":
                overflow += extra
        util = sorted(overflow, key=lambda p: p["avg"], reverse=True)[:s["U"]]
        games += len(util)
        points += sum(p["avg"] for p in util)
    return games, points


def weakest(roster, group):
    cands = [p for p in roster if p["group"] == group and p["slot"] != "IR" and healthy(p)]
    return min(cands, key=lambda p: p["avg"]) if cands else None


# ---------- sections ----------
def matchup_section(league, sched, teams):
    me = teams.get(TEAM_ID)
    box = next((b for b in league.box_scores(matchup_total=True)
                if TEAM_ID in (getattr(b.home_team, "team_id", None), getattr(b.away_team, "team_id", None))), None)
    if not me or not box:
        return ["No matchup this period."]
    side = "home" if getattr(box.home_team, "team_id", None) == TEAM_ID else "away"
    other = "away" if side == "home" else "home"
    opp_team = getattr(box, f"{other}_team")
    if not getattr(opp_team, "team_name", None):
        return ["Bye this week."]
    ms, os_ = getattr(box, f"{side}_score"), getattr(box, f"{other}_score")
    lines = [f"You {ms:.1f} vs {opp_team.team_name} {os_:.1f} ({ms - os_:+.1f})"]

    my_g, my_p = remaining_value(me["roster"], sched)
    opp = teams.get(opp_team.team_id)
    op_g, op_p = remaining_value(opp["roster"], sched) if opp else (0, 0)
    all_games = sorted((remaining_value(t["roster"], sched)[0] for t in teams.values()), reverse=True)
    rank = all_games.index(my_g) + 1 if my_g in all_games else "?"
    lines.append(f"Games left (usable): you {my_g} vs {op_g} · you rank {rank}/{len(all_games)}")
    fin_me, fin_op = ms + my_p, os_ + op_p
    lines.append(f"Pace to finish: you ~{fin_me:.0f} vs ~{fin_op:.0f} ({fin_me - fin_op:+.0f})")
    if fin_me < fin_op and my_g:
        lines.append(f"Behind pace: you need ~{(fin_op - fin_me) / my_g:.1f} more pts per game, add volume below")
    return lines


def lineup_section(sched, teams, fas):
    me = teams.get(TEAM_ID)
    if not me:
        return []
    roster = me["roster"]
    active = [p for p in roster if p["slot"] not in ("Bench", "IR")]
    bench = sorted((p for p in roster if p["slot"] == "Bench" and healthy(p) and sched.plays_today(p["pro_id"])),
                   key=lambda p: p["avg"], reverse=True)
    used, lines = set(), []
    for p in active:
        st = p["status"]
        if st == "DAY_TO_DAY":
            lines.append(f"🤕 {p['name']} is day-to-day, check his status before puck drop")
            continue
        if st in HARD_OUT:
            problem = f"is {STATUS_TEXT[st]}"
        elif sched.playing.get(sched.today) and not sched.plays_today(p["pro_id"]):
            problem = "has no game today"
        else:
            continue
        sub = next((b for b in bench if b["id"] not in used and p["slot"] in (b.get("eligible") or [])), None)
        if sub:
            used.add(sub["id"])
            lines.append(f"🔁 {p['name']} {problem} → start {sub['name']} ({sub['team']}, plays today)")
        elif st in HARD_OUT:
            fa = max((f for f in fas.values() if f["group"] == p["group"] and healthy(f)
                      and f["avail"] == "FREEAGENT" and sched.plays_today(f["pro_id"])),
                     key=lambda f: f["avg"], default=None)
            tip = f" Best FA playing today: {fa['name']} ({fa['team']}, {fa['avg']:.1f} FP/G)" if fa else ""
            lines.append(f"🚑 {p['name']} {problem} and no bench player can replace him.{tip}")
    ir_used = sum(p["slot"] == "IR" for p in roster)
    ir_ready = [p for p in roster if p["slot"] != "IR" and p["status"] in ("OUT", "INJURY_RESERVE")]
    if ir_ready and ir_used < sched.slots["IR"]:
        lines.append(f"🏥 Move {ir_ready[0]['name']} to IR to free a roster spot for a pickup")
    return lines


def fa_line(f, sched):
    hot = f" (last 7: {f['last7']:.1f})" if f.get("last7") and abs(f["last7"] - f["avg"]) >= 0.3 else ""
    waiv = " [waivers]" if f["avail"] == "WAIVERS" else ""
    return f"{f['name']} ({f['pos']}, {f['team']}) {f['avg']:.1f} FP/G{hot} × {sched.games_left(f['pro_id'])}g{waiv}"


def pickups_section(sched, teams, fas):
    me = teams.get(TEAM_ID)
    if not me:
        return []
    rows = []
    for f in fas.values():
        if not healthy(f) or not f["avg"]:
            continue
        drop = weakest(me["roster"], f["group"])
        if not drop:
            continue
        gain = week_value(f, sched) - week_value(drop, sched)
        if gain <= 0.5:
            continue
        star = " ⭐" if f["avg"] > drop["avg"] + 0.3 else ""
        rows.append((gain, f["group"], f"• +{gain:.1f} · {fa_line(f, sched)}{star} · drop {drop['name']}"))
    rows.sort(key=lambda r: r[0], reverse=True)
    skaters = [r[2] for r in rows if r[1] != "G"][:TOP_SKATERS]
    goalies = [r[2] for r in rows if r[1] == "G"][:TOP_GOALIES]
    if not skaters + goalies:
        return []
    legend = "(+pts gained this week vs the drop · ⭐ = also better long-term)"
    return [legend] + skaters + goalies


def trending_section(fas):
    hot = sorted((f for f in fas.values() if healthy(f) and f["change"] > 0),
                 key=lambda f: f["change"], reverse=True)[:TOP_TRENDING]
    return [f"• {f['name']} ({f['pos']}, {f['team']}) +{f['change']:.1f}% owned this wk → {f['owned']:.0f}%, "
            f"{f['avg']:.1f} FP/G" for f in hot]


def preview_section(sched):
    counts = {}
    for teams in sched.playing.values():
        for t in teams:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return []
    top = max(counts.values())
    low = min(counts.values())
    heavy = sorted(short_team(PRO_TEAM_MAP.get(t, "?")) for t, n in counts.items() if n == top)
    light = sorted(short_team(PRO_TEAM_MAP.get(t, "?")) for t, n in counts.items() if n == low)
    return [f"{top} games: {', '.join(heavy)}", f"Only {low}: {', '.join(light)}"]


def drops(league, sched, teams, fas, window_min):
    """Players dropped by other teams recently who'd beat your weakest at their position."""
    me = teams.get(TEAM_ID)
    by_name = {f["name"]: f for f in fas.values()}
    cutoff = (time.time() - window_min * 60) * 1000
    moves, steals = [], []
    for act in league.recent_activity(size=25):
        if act.date < cutoff:
            continue
        for team, action, player, *_ in act.actions:
            tname = getattr(team, "team_name", team)
            pname = getattr(player, "name", player)
            moves.append(f"• {tname}: {action} {pname}")
            f = by_name.get(pname)
            if "DROP" in str(action) and f and me and getattr(team, "team_id", None) != TEAM_ID:
                w = weakest(me["roster"], f["group"])
                if w and f["avg"] > w["avg"] + 0.3:
                    steals.append(f"🚨 {tname} dropped {fa_line(f, sched)}, better than your "
                                  f"{w['name']} ({w['avg']:.1f} FP/G)")
    return moves, steals



# ---------- main ----------
def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "summary").lower()
    try:
        league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
        sched = Schedule(league)
        fas = fetch_players(league, ["FREEAGENT", "WAIVERS"], 250)
        rostered = fetch_players(league, ["ONTEAM"], 400)
        teams = lineups(league, rostered)
    except Exception as e:
        notify("BudLight alerts: couldn't load ESPN",
               f"{type(e).__name__}: {e}\n\nIf this repeats, the espn_s2 / SWID cookies have probably "
               "expired. Grab new ones and update the GitHub secrets.", "high", "warning")
        raise

    if mode == "lineup":
        issues = lineup_section(sched, teams, fas)
        if issues:
            notify("Fix your lineup before puck drop", "\n\n".join(issues), "high", "rotating_light")
        print(f"Lineup check: {len(issues)} issue(s)")
        return

    if mode == "steals":
        _, steals = drops(league, sched, teams, fas, STEALS_WINDOW_MIN)
        if steals:
            notify("Grab him before your friends do", "\n\n".join(steals), "high", "rotating_light")
        print(f"Steals check: {len(steals)} alert(s)")
        return

    moves_steals = section("league moves", lambda: drops(league, sched, teams, fas, 24 * 60))
    moves, steals = moves_steals if isinstance(moves_steals, tuple) else (moves_steals, [])
    parts = ["🏒 MATCHUP", *section("matchup", lambda: matchup_section(league, sched, teams))]
    if sched.first_day:
        parts += ["", "📅 THIS WEEK'S SCHEDULE", *section("schedule", lambda: preview_section(sched))]
    parts += ["", "⚠️ LINEUP TODAY", *(section("lineup", lambda: lineup_section(sched, teams, fas)) or ["All good."])]
    parts += ["", "📈 BEST PICKUPS (this week)",
              *(section("pickups", lambda: pickups_section(sched, teams, fas)) or ["No upgrade worth a move."])]
    parts += ["", "🔥 TRENDING ON ESPN", *(section("trending", lambda: trending_section(fas)) or ["Nothing notable."])]
    parts += ["", "🔄 LEAGUE MOVES (24h)", *(steals + moves or ["None."])]
    notify("BudLight Cup daily", "\n".join(parts))
    print("Summary sent.")


if __name__ == "__main__":
    main()
