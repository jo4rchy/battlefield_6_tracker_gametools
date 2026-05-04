"""
Gametools -> TRN Battlefield Tracker profile converter.

Takes the corrected Gametools `/bf6/stats/` payload (the same dict that
GametoolsClient.fetch_stats returns) plus the Gametools `/bf6/profile/`
payload (for playerCard rank metadata) and produces a JSON shape that is
wire-compatible with `battlefieldtracker.com`'s profile endpoint, i.e.
`{ "data": { "platformInfo": {...}, "userInfo": {...}, "metadata": {...},
            "segments": [...], "availableSegments": [...], "expiryDate": ... } }`.

Segment types produced:
    overview, gamemode, gamemode-category, kit, kit-category, level,
    weapon, weapon-category, vehicle, vehicle-category, gadget, gadget-category.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

EPOCH_ZERO = "0001-01-01T00:00:00+00:00"

# Names/ids that gametools uses for aggregate "sum of everything" rows.
# We never want these to show up as a segment or a match row — they're
# already covered by the overview.
_AGGREGATE_TOKENS = {"all", "total", "overall", "official"}

_NAME_FIELDS = (
    "groupName", "gamemodeName", "className", "mapName",
    "weaponName", "vehicleName", "gadgetName", "name", "title",
)


def _is_aggregate(item: Dict[str, Any]) -> bool:
    """True when a list item is the 'All / Total' aggregate rather than a
    concrete gamemode/class/map/etc."""
    if not isinstance(item, dict):
        return False
    iid = str(item.get("id", "")).strip().lower()
    if iid in _AGGREGATE_TOKENS:
        return True
    for f in _NAME_FIELDS:
        v = item.get(f)
        if isinstance(v, str) and v.strip().lower() in _AGGREGATE_TOKENS:
            return True
    return False


def _filter_aggregates(items: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [it for it in (items or []) if not _is_aggregate(it)]


# ------------------------------------------------------------------
# primitives
# ------------------------------------------------------------------

def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default
    
def _pct(v, default: float = 0.0) -> float:
    try:
        if isinstance(v, str) and v.endswith("%"):
            return float(v[:-1])
        return float(v)
    except Exception:
        return default


def _safe_div(n, d, ndigits: int = 2) -> float:
    n = _f(n); d = _f(d)
    if d <= 0:
        return 0.0
    return round(n / d, ndigits)


def _kpm(kills, seconds) -> float:
    k = _f(kills); s = _f(seconds)
    if s <= 0:
        return 0.0
    return round(k / (s / 60.0), 2)


def _dpm(damage, seconds) -> float:
    d = _f(damage); s = _f(seconds)
    if s <= 0:
        return 0.0
    return round(d / (s / 60.0), 2)


# ------------------------------------------------------------------
# display formatters (match TRN conventions roughly)
# ------------------------------------------------------------------

def _fmt_int(v) -> str:
    return f"{_i(v):,}"


def _fmt_num(v, decimals: int = 2) -> str:
    return f"{_f(v):,.{decimals}f}"


def _fmt_pct(v, decimals: int = 1) -> str:
    return f"{_f(v):.{decimals}f}%"


def _fmt_ratio(v, decimals: int = 2) -> str:
    return f"{_f(v):.{decimals}f}"


def _fmt_np2(v) -> str:
    """TRN 'NumberPrecision2': 2dp with thousands separator.
    6.15 → '6.15', 48.0 → '48.00', 4918.0 → '4,918.00'."""
    return f"{_f(v):,.2f}"


def _fmt_time(seconds) -> str:
    s = _i(seconds)
    if s <= 0:
        return "0s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h >= 1 and m > 0:
        # Big hours: drop the minutes the way TRN does for >= 10h ("293h")
        if h >= 10:
            return f"{h}h"
        return f"{h}h {m:02d}m"
    if h >= 1:
        return f"{h}h"
    if m >= 1:
        return f"{m:02d}m {sec:02d}s"
    return f"{sec}s"


# ------------------------------------------------------------------
# v0.0.4.6: gametools' /player/playing endpoint sometimes returns a
# degraded shape where the top-level `playerProfiles` array is empty
# and the actual rank / badges payload is nested one level deeper
# under `other[*].playerProfiles[0]`. In that fallback shape, the
# fields (`badges`, `rank`, `rankImage`) are also inlined at the
# player-profile level instead of nested under a `playerCard` wrapper.
# Both shapes have to be handled or `[0]` on the empty list raises
# `IndexError: list index out of range` in the converter.
# ------------------------------------------------------------------
def _pick_player_card(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Best-effort extract of the gametools 'playerCard'-like dict.

    Returns a dict that exposes (where available): ``badges``, ``rank``,
    ``rankImage``. Always returns a dict (possibly empty) — never raises
    on missing/empty fields.
    """
    if not isinstance(profile, dict):
        return {}

    def _from_pp(pp: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(pp, dict):
            return None
        pc = pp.get("playerCard")
        if isinstance(pc, dict) and pc:
            return pc
        # degraded shape: badges / rank / rankImage inlined directly
        if any(k in pp for k in ("badges", "rank", "rankImage")):
            return pp
        return None

    # primary: profile.playerProfiles[0]
    pps = profile.get("playerProfiles") or []
    if isinstance(pps, list) and pps:
        pc = _from_pp(pps[0])
        if pc is not None:
            return pc

    # fallback: profile.other[*].playerProfiles[0]
    others = profile.get("other") or []
    if isinstance(others, list):
        for o in others:
            if not isinstance(o, dict):
                continue
            opps = o.get("playerProfiles") or []
            if isinstance(opps, list) and opps:
                pc = _from_pp(opps[0])
                if pc is not None:
                    return pc

    return {}


def _stat(
    display_name: str,
    display_category: str,
    category: str,
    value,
    display_type: str = "Number",
    metadata: Optional[Dict[str, Any]] = None,
    display_value: Optional[str] = None,
    percentile: Optional[float] = None,
) -> Dict[str, Any]:
    if display_value is None:
        if display_type == "TimeSeconds":
            display_value = _fmt_time(value)
        elif display_type == "NumberPrecision2":
            display_value = _fmt_np2(value)
        elif display_type == "NumberPercentage":
            display_value = _fmt_pct(value)
        # legacy aliases (older callers may still pass these)
        elif display_type == "Ratio":
            display_value = _fmt_ratio(value)
        elif display_type == "Percentage":
            display_value = _fmt_pct(value)
        elif isinstance(value, float):
            display_value = _fmt_num(value, 2)
        else:
            display_value = _fmt_int(value)
    out: Dict[str, Any] = {
        "displayName": display_name,
        "displayCategory": display_category,
        "category": category,
        "metadata": metadata or {},
        "value": value,
        "displayValue": display_value,
        "displayType": display_type,
    }
    if percentile is not None:
        out["percentile"] = percentile
    return out


# ------------------------------------------------------------------
# overview segment
# ------------------------------------------------------------------

def _overview_segment(stats: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = stats or {}

    # Note: aggregate "All" rows are filtered out everywhere; the overview
    # itself IS the all-aggregate, so summing filtered lists is correct here.
    weapons = _filter_aggregates(s.get("weapons"))
    vehicles = _filter_aggregates(s.get("vehicles"))
    gadgets = _filter_aggregates(s.get("gadgets"))
    classes = _filter_aggregates(s.get("classes"))

    weapon_kills = sum(_i(w.get("kills")) for w in weapons)
    weapon_damage = sum(_i(w.get("damage")) for w in weapons)
    vehicle_damage = sum(_i(v.get("damage")) for v in vehicles)
    gadget_damage = sum(_i(g.get("damage")) for g in gadgets)
    vehicle_time = sum(_i(v.get("timeIn")) for v in vehicles)
    vehicle_deployments = sum(_i(v.get("spawns")) for v in vehicles)
    kit_deployments = sum(_i(c.get("spawns")) for c in classes)
    weapon_multi = sum(_i(w.get("multiKills")) for w in weapons)
    vehicle_multi = sum(_i(v.get("multiKills")) for v in vehicles)
    gadget_multi = sum(_i(g.get("multiKills")) for g in gadgets)
    body_kills = sum(_i(w.get("bodyKills")) for w in weapons)
    gadget_kills = sum(_i(g.get("kills")) for g in gadgets)
    gadget_uses = sum(_i(g.get("uses")) for g in gadgets)
    vehicle_assists = sum(_i(v.get("assists")) for v in vehicles)
    passenger_assists = sum(_i(v.get("passengerAssists")) for v in vehicles)
    driver_assists = sum(_i(v.get("driverAssists")) for v in vehicles)
    vehicle_kills = sum(_i(v.get("kills")) for v in vehicles)
    road_kills = sum(_i(v.get("roadKills")) for v in vehicles)

    divided = s.get("dividedKills") or {}
    in_round = s.get("inRound") or {}
    objective = s.get("objective") or {}
    obj_time_dict = objective.get("time") or {}
    sector = s.get("sector") or {}

    matches = 0
    total_win_percent = 0.0
    for i in s.get("gameModeGroups", []):
        if i.get("gamemodeName") == "All":
            matches = i.get("matches")
            total_win_percent = _pct(i.get("winPercent"))
            break
    seconds_played = _i(s.get("secondsPlayed"))
    matches_played = _i(matches)



    kills = _i(s.get("kills"))
    deaths = _i(s.get("deaths"))
    assists = _i(s.get("killAssists"))
    wins = _i(s.get("wins"))
    loses = _i(s.get("loses"))
    total_match = wins + loses

    total_score = _i(s.get("XP")[0].get("performance"))

    # damage: fall back to summed weapon+vehicle+gadget damage when top-level is 0
    damage = _i(s.get("damage")) or (weapon_damage + vehicle_damage + gadget_damage)

    # rank from playerCard (from the profile endpoint).
    # v0.0.4.6: route through _pick_player_card so we tolerate the degraded
    # gametools shape where playerProfiles is empty and the data lives under
    # other[*].playerProfiles[0] with badges/rank/rankImage inlined.
    rank_value = 0
    rank_metadata: Dict[str, Any] = {}
    if profile:
        pc = _pick_player_card(profile)
        if pc:
            rank_value = _i(pc.get("rank"))
            ri = pc.get("rankImage") or {}
            rank_metadata = {
                "imageUrl": ri.get("large") or ri.get("small") or "",
                "rankName": "",
            }

    stats_out: Dict[str, Any] = {
        "score":                _stat("Score",              "Game",     "game",     total_score),
        "careerPlayerRank":     _stat("Player Rank",        "Game",     "game",     rank_value, metadata=rank_metadata),
        "matchesPlayed":        _stat("Matches Played",     "Game",     "game",     matches_played),
        "matchesWon":           _stat("Wins",               "Game",     "game",     wins),
        "matchesLost":          _stat("Losses",             "Game",     "game",     loses),
        "timePlayed":           _stat("Time Played",        "Game",     "game",     seconds_played, display_type="TimeSeconds"),

        "kills":                _stat("Kills",              "Combat",   "combat",   kills),
        "weaponKills":          _stat("Weapon Kills",       "Weapons",  "weapons",  weapon_kills),
        "vehicleTimePlayed":    _stat("Vehicle Time Played","Vehicles", "vehicles", vehicle_time, display_type="TimeSeconds"),
        "deployments":          _stat("Deployments",        "Game",     "game",     kit_deployments + vehicle_deployments),
        "kitDeployments":       _stat("Class Deployments",  "Game",     "game",     kit_deployments),
        "vehicleDeployments":   _stat("Vehicle Deployments","Vehicles", "vehicles", vehicle_deployments),

        "playerKills":          _stat("Player Kills",       "Combat",   "combat",   _i(divided.get("human"))),
        "meleeKills":           _stat("Melee Kills",        "Combat",   "combat",   _i(divided.get("melee"))),
        "vehicleKills":         _stat("Vehicle Kills",      "Vehicles", "vehicles", vehicle_kills),
        "gadgetKills":          _stat("Gadget Kills",       "Gadgets",  "gadgets",  gadget_kills),
        "headshotKills":        _stat("Headshot Kills",     "Weapons",  "weapons",  _i(s.get("headShots"))),
        "bodyKills":            _stat("Body Kills",         "Weapons",  "weapons",  body_kills),
        "adsKills":             _stat("ADS Kills",          "Weapons",  "weapons",  _i(divided.get("ads"))),
        "hipfireKills":         _stat("Hipfire Kills",      "Weapons",  "weapons",  _i(divided.get("hipfire"))),
        "multiKills":           _stat("Multi Kills",        "Game",     "game",     _i(divided.get("multiKills"))),
        "takedownKills":        _stat("Takedown Kills",     "Combat",   "combat",   _i(s.get("playerTakeDowns"))),
        "weaponMultiKills":     _stat("Weapon Multi Kills", "Weapons",  "weapons",  weapon_multi),
        "vehicleMultiKills":    _stat("Vehicle Multi Kills","Vehicles", "vehicles", vehicle_multi),
        "gadgetMultiKills":     _stat("Gadget Multi Kills", "Gadgets",  "gadgets",  gadget_multi),
        "roadKills":            _stat("Road Kills",         "Vehicles", "vehicles", _i(divided.get("roadkills")) or road_kills),

        "assists":              _stat("Assists",            "Combat",   "combat",   assists),
        "vehicleAssists":       _stat("Vehicle Assists",    "Vehicles", "vehicles", vehicle_assists),
        "passengerAssists":     _stat("Passenger Assists",  "Vehicles", "vehicles", passenger_assists),
        "driverAssists":        _stat("Driver Assists",     "Vehicles", "vehicles", driver_assists),

        "revives":              _stat("Revives",            "Combat",   "combat",   _i(s.get("revives"))),
        "deaths":               _stat("Deaths",             "Combat",   "combat",   deaths),

        "damageDealt":          _stat("Damage Dealt",        "Combat",   "combat",   damage),
        "weaponDamageDealt":    _stat("Weapon Damage Dealt", "Weapons",  "weapons",  weapon_damage),
        "vehicleDamageDealt":   _stat("Vehicle Damage Dealt","Vehicles", "vehicles", vehicle_damage),
        "gadgetDamageDealt":    _stat("Gadget Damage Dealt", "Gadgets",  "gadgets",  gadget_damage),

        "shotsFired":           _stat("Shots Fired",         "Weapons",  "weapons",  _i(s.get("shotsFired"))),
        "shotsHit":             _stat("Shots Hit",           "Weapons",  "weapons",  _i(s.get("shotsHit"))),

        "vehiclesDestroyed":    _stat("Vehicles Destroyed",  "Vehicles", "vehicles", _i(s.get("vehiclesDestroyed"))),
        "vehicleCallIns":       _stat("Vehicle Call-ins",    "Vehicles", "vehicles", vehicle_deployments),

        "gadgetUses":           _stat("Gadget Uses",         "Gadgets",  "gadgets",  gadget_uses),
        "repairs":              _stat("Repairs",             "Gadgets",  "gadgets",  _i(s.get("repairs"))),

        "defendedObjectives":   _stat("Defended Objectives", "Objective","objective",_i(objective.get("defused"))),
        "objectiveTime":        _stat("Objective Time",      "Objective","objective",_i(obj_time_dict.get("total")), display_type="TimeSeconds"),
        "objectivesDestroyed":  _stat("Objectives Destroyed","Objective","objective",_i(objective.get("destroyed"))),
        "objectivesCaptured":   _stat("Objectives Captured", "Objective","objective",_i(objective.get("captured"))),
        "objectivesArmed":      _stat("Objectives Armed",    "Objective","objective",_i(objective.get("armed"))),
        "objectivesDisarmed":   _stat("Objectives Disarmed", "Objective","objective",_i(objective.get("neutralized"))),
        "intelPickedUp":        _stat("Intel Picked Up",     "Objective","objective",0),
        "defendedSectors":      _stat("Defended Sectors",    "Objective","objective",_i(sector.get("captured"))),

        "healthRestored":       _stat("Health Restored",     "Support",  "support",  _i(s.get("heals"))),
        "playersResupplied":    _stat("Players Resupplied",  "Objective","objective",_i(s.get("resupplies"))),
        "spots":                _stat("Spots",               "Support",  "support",  _i(s.get("enemiesSpotted"))),

        "scorePerMinute":       _stat("Score/Min",           "Game",     "game",     _dpm(total_score, seconds_played), display_type="NumberPrecision2"),
        "killsPerMinute":       _stat("Kills/Min",           "Combat",   "combat",   _f(s.get("killsPerMinute")) or _kpm(kills, seconds_played), display_type="NumberPrecision2"),
        "killsPerMatch":        _stat("Kills/Match",         "Combat",   "combat",   _f(s.get("killsPerMatch")) or _safe_div(kills, matches_played, 2), display_type="NumberPrecision2"),
        "damagePerMinute":      _stat("Dmg/Min",             "Combat",   "combat",   _f(s.get("damagePerMinute")) or _dpm(damage, seconds_played), display_type="NumberPrecision2"),
        "damagePerMatch":       _stat("Dmg/Match",           "Combat",   "combat",   _f(s.get("damagePerMatch")) or _safe_div(damage, matches_played, 2), display_type="NumberPrecision2"),

        "kdRatio":              _stat("K/D",                 "Combat",   "combat",   _f(s.get("killDeath")) or _safe_div(kills, deaths, 2), display_type="NumberPrecision2"),
        "kdaRatio":             _stat("KDA",                 "Combat",   "combat",   _safe_div(kills + assists, deaths, 2), display_type="NumberPrecision2"),
        "playerKd":             _stat("Player K/D",          "Combat",   "combat",   _f(s.get("infantryKillDeath")) or _safe_div(_i(divided.get("human")), deaths, 2), display_type="NumberPrecision2"),
        "playerKillsPerMinute": _stat("Player Kills/Min",    "Combat",   "combat",   _kpm(_i(divided.get("human")), seconds_played), display_type="NumberPrecision2"),

        "headshotPercentage":   _stat("HS%",                 "Weapons",  "weapons",  _f(s.get("headshots")), display_type="NumberPercentage"),
        "wlPercentage":         _stat("Win %",               "Game",     "game",     _f(total_win_percent), display_type="NumberPercentage"),
        "objectiveTimePct":     _stat("Obj Time %",          "Objective","objective",_safe_div(_i(obj_time_dict.get("total")) * 100.0, seconds_played, 1), display_type="NumberPercentage"),
    }

    return {
        "type": "overview",
        "attributes": {},
        "metadata": {},
        "expiryDate": EPOCH_ZERO,
        "stats": stats_out,
    }


# ------------------------------------------------------------------
# per-group segment builders
# ------------------------------------------------------------------

def _fmt_img(item: Dict[str, Any]) -> str:
    img = item.get("image") or item.get("altImage")
    if img:
        return img
    
    item_id = str(item.get("id", "")).strip().lower()
    name = str(item.get("name", "")).strip()
    class_name = str(item.get("className", "")).strip()
    map_name = str(item.get("mapName", "")).strip()
    
    if item_id == "lvllvlmpsubsurface" or map_name == "Hagental Base":
        return "https://image.battlefield.su/bf6/maps/hagental_base.jpg"
    if item_id == "kit_kit_engineer" or class_name == "Engineer" or name == "Engineer":
        return "https://image.battlefield.su/bf6/classes/white/Engineer.svg"
    
    return ""


def _gamemode_stats_block(it: Dict[str, Any]) -> Dict[str, Any]:
    kills = _i(it.get("kills"))
    deaths = _i(it.get("deaths"))
    matches = _i(it.get("matches"))
    wins = _i(it.get("wins"))
    losses = _i(it.get("losses"))
    assists = _i(it.get("killAssists"))
    score = _i(it.get("scoreIn"))
    seconds = _i(it.get("secondsPlayed"))
    dpm = _f(it.get("dpm"))
    damage = _i(it.get("damage")) or int(round(dpm * (seconds / 60.0))) if seconds else 0
    obj_time = _i(it.get("objectiveTime"))
    return {
        "matchesPlayed":       _stat("Matches Played",      "Game",     "game",     matches),
        "matchesWon":          _stat("Wins",                "Game",     "game",     wins),
        "matchesLost":         _stat("Losses",              "Game",     "game",     losses),
        "timePlayed":          _stat("Time Played",         "Game",     "game",     seconds, display_type="TimeSeconds"),
        "kills":               _stat("Kills",               "Combat",   "combat",   kills),
        "deaths":              _stat("Deaths",              "Combat",   "combat",   deaths),
        "assists":             _stat("Assists",             "Combat",   "combat",   assists),
        "revives":             _stat("Revives",             "Support",  "support",  _i(it.get("revives"))),
        "score":               _stat("Score",               "Game",     "game",     score),
        "headshotKills":       _stat("Headshot Kills",      "Combat",   "combat",   _i(it.get("headshotKills"))),
        "killsWithVehicles":   _stat("Vehicle Kills",       "Vehicles", "vehicles", 0),
        "damageDealt":         _stat("Damage Dealt",        "Combat",   "combat",   damage),
        "healthRestored":      _stat("Health Restored",     "Support",  "support",  0),
        "defendedObjectives":  _stat("Defended Objectives", "Objective","objective",_i(it.get("objectivesDefended"))),
        "objectiveTime":       _stat("Objective Time",      "Objective","objective",obj_time, display_type="TimeSeconds"),
        "objectivesDestroyed": _stat("Objectives Destroyed","Objective","objective",0),
        "objectivesCaptured":  _stat("Objectives Captured", "Objective","objective",_i(it.get("objectivesCaptured"))),
        "objectivesArmed":     _stat("Objectives Armed",    "Objective","objective",0),
        "objectivesDisarmed":  _stat("Objectives Disarmed", "Objective","objective",0),
        "intelPickedUp":       _stat("Intel Picked Up",     "Objective","objective",0),
        "defendedSectors":     _stat("Defended Sectors",    "Objective","objective",0),
        "playersResupplied":   _stat("Players Resupplied",  "Support",  "support",  0),
        "spots":               _stat("Spots",               "Support",  "support",  _i(it.get("spots"))),
        "vehiclesDestroyed":   _stat("Vehicles Destroyed",  "Vehicles", "vehicles", _i(it.get("vehiclesDestroyedWith"))),
        "killsPerMinute":      _stat("Kills/min",           "Combat",   "combat",   _f(it.get("kpm")) or _kpm(kills, seconds), display_type="Number"),
        "killsPerMatch":       _stat("Kills/match",         "Combat",   "combat",   _safe_div(kills, matches, 2), display_type="Number"),
        "damagePerMinute":     _stat("Damage/min",          "Combat",   "combat",   dpm, display_type="Number"),
        "wlPercentage":        _stat("W/L %",               "Game",     "game",     _f(_pct(it.get("winPercent"))), display_type="Percentage"),
        "objectiveTimePct":    _stat("Objective Time %",    "Objective","objective",_safe_div(obj_time * 100.0, seconds, 1), display_type="Percentage"),
        "kdRatio":             _stat("K/D",                 "Combat",   "combat",   _f(it.get("killDeath")) or _safe_div(kills, deaths, 2), display_type="Ratio"),
        "kdaRatio":            _stat("KDA",                 "Combat",   "combat",   _safe_div(kills + assists, deaths, 2), display_type="Ratio"),
        "scorePerMinute":      _stat("Score/min",           "Game",     "game",     _dpm(score, seconds), display_type="Number"),
    }


def _gamemode_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    for gm in _filter_aggregates(stats.get("gameModes")):
        segs.append({
            "type": "gamemode",
            "attributes": {"key": f"gm_{gm.get('id','')}"},
            "metadata": {
                "name": gm.get("gamemodeName", ""),
                "imageUrl": _fmt_img(gm),
                "category": "gm_mp",
                "categoryName": "Multiplayer",
            },
            "expiryDate": EPOCH_ZERO,
            "stats": _gamemode_stats_block(gm),
        })
    return segs


def _gamemode_category_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    for g in _filter_aggregates(stats.get("gameModeGroups")):
        gid = g.get("id", "")
        segs.append({
            "type": "gamemode-category",
            "attributes": {"key": f"gm_{gid}"},
            "metadata": {
                "name": g.get("gamemodeName", "Gamemodes"),
                "imageUrl": _fmt_img(g) or None,
            },
            "expiryDate": EPOCH_ZERO,
            "stats": _gamemode_stats_block(g),
        })
    return segs


def _kit_stats_block(it: Dict[str, Any]) -> Dict[str, Any]:
    kills = _i(it.get("kills"))
    deaths = _i(it.get("deaths"))
    assists = _i(it.get("assists"))
    seconds = _i(it.get("secondsPlayed"))
    spawns = _i(it.get("spawns"))
    score = _i(it.get("score"))
    revives = _i(it.get("revives"))
    return {
        "timePlayed":     _stat("Time Played",    "Game",    "game",    seconds, display_type="TimeSeconds"),
        "deployments":    _stat("Deployments",    "Game",    "game",    spawns),
        "kills":          _stat("Kills",          "Combat",  "combat",  kills),
        "assists":        _stat("Assists",        "Combat",  "combat",  assists),
        "revives":        _stat("Revives",        "Support", "support", revives),
        "deaths":         _stat("Deaths",         "Combat",  "combat",  deaths),
        "score":          _stat("Score",          "Game",    "game",    score),
        "masteryLevel":   _stat("Mastery Level",  "Game",    "game",    0),
        "killsPerMinute": _stat("Kills/min",      "Combat",  "combat",  _f(it.get("kpm")) or _kpm(kills, seconds), display_type="Number"),
        "kdRatio":        _stat("K/D",            "Combat",  "combat",  _f(it.get("killDeath")) or _safe_div(kills, deaths, 2), display_type="Ratio"),
        "kdaRatio":       _stat("KDA",            "Combat",  "combat",  _safe_div(kills + assists, deaths, 2), display_type="Ratio"),
        "scorePerMinute": _stat("Score/min",      "Game",    "game",    _dpm(score, seconds), display_type="Number"),
    }


def _kit_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "type": "kit",
        "attributes": {"key": f"kit_{c.get('id','')}"},
        "metadata": {
            "name": c.get("className", ""),
            "imageUrl": _fmt_img(c),
        },
        "expiryDate": EPOCH_ZERO,
        "stats": _kit_stats_block(c),
    } for c in _filter_aggregates(stats.get("classes"))]


