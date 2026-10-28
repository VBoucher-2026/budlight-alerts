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
from espn_api.hockey.constant import PRO_TEAM_MAP, STATS_MAP
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
STREAM_SKATERS = os.environ.get("STREAM_SKATERS", "0") == "1"   # schedule-based skater swaps off; upgrades only
WEEKLY_ADDS = 7         # league limit per matchup week
ADDS_RESERVE = 1        # keep this many adds unused Mon-Fri for injuries; released on the weekend
MIN_GAIN = 4.0          # a skater add must gain this many points this week (a goalie start is worth ~4.5)
MIN_QUALITY = 0.85      # the pickup must project to at least 85% of the dropped player's per-game value
PRIOR_GAMES = 10        # blend actual FP/G with ESPN's projection as if the projection were 10 games
GOALIE_SHARE = 0.65
STEAL_MARGIN = 0.3
BASELINE = {"F": 1.2, "D": 1.0, "G": 3.0}   # per-game value assumed for players ESPN doesn't project
# Players the script must never suggest dropping (exact names, comma-separated; can also be set as PROTECT env var)
PROTECT = {n.strip() for n in os.environ.get("PROTECT", "Connor Hellebuyck").split(",") if n.strip()}

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


def season_rank(p):
    """Season value used for ranking: ESPN projection; if ESPN has none (rookies), actual average x 82 once he
    has 3+ games; otherwise the low baseline."""
    if p.get("proj_total"):
        return p["proj_total"]
    if (p.get("gp") or 0) >= 3 and p.get("actual"):
        return p["actual"] * (82 if p["group"] != "G" else 55)
    return BASELINE[p["group"]] * 60


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
        starter_raw = info.get("starterStatusByProGame") or {}
        starter = None  # resolved per today's game in Schedule.resolve_starters()
        if group_of(p.position) == "G" and starter_raw and len([o for o in out.values() if o["group"] == "G"]) < 3:
            print(f"[goalie-flag] {p.name}: starterStatusByProGame={starter_raw}")
        last = splits.get(f"00{YEAR - 1}", {})
        own = info.get("ownership") or {}
        inj = info.get("injuryDetails") or {}
        ret = inj.get("expectedReturnDate")  # often [yyyy, m, d] when ESPN publishes one
        exp_return = None
        if isinstance(ret, list) and len(ret) == 3:
            try:
                exp_return = dt.date(int(ret[0]), int(ret[1]), int(ret[2])).isoformat()
            except Exception:
                exp_return = None
        out[pid] = {"id": pid, "name": p.name, "group": group_of(p.position), "pos": POS.get(p.position, "?"),
                    "owned_pct": round(own.get("percentOwned", 0) or 0, 1), "owned_change": round(own.get("percentChange", 0) or 0, 1),
                    "expected_return": exp_return,
                    "eligible": p.eligibleSlots, "injured": bool(info.get("injured")),
                    "starter": None, "starter_raw": {str(k): str(v).upper() for k, v in starter_raw.items()} if isinstance(starter_raw, dict) else {},
                    "cur_total": season.get("appliedTotal") or 0, "proj_avg": proj_avg,
                    "last_gp": (last.get("stats") or {}).get("34") or (last.get("stats") or {}).get("0") or 0,
                    "last_total": last.get("appliedTotal") or 0, "last_avg": last.get("appliedAverage") or 0,
                    "stats": {k: v for k, v in (p.stats or {}).items() if str(YEAR) in k or str(YEAR - 1) in k},
                    "team": short_team(p.proTeam), "pro_id": info.get("proTeamId"),
                    "status": p.injuryStatus or "ACTIVE", "value": value, "gp": gp, "actual": actual,
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
        self.played_yesterday = set()
        self.today_game_id = {}
        for tid, games in league._get_all_pro_schedule().items():
            if games.get(str(self.today - 1)):
                self.played_yesterday.add(int(tid))
            for g in games.get(str(self.today)) or []:
                if g.get("id") is not None:
                    self.today_game_id[int(tid)] = str(g["id"])
            for sp in self.days:
                for g in games.get(str(sp)) or []:
                    self.all_games[sp].add(int(tid))
                    if g.get("date", 0) > now_ms:
                        self.playing[sp].add(int(tid))
                    if sp not in self.day_name and g.get("date"):
                        self.day_name[sp] = dt.datetime.fromtimestamp(g["date"] / 1000, TZ).strftime("%a")
        print(f"[schedule] period {league.currentMatchupPeriod} today=sp{self.today} days={self.days} "
              f"teams still to play={ {self.day_name.get(k, k): len(v) for k, v in self.playing.items()} }")

    def resolve_starters(self, players):
        """Set p['starter'] from ESPN's per-game starter status, using today's game for the player's team."""
        for p in players.values():
            if p.get("group") != "G":
                continue
            gid = self.today_game_id.get(p.get("pro_id"))
            p["starter"] = (p.get("starter_raw") or {}).get(gid) if gid else None

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


HOLD = {n.strip() for n in os.environ.get("HOLD", "").split(",") if n.strip()}   # manual: players in offers


def pending_trade_players(league):
    """Your players currently in pending trade offers (ESPN pendingTransactions), so the script never drops them."""
    names = set()
    try:
        data = league.espn_request.league_get(params={"view": "mPendingTransactions"})
        id_to_name = {}
        for t in data.get("teams", []):
            for e in (t.get("roster") or {}).get("entries", []):
                pl = (e.get("playerPoolEntry") or {}).get("player") or {}
                id_to_name[pl.get("id")] = pl.get("fullName")
        for tx in data.get("pendingTransactions", []) or data.get("transactions", []) or []:
            if tx.get("status") not in (None, "PENDING", "PROPOSED"):
                continue
            for it in tx.get("items", []) or []:
                if it.get("fromTeamId") == TEAM_ID:
                    n = id_to_name.get(it.get("playerId"))
                    if n:
                        names.add(n)
    except Exception as e:
        print(f"[pending] could not read pending trades ({type(e).__name__})")
    print(f"[pending] players in your pending offers: {sorted(names) or 'none detected'}")
    return names


def stream_slots(roster):
    """Your N healthy skaters with the lowest ESPN season projection, the only ones ever suggested as drops.
    Uses the projection (not the early-season blend) so a star's slow start can't make him droppable."""
    skaters = [p for p in roster if p["group"] != "G" and p["slot"] != "IR" and healthy(p) and p["name"] not in PROTECT]
    return sorted(skaters, key=lambda p: (season_rank(p), p["value"]))[:STREAM_SLOTS]


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


# ---------- role-change detection ----------
SKATER_PTS = {"G": 2, "A": 1, "PPG": 1, "PPA": 1, "SHG": 2, "SHA": 1, "SOG": 0.15, "HIT": 0.25, "BLK": 0.25}


def split_stats(p, label):
    st = ((p.get("stats") or {}).get(label) or {}).get("total") or {}
    return st


def split_ppg(p, label):
    """Fantasy points per game for a stats split under this league's scoring (skaters)."""
    st = split_stats(p, label)
    gp = st.get("GP") or 0
    if not gp:
        return 0.0, 0
    pts = sum(SKATER_PTS[k] * (st.get(k) or 0) for k in SKATER_PTS)
    return pts / gp, int(gp)


def role_changes(me, fas, state, everyone=None):
    """Free agents whose role has grown: more ice time / PP use than last season, producing 2.3+ over the last 15 games."""
    seen = state.setdefault("seen_roles", [])
    out = []
    pool = everyone or fas
    def injured_teammate(f):
        """A notable teammate (same NHL team, projected 150+) who is Out or on IR: the likely reason for the new role."""
        hurt = [p for p in pool.values() if p.get("pro_id") == f.get("pro_id") and p["group"] != "G"
                and p["status"] in ("OUT", "INJURY_RESERVE") and (p.get("proj_total") or 0) >= 150]
        return max(hurt, key=lambda p: p.get("proj_total") or 0, default=None)
    for f in fas.values():
        if f["group"] == "G" or not healthy(f) or f["avail"] != "FREEAGENT":
            continue
        now_avg, now_gp = split_ppg(f, f"Last 15 {YEAR}")
        if now_gp < 6 or now_avg < 2.3:
            continue
        cur, last = split_stats(f, f"Total {YEAR}"), split_stats(f, f"Total {YEAR - 1}")
        atoi_now, atoi_last = cur.get("ATOI") or 0, last.get("ATOI") or 0
        ppp_rate_now = (cur.get("PPP") or 0) / max(cur.get("GP") or 1, 1)
        ppp_rate_last = (last.get("PPP") or 0) / max(last.get("GP") or 1, 1)
        if atoi_now < 17 * 60:
            continue
        if not (atoi_now - atoi_last >= 90 or ppp_rate_now >= ppp_rate_last + 0.15 or not last):
            continue
        # offense must be behind it: a PP role or real scoring over the stretch, not just minutes and hits
        l15 = split_stats(f, f"Last 15 {YEAR}")
        scoring_rate = ((l15.get("G") or 0) + (l15.get("A") or 0)) / max(l15.get("GP") or 1, 1)
        if ppp_rate_now < 0.2 and scoring_rate < 0.5:
            continue
        key = f"{f['id']}-{int(now_gp)}"
        if key in seen:
            continue
        seen.append(key)
        why = injured_teammate(f)
        cause = (f"likely injury-driven ({why['name']} out), may end when he returns" if why
                 else "no injury behind it, likely durable (trade or coaching change)")
        out.append(f"📈 ROLE CHANGE: {f['name']} ({f['pos']}, {f['team']}) {now_avg:.1f}/g over last {now_gp} games, "
                   f"ice time {atoi_now / 60:.0f} min (last season {atoi_last / 60:.0f}), PP points/game {ppp_rate_now:.2f} "
                   f"(last season {ppp_rate_last:.2f}); {cause}. Info only: he becomes an add if the UPGRADE rule also flags him.")
    state["seen_roles"] = seen[-300:]
    return out[:3]


# ---------- injured free agents worth stashing ----------
def stash_candidates(fas):
    out = []
    for f in fas.values():
        if f["avail"] != "FREEAGENT" or f["status"] not in ("INJURY_RESERVE", "OUT"):
            continue
        if f["group"] == "G":
            ok = (f.get("proj_avg") or 0) >= 5.0
        else:
            ok = (f.get("proj_avg") or 0) >= 2.3 or (f.get("last_avg") or 0) >= 2.4
        if ok:
            out.append(f)
    return sorted(out, key=lambda f: (f.get("proj_avg") or 0) / (5.5 if f["group"] == "G" else 2.5), reverse=True)


def stash_watch(me, fas, sched, state, adds_left):
    """Alerts: (1) return date within 10 days + free IR slot, (2) tracked player activated and unowned, (3) Sunday spare add."""
    lines = []
    free_ir = sched.slots["IR"] - sum(p["slot"] == "IR" for p in me["roster"])
    cands = stash_candidates(fas)
    tracked = state.setdefault("stash_tracked", {})
    today = dt.datetime.now(TZ).date()
    # (2) previously tracked injured players now healthy and still free agents
    for pid, info in list(tracked.items()):
        f = next((x for x in fas.values() if str(x["id"]) == pid), None)
        if f and f["avail"] == "FREEAGENT" and f["status"] in ("ACTIVE", "DAY_TO_DAY"):
            lines.append(f"🔓 BACK FROM INJURY: {f['name']} ({f['pos']}, {f['team']}, proj {f.get('proj_avg') or 0:.2f}/g, "
                         f"last season {f.get('last_avg') or 0:.2f}) is active and unowned. Grab him now if he beats your weakest player.")
            tracked.pop(pid, None)
        elif not f:
            tracked.pop(pid, None)  # rostered by someone or gone
    for f in cands:
        tracked[str(f["id"])] = f["status"]
    # (1) return date within 10 days
    if free_ir > 0 and adds_left > 0:
        for f in cands:
            r = f.get("expected_return")
            if r:
                days = (dt.date.fromisoformat(r) - today).days
                if 0 <= days <= 10:
                    key = f"stash-{f['id']}-{r}"
                    if key not in state.setdefault("stash_alerted", []):
                        state["stash_alerted"].append(key)
                        lines.append(f"🏥 STASH NOW: {f['name']} ({f['pos']}, {f['team']}) expected back {r}. Add him into your free "
                                     f"IR slot while he's still IR-tagged (proj {f.get('proj_avg') or 0:.2f}/g).")
    # (3) Sunday: spare add + free IR slot
    if (today.weekday() == 6 and free_ir > 0 and adds_left > 0 and cands
            and state.get("stash_sunday") != today.isoformat()):
        state["stash_sunday"] = today.isoformat()
        f = cands[0]
        lines.append(f"🧺 SPARE ADD: {adds_left} add(s) expire tonight and you have a free IR slot. Best stash: {f['name']} "
                     f"({f['pos']}, {f['team']}, proj {f.get('proj_avg') or 0:.2f}/g"
                     + (f", expected back {f['expected_return']}" if f.get("expected_return") else "") + ").")
    state["stash_alerted"] = state.get("stash_alerted", [])[-200:]
    return lines


# ---------- recommendations ----------
def fa_desc(f, sched):
    return f"{f['name']} ({f['pos']}, {f['team']}, {f['value']:.1f}/g, plays {', '.join(sched.days_left_for(f['pro_id'])) or 'no more games'})"


def stream_moves(me, fas, sched, budget):
    """Best add/drop for each stream slot. Returns list of (gain, text)."""
    moves = []
    if budget <= 0 or not STREAM_SKATERS:
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


def goalie_stream(me, fas, everyone, sched, budget):
    """For each of your goalies with no game today, name an available goalie who starts tonight."""
    goalies = [p for p in me["roster"] if p["group"] == "G" and p["slot"] != "IR"]
    print("[goalies] " + ", ".join(f"{p['name']} proj={p.get('proj_total') or 0:.0f} avg={p.get('actual') or 0:.1f} rank={season_rank(p):.0f}"
                                  for p in goalies))
    # how many goalie slots would be empty tonight even after benching/unbenching your own goalies
    playing_tonight = [p for p in goalies if healthy(p) and sched.plays_today(p["pro_id"])]
    open_slots = max(0, sched.slots["G"] - len(playing_tonight))
    idle = [p for p in goalies if p["name"] not in PROTECT and not sched.plays_today(p["pro_id"])]
    # drop the benched / lowest-value idle goalie first
    idle.sort(key=lambda p: (p["slot"] != "Bench", season_rank(p)))
    idle = idle[:open_slots]
    print(f"[goalies] playing tonight: {[p['name'] for p in playing_tonight]}, open slots: {open_slots}")
    if not idle or budget <= 0:
        return []
    best_on_team = {}
    for p in everyone.values():
        if p["group"] == "G" and p.get("pro_id"):
            cur = best_on_team.get(p["pro_id"])
            if cur is None or (p.get("proj_total") or 0) > (cur.get("proj_total") or 0):
                best_on_team[p["pro_id"]] = p
    pool = [f for f in fas.values() if f["group"] == "G" and healthy(f) and f["avail"] == "FREEAGENT"
            and sched.plays_today(f["pro_id"])]
    confirmed = [f for f in pool if (f.get("starter") or "") in ("EXPECTED", "PROBABLE", "CONFIRMED", "STARTER", "P", "S")]
    likely = [f for f in pool if f not in confirmed and f["pro_id"] not in sched.played_yesterday
              and best_on_team.get(f["pro_id"], {}).get("id") == f["id"]]
    ranked = sorted(confirmed, key=season_rank, reverse=True) + sorted(likely, key=season_rank, reverse=True)
    lines = []
    for g in idle:
        if len(lines) >= budget:
            break
        def quality(p):  # season average once he has 5+ starts, else projection-based
            return p["actual"] if (p.get("gp") or 0) >= 5 and p.get("actual") else season_rank(p) / 55
        ok = [c for c in ranked if quality(c) >= 0.75 * quality(g)]   # never drop a goalie for a clearly worse one
        # with adds to keep chaining, a start tonight is enough; with the last add, require more starts overall
        if budget - len(lines) >= 2:
            f = ok[0] if ok else None
        else:
            f = next((c for c in ok if sched.games_left(c["pro_id"]) > sched.games_left(g["pro_id"])), None)
        if not f:
            continue
        ranked.remove(f)
        how = (f"ESPN lists him as {f['starter'].lower()} starter tonight" if f in confirmed
               else "his team's No. 1, plays tonight, no back-to-back; confirm the P/S badge first")
        lines.append(f"🥅 {g['name']} has no game today → drop him, add {f['name']} ({f['team']}): {how}. "
                     f"Games left this week: {f['name']} {sched.games_left(f['pro_id'])}, {g['name']} {sched.games_left(g['pro_id'])}.")
    return lines


def goalie_bench_swap(me, sched):
    """Carrying 3 goalies: if a benched goalie plays tonight and a slotted one doesn't, say which to swap."""
    gs = [p for p in me["roster"] if p["group"] == "G" and p["slot"] != "IR"]
    benched = [p for p in gs if p["slot"] == "Bench"]
    slotted = [p for p in gs if p["slot"] != "Bench"]
    lines = []
    for b in benched:
        if not sched.plays_today(b["pro_id"]) or not healthy(b):
            continue
        idle = [p for p in slotted if not sched.plays_today(p["pro_id"])]
        if idle:
            lines.append(f"🔀 Goalie swap (no add): start {b['name']}, bench {idle[0]['name']} (no game today). "
                         f"Check {b['name']}'s P/S badge first.")
    return lines


def goalie_report(me, fas, sched):
    def line(p, tag=""):
        badge = {"EXPECTED": " · P tonight", "PROBABLE": " · P tonight", "CONFIRMED": " · S tonight", "STARTER": " · S tonight"}.get(p.get("starter") or "", "")
        return (f"• {p['name']} ({p['team']}){tag}: avg {p.get('actual') or 0:.1f} over {int(p.get('gp') or 0)} gp · "
                f"proj {p.get('proj_total') or 0:.0f} · {sched.games_left(p['pro_id'])} games left{badge}")
    mine = [p for p in me["roster"] if p["group"] == "G"]
    avail = sorted((f for f in fas.values() if f["group"] == "G" and healthy(f)), key=season_rank, reverse=True)[:8]
    return ["YOUR GOALIES", *[line(p, " (IR)" if p["slot"] == "IR" else "") for p in mine], "",
            "TOP AVAILABLE", *[line(p) for p in avail]]


def upgrade_moves(me, fas, budget):
    """Lasting upgrades: an available skater out-producing one of your stream slots by 0.5+/game (10+ games each)."""
    if budget <= 0:
        return []
    out = []
    for occ in stream_slots(me["roster"]):
        if (occ.get("gp") or 0) < 10:
            continue
        def credible(f):  # a hot month is not enough: needs a track record comparable to the player he'd replace
            return ((f.get("proj_total") or 0) >= 0.9 * (occ.get("proj_total") or 0)
                    or (f.get("last_avg") or 0) >= (occ.get("last_avg") or 0))
        best = max((f for f in fas.values() if f["group"] == occ["group"] and healthy(f) and f["avail"] == "FREEAGENT"
                    and (f.get("gp") or 0) >= 10 and (f.get("actual") or 0) >= (occ.get("actual") or 0) + 0.5 and credible(f)),
                   key=lambda f: f.get("actual") or 0, default=None)
        if best:
            out.append(f"⬆️ UPGRADE: add {best['name']} ({best['pos']}, {best['team']}, {best['actual']:.2f}/g over {int(best['gp'])} gp, "
                       f"proj {best.get('proj_total') or 0:.0f}) for {occ['name']} ({occ['actual']:.2f}/g, proj {occ.get('proj_total') or 0:.0f})")
    return out[:1]


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
    to_ir = [p for p in me["roster"] if p["slot"] != "IR" and "IR" in (p.get("eligible") or [])
             and p["status"] in ("OUT", "INJURY_RESERVE", "SUSPENSION")]
    free_ir = sched.slots["IR"] - sum(p["slot"] == "IR" for p in me["roster"])
    ready = [p for p in me["roster"] if p["slot"] == "IR" and p["status"] == "ACTIVE"]
    lines = []
    for p in to_ir[:max(free_ir, 0)]:
        lines.append(f"🏥 {p['name']} is IR-eligible: move him to IR (frees a roster spot), then add a {p['group']}")
    for p in ready:
        drop = next(iter(sorted((q for q in me["roster"] if q["group"] == p["group"] and q["slot"] != "IR"
                                 and q["name"] not in PROTECT),
                                key=season_rank)), None)
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
    anchor = next((p for p in me["roster"] if p["group"] == "G" and p["name"] in PROTECT and p["slot"] != "IR"), None)
    nights = []
    for sp in sched.days:
        n_teams = len(sched.all_games.get(sp, set()))
        tag = "light" if n_teams <= 10 else ""
        a_plays = anchor and anchor.get("pro_id") in sched.all_games.get(sp, set())
        nights.append(f"{sched.day_name.get(sp, sp)}: {n_teams // 2} games{' (' + tag + ')' if tag else ''}"
                      f"{', ' + anchor['name'].split()[-1] + ' plays' if a_plays else ''}")
    parts += ["", "🥅 GOALIE STREAM WEEK (slot 2 gets a confirmed starter each night; light nights may have none)", *nights]
    shrinking = []
    for p in me["roster"]:
        if p["group"] == "G" or p["slot"] == "IR":
            continue
        cur, last = split_stats(p, f"Total {YEAR}"), split_stats(p, f"Total {YEAR - 1}")
        if (cur.get("GP") or 0) >= 8 and last.get("ATOI") and (last["ATOI"] - (cur.get("ATOI") or 0)) >= 120:
            shrinking.append(f"{p['name']}: {(cur.get('ATOI') or 0) / 60:.0f} min/game vs {last['ATOI'] / 60:.0f} last season")
    if shrinking:
        parts += ["", "📉 ROLE SHRINKING (consider selling while the name still carries value)", *shrinking]
    parts += ["", f"🔁 STREAMING PLAN (adds left {WEEKLY_ADDS - used}/{WEEKLY_ADDS}, keeping {ADDS_RESERVE} in reserve)"]
    moves = stream_moves(me, fas, sched, budget)
    parts += [m[1] for m in moves] or ["No stream worth making this week. Your lowest-value skaters: "
                                        + ", ".join(f"{p['name']} ({p['value']:.1f}/g)" for p in stream_slots(me["roster"]))]
    parts += ["", "🏒 MATCHUP", *matchup_lines(league, sched, teams)]
    notify("Weekly plan: " + (f"{len(moves)} move(s)" if moves else "no moves"), "\n".join(parts), "high")


def daily_brief(league, sched, teams, fas, everyone=None, state=None):
    me = teams[TEAM_ID]
    state = state if state is not None else {}
    used = adds_used(league)
    reserve = ADDS_RESERVE if len(sched.days) > 2 else 0   # adds expire Sunday night: spend them on the weekend
    budget = WEEKLY_ADDS - reserve - used
    actions = ir_actions(me, fas, sched)
    moves = stream_moves(me, fas, sched, budget)
    ups = upgrade_moves(me, fas, budget)
    dead = dead_spots(me, sched)
    parts = matchup_lines(league, sched, teams)
    if actions:
        parts += ["", *actions]
    if ups:
        parts += ["", *ups]
        actions = actions + ups
    if moves:
        parts += ["", "🔁 TODAY'S MOVE", moves[0][1]]
    gs = goalie_bench_swap(me, sched) + goalie_stream(me, fas, everyone or fas, sched, budget)
    if gs:
        parts += ["", *gs]
        actions = actions + gs
    if dead:
        parts += ["", "⚠️ " + "; ".join(dead)]
    roles = role_changes(me, fas, state, everyone)
    if roles:
        parts += ["", *roles]
    left = WEEKLY_ADDS - used
    if dt.datetime.now(TZ).weekday() >= 4 and left > 0:
        parts += ["", f"⏳ {left} add(s) left and they expire Sunday night: spend them on confirmed weekend starters "
                      f"(Saturday has the most games) or an IR stash."]
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
    try:
        left = WEEKLY_ADDS - adds_used(league)
        for line in stash_watch(me, fas, sched, state, left):
            alerts.append(("Stash watch", line))
    except Exception as e:
        print(f"[stash] skipped ({type(e).__name__}: {e})")
    for title, body in alerts:
        notify(title, body, "high", "rotating_light")
    return len(alerts)


# ---------- snapshot for analysis outside the script ----------
def build_snapshot(league, sched, teams, fas, rostered, now):
    """Everything useful for roster decisions, written to snapshot.json (committed by the workflow)."""
    req = league.espn_request
    settings = req.league_get(params={"view": "mSettings"}).get("settings", {})
    scoring = {}
    for item in (settings.get("scoringSettings") or {}).get("scoringItems", []):
        name = STATS_MAP.get(str(item.get("statId")), STATS_MAP.get(item.get("statId"), f"stat{item.get('statId')}"))
        scoring[name or f"stat{item.get('statId')}"] = item.get("points")
    sched_settings = settings.get("scheduleSettings") or {}
    league_settings = {
        "scoring": scoring,
        "lineup_slots": sched.slots,
        "roster_size": (settings.get("rosterSettings") or {}).get("rosterLocktimeType"),
        "acquisition": {k: (settings.get("acquisitionSettings") or {}).get(k) for k in
                        ("acquisitionLimit", "matchupAcquisitionLimit", "waiverHours", "waiverOrderReset", "acquisitionType")},
        "trade": {k: (settings.get("tradeSettings") or {}).get(k) for k in ("deadlineDate", "vetoVotesRequired", "revisionHours")},
        "schedule": {k: sched_settings.get(k) for k in ("matchupPeriodCount", "playoffTeamCount", "playoffMatchupPeriodLength",
                                                        "playoffSeedingRule", "matchupPeriods")},
    }
    # standings + waiver order + acquisitions
    tdata = req.league_get(params={"view": "mTeam"}).get("teams", [])
    standings = []
    for t in tdata:
        rec = (t.get("record") or {}).get("overall") or {}
        standings.append({"team_id": t.get("id"), "name": f"{t.get('location', '')} {t.get('nickname', '')}".strip() or t.get("name"),
                          "wins": rec.get("wins"), "losses": rec.get("losses"), "ties": rec.get("ties"),
                          "points_for": round(rec.get("pointsFor", 0) or 0, 1), "points_against": round(rec.get("pointsAgainst", 0) or 0, 1),
                          "playoff_seed": t.get("playoffSeed"), "waiver_rank": t.get("waiverRank"),
                          "acquisitions_season": (t.get("transactionCounter") or {}).get("acquisitions"),
                          "acquisitions_this_matchup": ((t.get("transactionCounter") or {}).get("matchupAcquisitionTotals") or {}).get(str(league.currentMatchupPeriod), 0)})
    # full season schedule for every team (who plays whom each matchup period, with totals so far)
    mdata = req.league_get(params={"view": "mMatchup"}).get("schedule", [])
    matchups = [{"period": m.get("matchupPeriodId"), "home": (m.get("home") or {}).get("teamId"), "away": (m.get("away") or {}).get("teamId"),
                 "home_pts": round((m.get("home") or {}).get("totalPoints", 0) or 0, 1),
                 "away_pts": round((m.get("away") or {}).get("totalPoints", 0) or 0, 1), "winner": m.get("winner")}
                for m in mdata]
    # NHL games per team per upcoming week (next 6 weeks), from ESPN's pro schedule
    pro = league._get_all_pro_schedule()
    week_games = {}
    for tid, games in pro.items():
        for sp, gl in games.items():
            for g in gl or []:
                if not g.get("date"):
                    continue
                d = dt.datetime.fromtimestamp(g["date"] / 1000, TZ).date()
                if d < now.date() or (d - now.date()).days > 42:
                    continue
                wk = (d - dt.timedelta(days=d.weekday())).isoformat()  # Monday of that week
                week_games.setdefault(short_team(PRO_TEAM_MAP.get(int(tid), tid)), {}).setdefault(wk, []).append(d.strftime("%a"))
    # current matchup: both lineups with today's and matchup points
    me = teams[TEAM_ID]
    current = None
    for box in league.box_scores(matchup_total=True):
        ids = (getattr(box.home_team, "team_id", None), getattr(box.away_team, "team_id", None))
        if TEAM_ID in ids:
            side = "home" if ids[0] == TEAM_ID else "away"
            other = "away" if side == "home" else "home"
            opp = getattr(box, f"{other}_team")
            current = {"opponent": getattr(opp, "team_name", None), "opponent_id": getattr(opp, "team_id", None),
                       "my_points": getattr(box, f"{side}_score"), "opp_points": getattr(box, f"{other}_score"),
                       "my_games_left": remaining_value(me["roster"], sched)[0],
                       "opp_games_left": remaining_value(teams[opp.team_id]["roster"], sched)[0] if getattr(opp, "team_id", None) in teams else None,
                       "opp_lineup": [{"name": p["name"], "pos": p["pos"], "team": p["team"], "slot": p["slot"], "status": p["status"],
                                       "gp": p.get("gp"), "avg": p.get("actual"), "proj": p.get("proj_total"),
                                       "games_left": sched.games_left(p.get("pro_id")), "days": sched.days_left_for(p.get("pro_id"))}
                                      for p in teams.get(getattr(opp, "team_id", None), {}).get("roster", [])]}
    # recent league activity
    activity = []
    for act in league.recent_activity(size=50):
        for team, action, player, *_ in act.actions:
            activity.append({"when": dt.datetime.fromtimestamp(act.date / 1000, TZ).isoformat(timespec="minutes"),
                             "team": getattr(team, "team_name", str(team)), "action": action, "player": getattr(player, "name", str(player))})
    keys = ("id", "name", "pos", "team", "status", "injured", "avail", "starter", "owned_pct", "owned_change", "eligible",
            "gp", "actual", "cur_total", "proj_total", "proj_avg", "last_gp", "last_total", "last_avg", "stats")
    def row(p, extra=None, full=True):
        r = {k: p.get(k) for k in keys if full or k != "stats"}
        r["games_left"] = sched.games_left(p.get("pro_id"))
        r["days"] = sched.days_left_for(p.get("pro_id"))
        if extra:
            r.update(extra)
        return r
    avail_sorted = sorted(fas.values(), key=season_rank, reverse=True)
    team_names = {tid: getattr(t["team"], "team_name", str(tid)) for tid, t in teams.items()}
    snap = {"generated": now.isoformat(), "my_team_id": TEAM_ID, "matchup_period": league.currentMatchupPeriod,
            "scoring_period": league.current_week, "week_days": [sched.day_name.get(d, d) for d in sched.days],
            "adds_used": adds_used(league), "stream_slots": [p["name"] for p in stream_slots(me["roster"])],
            "league_settings": league_settings, "standings": standings, "matchups": matchups, "current_matchup": current,
            "nhl_games_by_week": week_games, "recent_activity": activity,
            "roster": [row(p, {"slot": p.get("slot")}) for p in me["roster"]],
            "available": [row(f, full=(i < 250)) for i, f in enumerate(avail_sorted)],
            "other_teams": {team_names[tid]: [row(p, {"slot": p.get("slot")}) for p in t["roster"]]
                            for tid, t in teams.items() if tid != TEAM_ID}}
    with open("snapshot.json", "w") as fh:
        json.dump(snap, fh, separators=(",", ":"), default=str)
    print(f"[snapshot] written: {len(snap['available'])} available, {len(snap['other_teams'])} other teams, "
          f"{os.path.getsize('snapshot.json') // 1024} KB")


# ---------- main ----------
def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in ("auto", "weekly", "daily", "hourly", "goalies", "snapshot"):
        print(f"unknown mode '{mode}', treating as auto")
        mode = "auto"
    now = dt.datetime.now(TZ)
    try:
        league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
        sched = Schedule(league)
        fas = fetch_players(league, ["FREEAGENT", "WAIVERS"], 500)
        rostered = fetch_players(league, ["ONTEAM"], 600)
        sched.resolve_starters(fas)
        sched.resolve_starters(rostered)
        teams = lineups(league, rostered)
        PROTECT.update(HOLD | pending_trade_players(league))
    except Exception as e:
        notify("BudLight alerts: couldn't load ESPN",
               f"{type(e).__name__}: {e}\n\nIf this repeats, the espn_s2 / SWID cookies have probably expired.",
               "high", "warning")
        raise

    state = load_state()
    today_iso = now.date().isoformat()
    write_snapshot = mode in ("daily", "weekly", "goalies", "snapshot") or (
        mode == "auto" and now.hour >= 10 and state.get("last_daily") != today_iso)
    if write_snapshot:
        try:
            build_snapshot(league, sched, teams, fas, rostered, now)
        except Exception as e:
            print(f"[snapshot] skipped ({type(e).__name__}: {e})")
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
        daily_brief(league, sched, teams, fas, {**fas, **rostered}, state)
        state["last_daily"] = now.date().isoformat()
    if mode == "auto" and 11 <= now.hour <= 18 and state.get("last_goalie") != now.date().isoformat():
        me = teams[TEAM_ID]
        budget = WEEKLY_ADDS - (ADDS_RESERVE if len(sched.days) > 2 else 0) - adds_used(league)
        gs = goalie_bench_swap(me, sched) + goalie_stream(me, fas, {**fas, **rostered}, sched, budget)
        if gs:
            notify("Goalie move tonight", "\n\n".join(gs), "high", "goal_net")
            state["last_goalie"] = now.date().isoformat()
        elif now.hour == 18:
            state["last_goalie"] = now.date().isoformat()   # nothing found today; stop checking
    if mode == "goalies":
        notify("Goalie report", "\n".join(goalie_report(teams[TEAM_ID], fas, sched)), "high", "goal_net")
    save_state(state)
    print(f"done: mode={mode} local={now:%a %H:%M} hourly_alerts={n} weekly={run_weekly} daily={run_daily}")


if __name__ == "__main__":
    main()
