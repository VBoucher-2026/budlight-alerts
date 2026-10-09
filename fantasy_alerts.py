#!/usr/bin/env python3
"""BudLight Cup alerts, streaming edition (ESPN fantasy hockey, H2H points, no-bench roster).

Runs hourly (GitHub Actions) and decides what to send from Montreal local time:
  Monday 7 AM   weekly plan: games per player, dead spots, streaming calendar
  every day 10AM daily brief: matchup pace + today's add/drop (only concrete moves)
  every hour    instant alerts: IR-eligible player, IR player ready to activate, injury/suspension
                status changes, a league-mate drops someone better than your stream slot
State (what was already alerted) is kept in state.json and committed by the workflow.
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("America/Toronto")
STATE_PATH = "state.json"

# --- tuning knobs ---
STREAM_SLOTS = 2        # your N lowest-value healthy skaters are the only ones ever suggested as drops
WEEKLY_ADDS = 7         # league limit per matchup week
ADDS_RESERVE = 2        # keep this many adds unused for emergencies (injuries)
MIN_GAIN = 3.0          # suggest a stream only if it gains at least this many points over the rest of the week
MIN_QUALITY = 0.85      # the pickup must project to at least 85% of the dropped player's per-game value
PRIOR_GAMES = 10        # blend actual FP/G with ESPN's projection as if the projection were 10 games
GOALIE_SHARE = 0.65
STEAL_MARGIN = 0.3
BASELINE = {"F": 1.2, "D": 1.0, "G": 3.0}   # per-game value assumed for players ESPN doesn't project

HARD_OUT = {"OUT", "INJURY_RESERVE", "SUSPENSION"}
STATUS_TEXT = {"OUT": "out", "INJURY_RESERVE": "on IR", "SUSPENSION": "suspended",
               "DAY_TO_DAY": "day-to-day", "ACTIVE": "active"}
POS = {"Center": "C", "Left Wing": "LW", "Right Wing": "RW", "Forward": "F", "Defense": "D", "Goalie": "G"}
NAME_TO_PRO_ID = {v: k for k, v in PRO_TEAM_MAP.items()}


# ---------- helpers ----------
def notify(title, body, priority="high", tags="ice_hockey"):
    r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body[:3900].encode("utf-8"),
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags},
                      timeout=20)
    r.raise_for_status()
    try:
        msg_id = r.json().get("id")
    except Exception:
        msg_id = "?"
    print(f"[sent id={msg_id} priority={priority}] {title}\n{body}\n")


def short_team(name):
    return "Utah" if str(name).startswith("Utah") else str(name).split()[-1]


def group_of(position):
    return {"Goalie": "G", "Defense": "D"}.get(position, "F")


def healthy(p):
    return p["status"] not in HARD_OUT


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ---------- data ----------
def fetch_players(league, statuses, limit):
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
        proj = splits.get(f"10{YEAR}", {})
        stats = season.get("stats") or {}
        gp = stats.get("34") or stats.get("0") or 0
        actual = season.get("appliedAverage") or 0
        proj_avg = proj.get("appliedAverage") or 0
        # No ESPN projection = fringe player: shrink toward a low baseline so 2 hot games can't make him look like a star
        prior = proj_avg or BASELINE[group_of(p.position)]
        value = (gp * actual + PRIOR_GAMES * prior) / (gp + PRIOR_GAMES)
        pid = info.get("id") or entry.get("id")
        out[pid] = {"id": pid, "name": p.name, "group": group_of(p.position), "pos": POS.get(p.position, "?"),
                    "team": short_team(p.proTeam), "pro_id": info.get("proTeamId"),
                    "status": p.injuryStatus or "ACTIVE", "value": value, "gp": gp,
                    "proj_total": proj.get("appliedTotal") or 0,
                    "avail": entry.get("status"), "on_team": entry.get("onTeamId"),
                    "eligible": [], "slot": None}
    return out


class Schedule:
    def __init__(self, league):
        settings = league.espn_request.league_get(params={"view": "mSettings"})["settings"]
        periods = settings.get("scheduleSettings", {}).get("matchupPeriods", {})
        days = sorted(int(d) for d in periods.get(str(league.currentMatchupPeriod), []))
        self.today = league.current_week
        now_local = dt.datetime.now(TZ)
        if not days or days[-1] < self.today:
            days = list(range(self.today, self.today + (6 - now_local.weekday()) + 1))
        self.days = [d for d in days if d >= self.today]
        counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
        c = lambda i: int(counts.get(str(i), 0))
        self.slots = {"F": c(0) + c(1) + c(2) + c(3), "D": c(4), "G": c(5), "U": c(6), "IR": c(8)}
        now_ms = time.time() * 1000
        self.playing = {sp: set() for sp in self.days}      # games not started yet
        self.all_games = {sp: set() for sp in self.days}    # all games that day
        self.day_name = {}
        for tid, games in league._get_all_pro_schedule().items():
            for sp in self.days:
                for g in games.get(str(sp)) or []:
                    self.all_games[sp].add(int(tid))
                    if g.get("date", 0) > now_ms:
                        self.playing[sp].add(int(tid))
                    if sp not in self.day_name and g.get("date"):
                        self.day_name[sp] = dt.datetime.fromtimestamp(g["date"] / 1000, TZ).strftime("%a")
        print(f"[schedule] period {league.currentMatchupPeriod} today=sp{self.today} days={self.days} "
              f"teams still to play={ {self.day_name.get(k, k): len(v) for k, v in self.playing.items()} }")

    def games_left(self, pro_id):
        return sum(pro_id in s for s in self.playing.values())

    def games_week(self, pro_id):
        return sum(pro_id in s for s in self.all_games.values())

    def plays_today(self, pro_id):
        return pro_id in self.all_games.get(self.today, set())

    def days_left_for(self, pro_id):
        return [self.day_name.get(sp, str(sp)) for sp, s in self.playing.items() if pro_id in s]


def lineups(league, players):
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
                            "eligible": bp.eligibleSlots or [], "status": bp.injuryStatus or "ACTIVE",
                            "pro_id": NAME_TO_PRO_ID.get(bp.proTeam), "team": short_team(bp.proTeam),
                            "group": group_of(bp.position), "pos": POS.get(bp.position, "?")})
                rec.setdefault("value", 0)
                roster.append(rec)
            teams[tid] = {"team": team, "roster": roster}
    return teams


def week_points(p, sched):
    share = GOALIE_SHARE if p["group"] == "G" else 1
    return p["value"] * sched.games_left(p["pro_id"]) * share


def remaining_value(roster, sched):
    games = points = 0
    s = sched.slots
    for teams in sched.playing.values():
        pool = [p for p in roster if p["slot"] != "IR" and healthy(p) and p.get("pro_id") in teams]
        overflow = []
        for g in ("F", "D", "G"):
            grp = sorted((p for p in pool if p["group"] == g), key=lambda p: p["value"], reverse=True)
            used, extra = grp[:s[g]], grp[s[g]:]
            games += len(used)
            points += sum(p["value"] for p in used)
            if g != "G":
                overflow += extra
        util = sorted(overflow, key=lambda p: p["value"], reverse=True)[:s["U"]]
        games += len(util)
        points += sum(p["value"] for p in util)
    return games, points


def stream_slots(roster):
    """Your N lowest-value healthy skaters, the only ones ever suggested as drops."""
    skaters = [p for p in roster if p["group"] != "G" and p["slot"] != "IR" and healthy(p)]
    return sorted(skaters, key=lambda p: p["value"])[:STREAM_SLOTS]


def adds_used(league):
    """Adds counted by ESPN for the current matchup period (same number the app shows)."""
    try:
        data = league.espn_request.league_get(params={"view": "mTeam"})
        for t in data.get("teams", []):
            if t.get("id") == TEAM_ID:
                totals = (t.get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {}
                return int(totals.get(str(league.currentMatchupPeriod), 0))
    except Exception as e:
        print(f"[adds] ESPN counter unavailable ({type(e).__name__}), estimating from activity")
    now = dt.datetime.now(TZ)
    monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = monday.timestamp() * 1000
    n = 0
    for act in league.recent_activity(size=50):
        if act.date < cutoff:
            continue
        for team, action, *_ in act.actions:
            if getattr(team, "team_id", None) == TEAM_ID and "ADDED" in str(action):
                n += 1
    return n


# ---------- recommendations ----------
def fa_desc(f, sched):
    return f"{f['name']} ({f['pos']}, {f['team']}, {f['value']:.1f}/g, plays {', '.join(sched.days_left_for(f['pro_id'])) or 'no more games'})"


def stream_moves(me, fas, sched, budget):
    """Best add/drop for each stream slot. Returns list of (gain, text)."""
    moves = []
    if budget <= 0:
        return moves
    for occ in stream_slots(me["roster"]):
        best = None
        for f in fas.values():
            if f["group"] != occ["group"] or not healthy(f) or f["avail"] != "FREEAGENT" or not f["proj_total"]:
                continue  # only recommend players ESPN projects for the season
            if f["value"] < MIN_QUALITY * occ["value"]:
                continue
            gain = week_points(f, sched) - week_points(occ, sched)
            if gain >= MIN_GAIN and (best is None or gain > best[0]):
                best = (gain, f)
        if best:
            gain, f = best
            occ_days = ", ".join(sched.days_left_for(occ["pro_id"])) or "done for the week"
            moves.append((gain, f"ADD {fa_desc(f, sched)}\nDROP {occ['name']} ({occ['value']:.1f}/g, {occ_days})\n"
                                f"≈ +{gain:.0f} pts this week"))
    moves.sort(key=lambda m: m[0], reverse=True)
    return moves[:budget]


def dead_spots(me, sched):
    out = []
    for p in me["roster"]:
        if p["slot"] == "IR":
            continue
        if not healthy(p):
            out.append(f"{p['name']} is {STATUS_TEXT.get(p['status'], p['status'].lower())}")
        elif p["status"] == "DAY_TO_DAY":
            out.append(f"{p['name']} is day-to-day")
    return out


def ir_actions(me, fas, sched):
    """(a) players who can move to IR now, (b) IR players ready to come back."""
    to_ir = [p for p in me["roster"] if p["slot"] != "IR" and "IR" in p["eligible"]]
    free_ir = sched.slots["IR"] - sum(p["slot"] == "IR" for p in me["roster"])
    ready = [p for p in me["roster"] if p["slot"] == "IR" and p["status"] == "ACTIVE"]
    lines = []
    for p in to_ir[:max(free_ir, 0)]:
        lines.append(f"🏥 {p['name']} is IR-eligible: move him to IR (frees a roster spot), then add a {p['group']}")
    for p in ready:
        drop = next(iter(sorted((q for q in me["roster"] if q["group"] == p["group"] and q["slot"] != "IR"),
                                key=lambda q: q["value"])), None)
        tip = f", drop {drop['name']} ({drop['value']:.1f}/g)" if drop else ""
        lines.append(f"✅ {p['name']} is active again: move him off IR{tip}")
    return lines


def matchup_lines(league, sched, teams):
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
    my_g, my_p = remaining_value(me["roster"], sched)
    opp = teams.get(opp_team.team_id)
    op_g, op_p = remaining_value(opp["roster"], sched) if opp else (0, 0)
    return [f"You {ms:.1f} vs {opp_team.team_name} {os_:.1f} ({ms - os_:+.1f}) · games left {my_g} vs {op_g} · "
            f"pace ~{ms + my_p:.0f} vs ~{os_ + op_p:.0f}"]


# ---------- the three alerts ----------
def weekly_plan(league, sched, teams, fas):
    me = teams[TEAM_ID]
    used = adds_used(league)
    budget = WEEKLY_ADDS - ADDS_RESERVE - used
    by_games = {}
    for p in me["roster"]:
        if p["slot"] == "IR":
            continue
        by_games.setdefault(sched.games_week(p["pro_id"]), []).append(p["name"].split()[-1])
    parts = ["📅 GAMES THIS WEEK"]
    for n in sorted(by_games, reverse=True):
        parts.append(f"{n} games: {', '.join(sorted(by_games[n]))}")
    heavy = sorted(short_team(PRO_TEAM_MAP.get(t, "?")) for t in set().union(*sched.all_games.values())
                   if sched.games_week(t) >= 4)
    if heavy:
        parts.append(f"4-game NHL teams: {', '.join(heavy)}")
    dead = dead_spots(me, sched)
    parts += ["", "⚠️ DEAD SPOTS", *(dead or ["None"])]
    parts += ["", f"🔁 STREAMING PLAN (adds left {WEEKLY_ADDS - used}/{WEEKLY_ADDS}, keeping {ADDS_RESERVE} in reserve)"]
    moves = stream_moves(me, fas, sched, budget)
    parts += [m[1] for m in moves] or ["No stream worth making this week. Your lowest-value skaters: "
                                        + ", ".join(f"{p['name']} ({p['value']:.1f}/g)" for p in stream_slots(me["roster"]))]
    parts += ["", "🏒 MATCHUP", *matchup_lines(league, sched, teams)]
    notify("Weekly plan: " + (f"{len(moves)} move(s)" if moves else "no moves"), "\n".join(parts), "high")


def daily_brief(league, sched, teams, fas):
    me = teams[TEAM_ID]
    used = adds_used(league)
    budget = WEEKLY_ADDS - ADDS_RESERVE - used
    actions = ir_actions(me, fas, sched)
    moves = stream_moves(me, fas, sched, budget)
    dead = dead_spots(me, sched)
    parts = matchup_lines(league, sched, teams)
    if actions:
        parts += ["", *actions]
    if moves:
        parts += ["", "🔁 TODAY'S MOVE", moves[0][1]]
    if dead:
        parts += ["", "⚠️ " + "; ".join(dead)]
    parts += ["", f"Adds used {used}/{WEEKLY_ADDS}"]
    title = ("ACTION: " + (moves[0][1].split("\n")[0] if moves else actions[0][2:].split(":")[0])) if (moves or actions) \
        else "No moves today"
    notify(title[:80], "\n".join(parts), "high")


def hourly_checks(league, sched, teams, fas, state):
    me = teams[TEAM_ID]
    alerts = []
    # 1) injury/suspension status changes on your roster (incl. IR players becoming active)
    statuses = state.setdefault("statuses", {})
    for p in me["roster"]:
        key = str(p["id"])
        old = statuses.get(key)
        if old is not None and old != p["status"]:
            alerts.append(("Status change: " + p["name"],
                           f"{p['name']} went from {STATUS_TEXT.get(old, old)} to {STATUS_TEXT.get(p['status'], p['status'])}."))
        statuses[key] = p["status"]
    # 2) IR eligibility / IR return (dedupe by player+kind)
    done = state.setdefault("ir_alerts", [])
    for line in ir_actions(me, fas, sched):
        key = line[:40]
        if key not in done:
            done.append(key)
            alerts.append(("IR move available", line))
    # 3) league-mates dropping someone better than your stream slot
    seen = state.setdefault("seen_drops", [])
    floor = min((p["value"] for p in stream_slots(me["roster"])), default=0)
    free_ir = sched.slots["IR"] - sum(p["slot"] == "IR" for p in me["roster"])
    cutoff = (time.time() - 70 * 60) * 1000
    by_name = {f["name"]: f for f in fas.values()}
    for act in league.recent_activity(size=25):
        if act.date < cutoff:
            continue
        for team, action, player, *_ in act.actions:
            pname = getattr(player, "name", str(player))
            key = f"{act.date}-{pname}"
            if "DROP" not in str(action) or getattr(team, "team_id", None) == TEAM_ID or key in seen:
                continue
            seen.append(key)
            f = by_name.get(pname)
            if not f or f["group"] == "G" or f["value"] <= floor + STEAL_MARGIN:
                continue
            tname = getattr(team, "team_name", team)
            if healthy(f):
                alerts.append(("Grab him: " + pname,
                               f"{tname} dropped {fa_desc(f, sched)}.\n"
                               f"Better than your stream slot ({floor:.1f}/g). He's on waivers for 1 day: claim him "
                               f"if you'd keep him, otherwise skip (a claim moves you to last in waiver order)."))
            elif free_ir > 0:
                alerts.append(("IR stash? " + pname,
                               f"{tname} dropped {pname} ({f['pos']}, {f['team']}, {f['value']:.1f}/g), but he is "
                               f"{STATUS_TEXT.get(f['status'], f['status'].lower())}. You have a free IR slot: claim him "
                               f"only if you'd want him for the second half of the season."))
    state["seen_drops"] = seen[-200:]
    for title, body in alerts:
        notify(title, body, "high", "rotating_light")
    return len(alerts)


# ---------- main ----------
def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in ("auto", "weekly", "daily", "hourly"):
        print(f"unknown mode '{mode}', treating as auto")
        mode = "auto"
    now = dt.datetime.now(TZ)
    try:
        league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
        sched = Schedule(league)
        fas = fetch_players(league, ["FREEAGENT", "WAIVERS"], 250)
        rostered = fetch_players(league, ["ONTEAM"], 400)
        teams = lineups(league, rostered)
    except Exception as e:
        notify("BudLight alerts: couldn't load ESPN",
               f"{type(e).__name__}: {e}\n\nIf this repeats, the espn_s2 / SWID cookies have probably expired.",
               "high", "warning")
        raise

    state = load_state()
    if mode == "auto":
        today = now.date().isoformat()
        run_weekly = now.weekday() == 0 and now.hour >= 7 and state.get("last_weekly") != today
        run_daily = now.hour >= 10 and state.get("last_daily") != today
    else:
        run_weekly, run_daily = mode == "weekly", mode == "daily"

    n = hourly_checks(league, sched, teams, fas, state) if mode in ("auto", "hourly") else 0
    if run_weekly:
        weekly_plan(league, sched, teams, fas)
        state["last_weekly"] = now.date().isoformat()
    if run_daily:
        daily_brief(league, sched, teams, fas)
        state["last_daily"] = now.date().isoformat()
    save_state(state)
    print(f"done: mode={mode} local={now:%a %H:%M} hourly_alerts={n} weekly={run_weekly} daily={run_daily}")


if __name__ == "__main__":
    main()