def _kit_category_segment(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    classes = _filter_aggregates(stats.get("classes"))
    if not classes:
        return []
    # aggregate across all classes
    agg: Dict[str, int] = {
        "kills": 0, "deaths": 0, "assists": 0, "secondsPlayed": 0,
        "spawns": 0, "score": 0,
    }
    for c in classes:
        for k in agg:
            agg[k] += _i(c.get(k))
    agg_block = {**agg, "kpm": 0, "killDeath": 0}
    return [{
        "type": "kit-category",
        "attributes": {"key": "kit"},
        "metadata": {"name": "Classes", "imageUrl": None},
        "expiryDate": EPOCH_ZERO,
        "stats": _kit_stats_block(agg_block),
    }]


def _level_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    for m in _filter_aggregates(stats.get("maps")):
        seconds = _i(m.get("secondsPlayed"))
        wins = _i(m.get("wins")); losses = _i(m.get("losses"))
        matches = _i(m.get("matches"))
        segs.append({
            "type": "level",
            "attributes": {"key": f"lvl{m.get('id','')}"},
            "metadata": {
                "name": m.get("mapName", ""),
                "imageUrl": _fmt_img(m),
            },
            "expiryDate": EPOCH_ZERO,
            "stats": {
                "timePlayed":    _stat("Time Played",    "Game", "game", seconds, display_type="TimeSeconds"),
                "matchesPlayed": _stat("Matches Played", "Game", "game", matches),
                "matchesWon":    _stat("Wins",           "Game", "game", wins),
                "matchesLost":   _stat("Losses",         "Game", "game", losses),
                "wlPercentage":  _stat("W/L %",          "Game", "game", _pct(m.get("winPercent")), display_type="Percentage"),
            },
        })
    return segs

def _weapon_stats_block(it: Dict[str, Any]) -> Dict[str, Any]:
    kills = _i(it.get("kills"))
    time_eq = _i(it.get("timeEquipped"))
    shots_fired = _i(it.get("shotsFired"))
    shots_hit = _i(it.get("shotsHit"))
    damage = _i(it.get("damage"))
    accuracy = _f(it.get("accuracy")) or _safe_div(shots_hit * 100.0, shots_fired, 2)
    headshot_pct = _f(it.get("headshots")) or _safe_div(_i(it.get("headshotKills")) * 100.0, kills, 2)
    return {
        "kills":                _stat("Kills",              "Combat",  "combat",  kills),
        "damageDealt":          _stat("Damage Dealt",       "Combat",  "combat",  damage),
        "shotsFired":           _stat("Shots Fired",        "Combat",  "combat",  shots_fired),
        "shotsHit":             _stat("Shots Hit",          "Combat",  "combat",  shots_hit),
        "adsKills":             _stat("ADS Kills",          "Combat",  "combat",  _i(it.get("scopedKills"))),
        "hipfireKills":         _stat("Hipfire Kills",      "Combat",  "combat",  _i(it.get("hipfireKills"))),
        "headshotKills":        _stat("Headshot Kills",     "Combat",  "combat",  _i(it.get("headshotKills"))),
        "timePlayed":           _stat("Time Equipped",      "Game",    "game",    time_eq, display_type="TimeSeconds"),
        "multiKills":           _stat("Multi Kills",        "Combat",  "combat",  _i(it.get("multiKills"))),
        "bodyKills":            _stat("Body Kills",         "Combat",  "combat",  _i(it.get("bodyKills"))),
        "masteryLevel":         _stat("Mastery Level",      "Game",    "game",    0),
        "assistDamage":         _stat("Assist Damage",      "Combat",  "combat",  _i(it.get("assistsDamage"))),
        "deployments":          _stat("Deployments",        "Game",    "game",    _i(it.get("spawns"))),
        "deployments2":         _stat("Deployments (alt)",  "Game",    "game",    _i(it.get("spawns"))),
        "killsPerMinute":       _stat("Kills/min",          "Combat",  "combat",  _f(it.get("killsPerMinute")) or _kpm(kills, time_eq), display_type="Number"),
        "damagePerMinute":      _stat("Damage/min",         "Combat",  "combat",  _f(it.get("damagePerMinute")) or _dpm(damage, time_eq), display_type="Number"),
        "assistDamagePerMinute":_stat("Assist DMG/min",     "Combat",  "combat",  _dpm(_i(it.get("assistsDamage")), time_eq), display_type="Number"),
        "shotsAccuracy":        _stat("Accuracy",           "Combat",  "combat",  accuracy, display_type="Percentage"),
        "headshotPercentage":   _stat("Headshot %",         "Combat",  "combat",  headshot_pct, display_type="Percentage"),
    }


def _weapon_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    # find category slug for each weapon from weaponGroups by type-name match if present
    group_by_name = {(g.get("groupName") or "").lower(): g.get("id")
                      for g in _filter_aggregates(stats.get("weaponGroups"))}
    segs: List[Dict[str, Any]] = []
    for w in _filter_aggregates(stats.get("weapons")):
        cat_name = w.get("type") or ""
        cat_id = group_by_name.get(cat_name.lower(), "")
        segs.append({
            "type": "weapon",
            "attributes": {"key": f"wp_{w.get('id','')}"},
            "metadata": {
                "name": w.get("weaponName", ""),
                "imageUrl": _fmt_img(w),
                "category": f"wp_{cat_id}" if cat_id else "",
                "categoryName": cat_name,
            },
            "expiryDate": EPOCH_ZERO,
            "stats": _weapon_stats_block(w),
        })
    return segs


def _weapon_category_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "type": "weapon-category",
        "attributes": {"key": f"wp_{g.get('id','')}"},
        "metadata": {
            "name": g.get("groupName", ""),
            "imageUrl": _fmt_img(g) or None,
        },
        "expiryDate": EPOCH_ZERO,
        "stats": _weapon_stats_block(g),
    } for g in _filter_aggregates(stats.get("weaponGroups"))]


def _vehicle_stats_block(it: Dict[str, Any]) -> Dict[str, Any]:
    kills = _i(it.get("kills"))
    time_in = _i(it.get("timeIn"))
    damage = _i(it.get("damage"))
    return {
        "timePlayed":       _stat("Time Played",        "Game",     "game",     time_in, display_type="TimeSeconds"),
        "kills":            _stat("Kills",              "Combat",   "combat",   kills),
        "roadKills":        _stat("Road Kills",         "Combat",   "combat",   _i(it.get("roadKills"))),
        "damageDealt":      _stat("Damage Dealt",       "Combat",   "combat",   damage),
        "destroyedWith":    _stat("Vehicles Destroyed With","Vehicles","vehicles",_i(it.get("vehiclesDestroyedWith"))),
        "damageDealtTo":    _stat("Damage Dealt To",    "Vehicles", "vehicles", _i(it.get("damageTo"))),
        "destroyedOfType":  _stat("Destroyed (of type)","Vehicles", "vehicles", _i(it.get("destroyed"))),
        "assists":          _stat("Assists",            "Combat",   "combat",   _i(it.get("assists"))),
        "passengerAssists": _stat("Passenger Assists",  "Vehicles", "vehicles", _i(it.get("passengerAssists"))),
        "driverAssists":    _stat("Driver Assists",     "Vehicles", "vehicles", _i(it.get("driverAssists"))),
        "deployments":      _stat("Deployments",        "Game",     "game",     _i(it.get("spawns"))),
        "multiKills":       _stat("Multi Kills",        "Combat",   "combat",   _i(it.get("multiKills"))),
        "distanceTraveled": _stat("Distance Traveled",  "Vehicles", "vehicles", _i(it.get("distanceTraveled"))),
        "callIns":          _stat("Call-Ins",           "Vehicles", "vehicles", _i(it.get("spawns"))),
        "damagePerMinute":  _stat("Damage/min",         "Combat",   "combat",   _dpm(damage, time_in), display_type="Number"),
        "killsPerMinute":   _stat("Kills/min",          "Combat",   "combat",   _f(it.get("killsPerMinute")) or _kpm(kills, time_in), display_type="Number"),
    }


def _vehicle_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    group_by_name = {(g.get("groupName") or "").lower(): g.get("id")
                      for g in _filter_aggregates(stats.get("vehicleGroups"))}
    segs: List[Dict[str, Any]] = []
    for v in _filter_aggregates(stats.get("vehicles")):
        cat_name = v.get("type") or ""
        cat_id = group_by_name.get(cat_name.lower(), "")
        segs.append({
            "type": "vehicle",
            "attributes": {"key": f"veh_{v.get('id','')}"},
            "metadata": {
                "name": v.get("vehicleName", ""),
                "imageUrl": _fmt_img(v),
                "category": f"veh_{cat_id}" if cat_id else "",
                "categoryName": cat_name,
            },
            "expiryDate": EPOCH_ZERO,
            "stats": _vehicle_stats_block(v),
        })
    return segs


def _vehicle_category_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "type": "vehicle-category",
        "attributes": {"key": f"veh_{g.get('id','')}"},
        "metadata": {
            "name": g.get("groupName", ""),
            "imageUrl": _fmt_img(g) or None,
        },
        "expiryDate": EPOCH_ZERO,
        "stats": _vehicle_stats_block(g),
    } for g in _filter_aggregates(stats.get("vehicleGroups"))]


def _gadget_stats_block(it: Dict[str, Any]) -> Dict[str, Any]:
    kills = _i(it.get("kills"))
    seconds = _i(it.get("secondsPlayed"))
    damage = _i(it.get("damage"))
    return {
        "timePlayed":            _stat("Time Played",        "Game",     "game",     seconds, display_type="TimeSeconds"),
        "uses":                  _stat("Uses",               "Gadgets",  "gadgets",  _i(it.get("uses"))),
        "kills":                 _stat("Kills",              "Combat",   "combat",   kills),
        "assists":               _stat("Assists",            "Combat",   "combat",   _i(it.get("assists"))),
        "assistDamage":          _stat("Assist Damage",      "Combat",   "combat",   _i(it.get("assistsDamage"))),
        "takedownKills":         _stat("Takedown Kills",     "Combat",   "combat",   0),
        "damageDealt":           _stat("Damage Dealt",       "Combat",   "combat",   damage),
        "multiKills":            _stat("Multi Kills",        "Combat",   "combat",   _i(it.get("multiKills"))),
        "repairs":               _stat("Repairs",            "Support",  "support",  0),
        "healthRestored":        _stat("Health Restored",    "Support",  "support",  0),
        "spots":                 _stat("Spots",              "Support",  "support",  _i(it.get("spots"))),
        "spotAssists":           _stat("Spot Assists",       "Support",  "support",  _i(it.get("spotAssists"))),
        "deployments2":          _stat("Deployments",        "Game",     "game",     _i(it.get("spawns"))),
        "deployments3":          _stat("Deployments (alt)",  "Game",     "game",     _i(it.get("spawns"))),
        "killsPerMinute":        _stat("Kills/min",          "Combat",   "combat",   _f(it.get("kpm")) or _kpm(kills, seconds), display_type="Number"),
        "damagePerMinute":       _stat("Damage/min",         "Combat",   "combat",   _f(it.get("dpm")) or _dpm(damage, seconds), display_type="Number"),
        "assistDamagePerMinute": _stat("Assist DMG/min",     "Combat",   "combat",   _dpm(_i(it.get("assistsDamage")), seconds), display_type="Number"),
    }


def _gadget_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs: List[Dict[str, Any]] = []
    for g in _filter_aggregates(stats.get("gadgets")):
        cat_name = g.get("type") or ""
        segs.append({
            "type": "gadget",
            "attributes": {"key": f"gad_{g.get('id','')}"},
            "metadata": {
                "name": g.get("gadgetName", ""),
                "subtitle": None,
                "imageUrl": _fmt_img(g),
                "className": cat_name,
                "subcategoryName": None,
                "categoryName": cat_name,
            },
            "expiryDate": EPOCH_ZERO,
            "stats": _gadget_stats_block(g),
        })
    return segs


def _gadget_category_segments(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{
        "type": "gadget-category",
        "attributes": {"key": f"gad_{g.get('id','')}"},
        "metadata": {
            "name": g.get("groupName", ""),
            "subtitle": None,
            "imageUrl": _fmt_img(g) or None,
        },
        "expiryDate": EPOCH_ZERO,
        "stats": _gadget_stats_block(g),
    } for g in _filter_aggregates(stats.get("gadgetGroups"))]


# ------------------------------------------------------------------
# top-level
# ------------------------------------------------------------------

AVAILABLE_SEGMENT_TYPES = [
    "overview",
    "gamemode", "gamemode-category",
    "kit", "kit-category",
    "level",
    "weapon", "weapon-category",
    "vehicle", "vehicle-category",
    "gadget", "gadget-category",
]


def _platform_info(stats: Dict[str, Any], profile: Optional[Dict[str, Any]], name: str, platform: str) -> Dict[str, Any]:
    user_id = str(stats.get("userId") or stats.get("id") or "")
    user_name = stats.get("userName") or name
    # avatar = stats.get("avatar") or ""
    avatar = ""
    # platform slug: gametools uses 'pc' which we map to 'origin' to match TRN
    slug_map = {"pc": "origin", "xbox": "xbl", "ps": "psn"}
    return {
        "platformSlug": slug_map.get(platform, platform),
        "platformUserId": None,
        "platformUserHandle": user_name,
        "platformUserIdentifier": user_id,
        "avatarUrl": avatar or None,
        "additionalParameters": None,
    }


def _user_info(stats: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "userId": None,
        "isPremium": False,
        "isVerified": False,
        "isInfluencer": False,
        "isPartner": False,
        "countryCode": None,
        "customAvatarUrl": None,
        "customHeroUrl": None,
        "customAvatarFrame": None,
        "customAvatarFrameInfo": None,
        "premiumDuration": None,
        "socialAccounts": [],
        # v0.0.4.6: _pick_player_card handles both the normal shape and the
        # degraded `other[*].playerProfiles[0]` fallback shape, and returns
        # an empty dict (never raises) when nothing is parseable.
        "badges": _i(_pick_player_card(profile).get("badges")) if profile else None,
        "pageviews": 0,
        "xpTier": None,
        "isSuspicious": None,
    }


# ------------------------------------------------------------------
# delta-match builder (TRN `matches` shape)
# ------------------------------------------------------------------

def _sub_nn(new_v, old_v) -> int:
    d = _i(new_v) - _i(old_v)
    return d if d > 0 else 0


def _subtract_top_counters(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Subtract top-level scalar counters and structured sub-dicts so the result
    looks like a corrected gametools-stats payload representing the delta
    between two snapshots."""
    keys = [
        "kills", "deaths", "wins", "loses", "matchesPlayed", "secondsPlayed",
        "shotsFired", "shotsHit", "killAssists", "revives", "heals",
        "resupplies", "repairs", "vehiclesDestroyed", "enemiesSpotted",
        "headShots", "damage", "saviorKills", "thrownThrowables",
        "gadgetsDestoyed", "playerTakeDowns", "squadmateRevive",
    ]
    d: Dict[str, Any] = {k: _sub_nn(new.get(k, 0), old.get(k, 0)) for k in keys}

    # passthrough identity so platformInfo still renders
    for k in ("userId", "userName", "id", "platform", "avatar"):
        d[k] = new.get(k) or old.get(k)

    # dividedKills — subtract each numeric child
    old_dk = old.get("dividedKills") or {}
    new_dk = new.get("dividedKills") or {}
    d["dividedKills"] = {
        k: _sub_nn(new_dk.get(k, 0), old_dk.get(k, 0))
        for k in ("ads", "grenades", "hipfire", "longDistance", "melee",
                   "multiKills", "passenger", "vehicle", "roadkills", "human")
    }

    # objective
    old_obj = old.get("objective") or {}
    new_obj = new.get("objective") or {}
    old_t = old_obj.get("time") or {}
    new_t = new_obj.get("time") or {}
    d["objective"] = {
        "time": {k: _sub_nn(new_t.get(k, 0), old_t.get(k, 0))
                  for k in ("total", "attacked", "defended")},
        "armed":        _sub_nn(new_obj.get("armed", 0),       old_obj.get("armed", 0)),
        "captured":     _sub_nn(new_obj.get("captured", 0),    old_obj.get("captured", 0)),
        "neutralized":  _sub_nn(new_obj.get("neutralized", 0), old_obj.get("neutralized", 0)),
        "defused":      _sub_nn(new_obj.get("defused", 0),     old_obj.get("defused", 0)),
        "destroyed":    _sub_nn(new_obj.get("destroyed", 0),   old_obj.get("destroyed", 0)),
    }

    # sector
    d["sector"] = {
        "captured": _sub_nn((new.get("sector") or {}).get("captured", 0),
                              (old.get("sector") or {}).get("captured", 0))
    }

    # distanceTraveled — kept as-is (not really a delta concept)
    d["distanceTraveled"] = new.get("distanceTraveled") or old.get("distanceTraveled") or {}

    # derived per-minute stats are computed downstream; clear legacy ones
    d["winPercent"] = 0
    d["killsPerMinute"] = 0
    d["damagePerMinute"] = 0
    d["killsPerMatch"] = 0
    d["damagePerMatch"] = 0
    d["headshots"] = 0
    d["killDeath"] = 0
    d["infantryKillDeath"] = 0
    return d


def _index_by_id(arr):
    out = {}
    for it in _filter_aggregates(arr):
        if isinstance(it, dict) and "id" in it:
            out[str(it["id"])] = it
    return out


_LIST_SPEC = [
    # (key, numeric-fields-to-subtract, identity/metadata fields to copy from new side)
    ("weapons",        ["kills", "damage", "assistsDamage", "bodyKills", "headshotKills",
                         "hipfireKills", "multiKills", "shotsHit", "shotsFired",
                         "scopedKills", "spawns", "timeEquipped"],
                        ["weaponName", "type", "image", "altImage"]),
    ("weaponGroups",   ["kills", "damage", "assistsDamage", "bodyKills", "headshotKills",
                         "hipfireKills", "multiKills", "shotsHit", "shotsFired",
                         "scopedKills", "spawns", "timeEquipped"],
                        ["groupName"]),
    ("vehicles",       ["kills", "damage", "spawns", "roadKills", "passengerAssists",
                         "multiKills", "distanceTraveled", "driverAssists",
                         "vehiclesDestroyedWith", "assists", "damageTo",
                         "destroyed", "timeIn"],
                        ["vehicleName", "type", "image", "altImage"]),
    ("vehicleGroups",  ["kills", "damage", "spawns", "roadKills", "passengerAssists",
                         "multiKills", "distanceTraveled", "driverAssists",
                         "vehiclesDestroyedWith", "assists", "damageTo",
                         "destroyed", "timeIn"],
                        ["groupName"]),
    ("gadgets",        ["kills", "assistsDamage", "assists", "spotAssists", "spots",
                         "spawns", "damage", "uses", "multiKills",
                         "vehiclesDestroyedWith", "secondsPlayed"],
                        ["gadgetName", "type", "image"]),
    ("gadgetGroups",   ["kills", "assistsDamage", "assists", "spotAssists", "spots",
                         "spawns", "damage", "uses", "multiKills",
                         "vehiclesDestroyedWith", "secondsPlayed"],
                        ["groupName"]),
    ("classes",        ["kills", "deaths", "spawns", "score", "assists", "secondsPlayed"],
                        ["className", "image", "altImage"]),
    ("maps",           ["wins", "losses", "matches", "secondsPlayed"],
                        ["mapName", "image"]),
    ("gameModes",      ["kills", "deaths", "wins", "losses", "killAssists", "matches",
                         "repairs", "revives", "spots", "objectiveTime",
                         "objectivesCaptured", "objectivesDefended", "scoreIn",
                         "headshotKills", "secondsPlayed"],
                        ["gamemodeName", "image", "altImage"]),
    ("gameModeGroups", ["kills", "deaths", "wins", "losses", "killAssists", "matches",
                         "repairs", "revives", "spots", "objectiveTime",
                         "objectivesCaptured", "objectivesDefended", "scoreIn",
                         "headshotKills", "secondsPlayed"],
                        ["gamemodeName", "image", "altImage"]),
]


def _subtract_lists(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """For each tracked list (weapons/vehicles/...), subtract by id. Drop items
    where no tracked field changed. Metadata fields are copied from the newer
    entry when available."""
    out: Dict[str, Any] = {}
    for key, num_fields, meta_fields in _LIST_SPEC:
        old_map = _index_by_id(old.get(key))
        new_map = _index_by_id(new.get(key))
        items = []
        for _id in sorted(set(old_map) | set(new_map)):
            o = old_map.get(_id, {})
            n = new_map.get(_id, {})
            base = n if n else o
            it = {"id": _id}
            for mf in meta_fields:
                it[mf] = base.get(mf)
            changed = False
            for f in num_fields:
                v = _sub_nn(n.get(f, 0), o.get(f, 0))
                it[f] = v
                if v > 0:
                    changed = True
            if changed:
                items.append(it)
        out[key] = items
    return out


def _build_delta_stats(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> Dict[str, Any]:
    """Compose a gametools-shaped dict whose values are the delta (new - old)."""
    old = old or {}
    new = new or {}
    d = _subtract_top_counters(old, new)
    d.update(_subtract_lists(old, new))
    return d


def _flatten_stat_block(block: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Take a TRN-stat-object block (as produced by _*_stats_block) and flatten
    each entry to its raw `value`, rounding floats to 2dp to match TRN output."""
    out: Dict[str, Any] = {}
    for k, v in block.items():
        val = v.get("value") if isinstance(v, dict) else v
        if isinstance(val, float):
            val = round(val, 2)
        out[k] = val
    return out


def _flat_group_items(stats: Dict[str, Any],
                       list_key: str,
                       key_prefix: str,
                       name_field: str,
                       stats_block_fn) -> List[Dict[str, Any]]:
    """Turn a delta-stats list (e.g. delta['weapons']) into TRN match-metadata
    group items: `{key, metadata:{name,imageUrl,[category,categoryName]}, stats:<flat>}`."""
    rows: List[Dict[str, Any]] = []
    for it in _filter_aggregates(stats.get(list_key)):
        md: Dict[str, Any] = {
            "name": it.get(name_field, ""),
            "imageUrl": _fmt_img(it),
        }
        if "type" in it:
            md["categoryName"] = it.get("type") or ""
        rows.append({
            "key": f"{key_prefix}{it.get('id','')}",
            "metadata": md,
            "stats": _flatten_stat_block(stats_block_fn(it)),
        })
    return rows


def _flat_level_items(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for m in _filter_aggregates(stats.get("maps")):
        rows.append({
            "key": f"lvl{m.get('id','')}",
            "metadata": {"name": m.get("mapName", ""), "imageUrl": _fmt_img(m)},
            "stats": {
                "timePlayed":    _i(m.get("secondsPlayed")),
                "matchesPlayed": _i(m.get("matches")),
                "matchesWon":    _i(m.get("wins")),
                "matchesLost":   _i(m.get("losses")),
                "wlPercentage":  round(_safe_div(_i(m.get("wins")) * 100.0, _i(m.get("matches")), 2), 2),
            },
        })
    return rows


def build_trn_match(
    old_stats: Optional[Dict[str, Any]],
    new_stats: Dict[str, Any],
    *,
    account_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    match_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce one TRN-`matches` entry (delta) from two gametools snapshots.

    Output shape (per entry):
        { "attributes": {"type":"delta","id":...},
          "metadata":   {"timestamp":...},
          "segments":   [ overview-segment-with-flat-group-metadata ],
          "streams":    [],
          "expiryDate": "0001-01-01T00:00:00+00:00" }
    """
    from datetime import datetime, timezone
    import uuid

    ts = timestamp or datetime.now(timezone.utc).isoformat()
    match_id = match_id or str(uuid.uuid4())

    delta = _build_delta_stats(old_stats, new_stats)

    # account id: prefer numeric for shape-parity with TRN
    acct_raw = account_id or new_stats.get("userId") or new_stats.get("id") or ""
    try:
        acct: Any = int(acct_raw) if str(acct_raw).isdigit() else str(acct_raw)
    except Exception:
        acct = str(acct_raw)

    overview_seg = _overview_segment(delta, None)
    overview_seg["attributes"] = {"accountId": acct}
    overview_seg["metadata"] = {
        "gamemodes": _flat_group_items(delta, "gameModes",   "gm_",  "gamemodeName", _gamemode_stats_block),
        "kits":      _flat_group_items(delta, "classes",     "kit_", "className",    _kit_stats_block),
        "levels":    _flat_level_items(delta),
        "weapons":   _flat_group_items(delta, "weapons",     "wp_",  "weaponName",   _weapon_stats_block),
        "vehicles":  _flat_group_items(delta, "vehicles",    "veh_", "vehicleName",  _vehicle_stats_block),
        "gadgets":   _flat_group_items(delta, "gadgets",     "gad_", "gadgetName",   _gadget_stats_block),
    }

    return {
        "attributes": {"type": "delta", "id": match_id},
        "metadata": {"timestamp": ts},
        "segments": [overview_seg],
        "streams": [],
        "expiryDate": EPOCH_ZERO,
    }


# ------------------------------------------------------------------
# delta match built from TWO already-converted TRN profiles
# ------------------------------------------------------------------

# Types that carry a raw-counter value that should be subtracted directly.
_COUNTER_TYPES = {"Number", "TimeSeconds"}

# Per-stat-key formulas for recomputing derived stats from delta counters.
# op:
#   "per_min"  -> numerator / (denominator / 60)
#   "ratio"    -> numerator / denominator (2dp)
#   "pct"      -> numerator * 100 / denominator (1dp)
# numerator / denominator may be a tuple of keys — their values get summed.

_OVERVIEW_FORMULAS: Dict[str, tuple] = {
    "scorePerMinute":       ("per_min", "score",         "timePlayed"),
    "killsPerMinute":       ("per_min", "kills",         "timePlayed"),
    "killsPerMatch":        ("ratio",   "kills",         "matchesPlayed"),
    "damagePerMinute":      ("per_min", "damageDealt",   "timePlayed"),
    "damagePerMatch":       ("ratio",   "damageDealt",   "matchesPlayed"),
    "kdRatio":              ("ratio",   "kills",         "deaths"),
    "kdaRatio":             ("ratio",   ("kills","assists"), "deaths"),
    "playerKd":             ("ratio",   "playerKills",   "deaths"),
    "playerKillsPerMinute": ("per_min", "playerKills",   "timePlayed"),
    "headshotPercentage":   ("pct",     "headshotKills", "weaponKills"),
    "wlPercentage":         ("pct",     "matchesWon",    ("matchesWon","matchesLost")),
    "objectiveTimePct":     ("pct",     "objectiveTime", "timePlayed"),
}

_GAMEMODE_FORMULAS: Dict[str, tuple] = {
    "killsPerMinute":   ("per_min", "kills",          "timePlayed"),
    "killsPerMatch":    ("ratio",   "kills",          "matchesPlayed"),
    "damagePerMinute":  ("per_min", "damageDealt",    "timePlayed"),
    "scorePerMinute":   ("per_min", "score",          "timePlayed"),
    "wlPercentage":     ("pct",     "matchesWon",     ("matchesWon","matchesLost")),
    "objectiveTimePct": ("pct",     "objectiveTime",  "timePlayed"),
    "kdRatio":          ("ratio",   "kills",          "deaths"),
    "kdaRatio":         ("ratio",   ("kills","assists"), "deaths"),
}

_KIT_FORMULAS: Dict[str, tuple] = {
    "killsPerMinute": ("per_min", "kills",          "timePlayed"),
    "kdRatio":        ("ratio",   "kills",          "deaths"),
    "kdaRatio":       ("ratio",   ("kills","assists"), "deaths"),
    "scorePerMinute": ("per_min", "score",          "timePlayed"),
}

_WEAPON_FORMULAS: Dict[str, tuple] = {
    "killsPerMinute":        ("per_min", "kills",         "timePlayed"),
    "damagePerMinute":       ("per_min", "damageDealt",   "timePlayed"),
    "assistDamagePerMinute": ("per_min", "assistDamage",  "timePlayed"),
    "shotsAccuracy":         ("pct",     "shotsHit",      "shotsFired"),
    "headshotPercentage":    ("pct",     "headshotKills", "kills"),
}

_VEHICLE_FORMULAS: Dict[str, tuple] = {
    "killsPerMinute":  ("per_min", "kills",       "timePlayed"),
    "damagePerMinute": ("per_min", "damageDealt", "timePlayed"),
}

_GADGET_FORMULAS: Dict[str, tuple] = {
    "killsPerMinute":        ("per_min", "kills",        "timePlayed"),
    "damagePerMinute":       ("per_min", "damageDealt",  "timePlayed"),
    "assistDamagePerMinute": ("per_min", "assistDamage", "timePlayed"),
}

_LEVEL_FORMULAS: Dict[str, tuple] = {
    "wlPercentage": ("pct", "matchesWon", ("matchesWon","matchesLost")),
}

_FORMULAS_BY_SEGMENT: Dict[str, Dict[str, tuple]] = {
    "overview":           _OVERVIEW_FORMULAS,
    "gamemode":           _GAMEMODE_FORMULAS,
    "gamemode-category":  _GAMEMODE_FORMULAS,
    "kit":                _KIT_FORMULAS,
    "kit-category":       _KIT_FORMULAS,
    "weapon":             _WEAPON_FORMULAS,
    "weapon-category":    _WEAPON_FORMULAS,
    "vehicle":            _VEHICLE_FORMULAS,
    "vehicle-category":   _VEHICLE_FORMULAS,
    "gadget":             _GADGET_FORMULAS,
    "gadget-category":    _GADGET_FORMULAS,
    "level":              _LEVEL_FORMULAS,
}


def _stat_value(block: Dict[str, Dict[str, Any]], key) -> float:
    """Read a counter value from a TRN stats block; supports tuple-of-keys sum."""
    if isinstance(key, tuple):
        return sum(_stat_value(block, k) for k in key)
    entry = block.get(key) or {}
    return _f(entry.get("value", 0))


def _recompute(op: str, num: float, den: float) -> float:
    if op == "per_min":
        return round(num / (den / 60.0), 2) if den > 0 else 0.0
    if op == "ratio":
        return round(num / den, 2) if den > 0 else 0.0
    if op == "pct":
        return round(num * 100.0 / den, 1) if den > 0 else 0.0
    return 0.0


def _format_display(value, display_type: str) -> str:
    if display_type == "TimeSeconds":
        return _fmt_time(value)
    if display_type == "NumberPrecision2":
        return _fmt_np2(value)
    if display_type == "NumberPercentage":
        return _fmt_pct(value)
    if display_type == "Ratio":
        return _fmt_ratio(value)
    if display_type == "Percentage":
        return _fmt_pct(value)
    return _fmt_int(value) if isinstance(value, int) else _fmt_num(_f(value), 2)


def _subtract_stats_block(
    old_block: Dict[str, Dict[str, Any]],
    new_block: Dict[str, Dict[str, Any]],
    formulas: Dict[str, tuple],
) -> Dict[str, Dict[str, Any]]:
    """Return a new TRN stats block where counters are (new - old) and derived
    stats are recomputed from those deltas via the per-segment formula map.
    Metadata (display names etc.) is copied from the new block."""
    out: Dict[str, Dict[str, Any]] = {}
    # first pass: subtract counters, zero derived stats
    for k, new_stat in (new_block or {}).items():
        if not isinstance(new_stat, dict):
            continue
        dt = new_stat.get("displayType", "Number")
        old_stat = (old_block or {}).get(k) or {}
        new_v = new_stat.get("value") or 0
        old_v = old_stat.get("value") or 0
        if dt in _COUNTER_TYPES:
            val: Any = _f(new_v) - _f(old_v)
            if val < 0:
                val = 0
            if dt == "Number" and isinstance(new_v, int) and isinstance(old_v, int):
                val = int(val)
        else:
            val = 0
        out[k] = {
            "displayName":     new_stat.get("displayName", ""),
            "displayCategory": new_stat.get("displayCategory", ""),
            "category":        new_stat.get("category", ""),
            "metadata":        dict(new_stat.get("metadata") or {}),
            "value":           val,
            "displayValue":    _format_display(val, dt),
            "displayType":     dt,
        }
    # second pass: recompute derived stats from delta counters
    for k, rule in (formulas or {}).items():
        if k not in out:
            continue
        op, num_key, den_key = rule
        num = _stat_value(out, num_key)
        den = _stat_value(out, den_key)
        val = _recompute(op, num, den)
        out[k]["value"] = val
        out[k]["displayValue"] = _format_display(val, out[k]["displayType"])
    return out


def _block_has_counter_movement(block: Dict[str, Dict[str, Any]]) -> bool:
    """True if any counter in the (post-subtraction) block moved."""
    for v in (block or {}).values():
        if not isinstance(v, dict):
            continue
        if v.get("displayType") in _COUNTER_TYPES and _f(v.get("value")) > 0:
            return True
    return False


def _flat_stats(block: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (block or {}).items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        val = v.get("value")
        if isinstance(val, float):
            val = round(val, 2)
        out[k] = val
    return out


def _seg_key(seg: Dict[str, Any]) -> tuple:
    return (seg.get("type", ""), (seg.get("attributes") or {}).get("key", ""))


def build_trn_match_from_profiles(
    old_profile: Optional[Dict[str, Any]],
    new_profile: Dict[str, Any],
    *,
    account_id: Optional[Any] = None,
    timestamp: Optional[str] = None,
    match_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce one TRN-`matches` entry (delta) from two converted TRN profile
    dicts. Counters are subtracted, derived stats recomputed. Per-group rows
    (gamemode/kit/weapon/vehicle/gadget/level) that had no counter movement
    are dropped. If `old_profile` is None/empty the "delta" equals the whole
    current profile (first-seen case)."""
    from datetime import datetime, timezone
    import uuid

    ts = timestamp or datetime.now(timezone.utc).isoformat()
    mid = match_id or str(uuid.uuid4())

    new_data = (new_profile or {}).get("data") or {}
    old_data = (old_profile or {}).get("data") or {}
    new_segs = new_data.get("segments") or []
    old_segs = old_data.get("segments") or []
    old_by_key = {_seg_key(s): s for s in old_segs}

    # --- 1. compute the delta overview segment (counter-subtract + recompute)
    new_overview = next((s for s in new_segs if s.get("type") == "overview"), None) or {}
    old_overview = next((s for s in old_segs if s.get("type") == "overview"), None) or {}
    overview_delta_stats = _subtract_stats_block(
        old_overview.get("stats") or {},
        new_overview.get("stats") or {},
        _OVERVIEW_FORMULAS,
    )

    # careerPlayerRank metadata: preserve rankImage/rankName + add delta
    rank_meta = dict(((new_overview.get("stats") or {}).get("careerPlayerRank") or {}).get("metadata") or {})
    rank_new = _i(((new_overview.get("stats") or {}).get("careerPlayerRank") or {}).get("value"))
    rank_old = _i(((old_overview.get("stats") or {}).get("careerPlayerRank") or {}).get("value"))
    rank_meta["delta"] = rank_new - rank_old
    if "careerPlayerRank" in overview_delta_stats:
        overview_delta_stats["careerPlayerRank"]["value"] = rank_new
        overview_delta_stats["careerPlayerRank"]["displayValue"] = _fmt_int(rank_new)
        overview_delta_stats["careerPlayerRank"]["metadata"] = rank_meta

    # --- 2. per-group delta metadata: iterate each new per-group segment,
    # subtract against its matching old segment (zero if missing), keep only
    # groups where counters moved, flatten.
    def _delta_group(seg_type: str, key_prefix_expected: str) -> List[Dict[str, Any]]:
        out_rows: List[Dict[str, Any]] = []
        formulas = _FORMULAS_BY_SEGMENT.get(seg_type, {})
        for ns in new_segs:
            if ns.get("type") != seg_type:
                continue
            os = old_by_key.get(_seg_key(ns), {})
            delta_block = _subtract_stats_block(
                os.get("stats") or {},
                ns.get("stats") or {},
                formulas,
            )
            if not _block_has_counter_movement(delta_block):
                continue
            md = dict(ns.get("metadata") or {})
            # keep only name/imageUrl/category/categoryName in match metadata
            md_clean = {
                "name":     md.get("name", ""),
                "imageUrl": md.get("imageUrl"),
            }
            if "category" in md or "categoryName" in md:
                md_clean["category"]     = md.get("category", "")
                md_clean["categoryName"] = md.get("categoryName", "")
            out_rows.append({
                "key":      (ns.get("attributes") or {}).get("key", ""),
                "metadata": md_clean,
                "stats":    _flat_stats(delta_block),
            })
        return out_rows

    metadata_groups = {
        "gamemodes": _delta_group("gamemode", "gm_"),
        "kits":      _delta_group("kit",      "kit_"),
        "levels":    _delta_group("level",    "lvl"),
        "weapons":   _delta_group("weapon",   "wp_"),
        "vehicles":  _delta_group("vehicle",  "veh_"),
        "gadgets":   _delta_group("gadget",   "gad_"),
    }

    # --- 3. account id: prefer numeric to match TRN's `accountId`
    if account_id is None:
        account_id = ((new_data.get("platformInfo") or {}).get("platformUserIdentifier")) \
                     or ((old_data.get("platformInfo") or {}).get("platformUserIdentifier"))
    try:
        acct: Any = int(account_id) if account_id is not None and str(account_id).isdigit() else account_id
    except Exception:
        acct = account_id

    overview_seg = {
        "type":       "overview",
        "attributes": {"accountId": acct},
        "metadata":   metadata_groups,
        "expiryDate": EPOCH_ZERO,
        "stats":      overview_delta_stats,
    }

    return {
        "attributes": {"type": "delta", "id": mid},
        "metadata":   {"timestamp": ts},
        "segments":   [overview_seg],
        "expiryDate": EPOCH_ZERO,
    }


def build_trn_matches_response(
    matches: List[Dict[str, Any]],
    *,
    account_id: Optional[str] = None,
    next_page: Optional[int] = None,
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap a list of TRN-match dicts into the full `matches` wire shape."""
    from datetime import datetime, timezone
    try:
        acct: Any = int(account_id) if account_id and str(account_id).isdigit() else account_id
    except Exception:
        acct = account_id
    return {
        "data": {
            "matches": matches,
            "metadata": {"next": next_page} if next_page is not None else {},
            "paginationType": "Page",
            "requestingPlayerAttributes": {"accountId": acct} if acct is not None else {},
            "expiryDate": expiry_date or datetime.now(timezone.utc).isoformat(),
        }
    }


def build_trn_profile(
    stats: Dict[str, Any],
    profile: Optional[Dict[str, Any]] = None,
    *,
    name: str = "",
    platform: str = "pc",
    update_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a Gametools stats (+ optional profile) payload into the TRN
    battlefield-tracker profile shape: `{ "data": { ... } }`."""
    stats = stats or {}

    segments: List[Dict[str, Any]] = []
    segments.append(_overview_segment(stats, profile))
    segments.extend(_gamemode_category_segments(stats))
    segments.extend(_gamemode_segments(stats))
    segments.extend(_kit_category_segment(stats))
    segments.extend(_kit_segments(stats))
    segments.extend(_level_segments(stats))
    segments.extend(_weapon_category_segments(stats))
    segments.extend(_weapon_segments(stats))
    segments.extend(_vehicle_category_segments(stats))
    segments.extend(_vehicle_segments(stats))
    segments.extend(_gadget_category_segments(stats))
    segments.extend(_gadget_segments(stats))

    return {
        "data": {
            "platformInfo": _platform_info(stats, profile, name, platform),
            "userInfo": _user_info(stats, profile),
            "metadata": {"updateHash": update_hash or ""},
            "segments": segments,
            "availableSegments": [{"type": t, "attributes": {}, "metadata": {}} for t in AVAILABLE_SEGMENT_TYPES],
            "expiryDate": EPOCH_ZERO,
        }
    }
