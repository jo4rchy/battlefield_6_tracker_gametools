import requests
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Union


# ============================================================
# 0) time helpers
# ============================================================

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_zero_delta_match(match_obj: Dict[str, Any]) -> bool:
    """Return True if `match_obj` is a "junk" delta — every overview counter is
    zero AND every per-group metadata bucket (gamemodes/kits/levels/weapons/
    vehicles/gadgets) is empty. These rows show up when the gametools update
    hash flips without any real gameplay (e.g. per-class secondsPlayed jitter
    in `_apply_corrections`); the resulting match is meaningless on the
    /matches feed because it advertises no movement.

    `careerPlayerRank.value` is intentionally excluded because it carries the
    player's *current* rank (not a counter) — its delta lives in
    `careerPlayerRank.metadata.delta`. We do NOT treat a non-zero rank delta
    as movement on its own: rank can wobble due to leaderboard recalcs even
    when no counters changed, and a "match" showing only "rank -1" with all
    other stats at zero is exactly the noise the user wants gone.
    """
    segs = (match_obj or {}).get("segments") or []
    if not segs:
        return False
    overview = next((s for s in segs if isinstance(s, dict) and s.get("type") == "overview"), None)
    if not overview:
        return False

    stats = overview.get("stats") or {}
    for key, blk in stats.items():
        if key == "careerPlayerRank":
            continue
        if not isinstance(blk, dict):
            continue
        try:
            v = float(blk.get("value") or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v != 0:
            return False

    md = overview.get("metadata") or {}
    for groupname in ("gamemodes", "kits", "levels", "weapons", "vehicles", "gadgets"):
        if md.get(groupname):
            return False

    return True


# ============================================================
# 1) Gametools client (with your correction logic)
# ============================================================

class GametoolsClient:
    def __init__(self):
        self.base_url_profile = "https://api.gametools.network/bf6/profile/"
        self.base_url_stats = "https://api.gametools.network/bf6/stats/"
        self.params = {
            "raw": "false",
            "format_values": "true",
            "skip_battlelog": "true"
        }

    def _apply_corrections(self, data_stats: Dict[str, Any]) -> Dict[str, Any]:
        """Correct the buggy aggregate secondsPlayed reported by gametools by
        summing per-class secondsPlayed."""
        try:
            if isinstance(data_stats, dict) and "classes" in data_stats and isinstance(data_stats["classes"], list):
                corrected_seconds = sum(int(c.get("secondsPlayed", 0) or 0) for c in data_stats["classes"] if isinstance(c, dict) and c.get("className") != "All")
                data_stats["_rawSecondsPlayed"] = data_stats.get("secondsPlayed", 0)
                data_stats["secondsPlayed"] = corrected_seconds

                hours = corrected_seconds // 3600
                rem = corrected_seconds % 3600
                minutes = rem // 60
                data_stats["timePlayed"] = f"{int(hours)} H, {minutes} M" if hours > 0 else f"{minutes} minutes"

                total_kills = int(data_stats.get("kills", 0) or 0)
                if corrected_seconds > 0:
                    data_stats["killsPerMinute"] = round(total_kills / (corrected_seconds / 60), 2)
                else:
                    data_stats["killsPerMinute"] = 0.0
            # -------------------------------------------

            return data_stats
        except Exception as e:
            print(f"[GametoolsClient._apply_corrections] Error: {e}")
            return data_stats

    def fetch_stats(self, name: str, platform: str = "steam") -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/stats/ by player name (corrected)."""
        params = {**self.params, "name": name, "platform": platform}
        try:
            r = requests.get(self.base_url_stats, params=params, timeout=10)
            r.raise_for_status()
            return self._apply_corrections(r.json())
        except Exception as e:
            print(f"[GametoolsClient.fetch_stats] Error: {e}")
            return None

    def fetch_profile(self, name: str, platform: str = "steam") -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/profile/ by player name (rank / playerCard metadata)."""
        params = {**self.params, "name": name, "platform": platform}
        try:
            r = requests.get(self.base_url_profile, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[GametoolsClient.fetch_profile] Error: {e}")
            return None

    def fetch_stats_by_id(self, player_id: Union[str, int], platform: str = "steam") -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/stats/ keyed by nucleus/player id. Preferred for the
        background poller because it is stable across name changes."""
        params = {
            **self.params,
            "playerid":  str(player_id),
            "nucleus_id": str(player_id),
            "platform":  platform,
        }
        try:
            r = requests.get(self.base_url_stats, params=params, timeout=10)
            r.raise_for_status()
            return self._apply_corrections(r.json())
        except Exception as e:
            print(f"[GametoolsClient.fetch_stats_by_id] Error: {e}")
            return None

    def fetch_profile_by_id(self, player_id: Union[str, int], platform: str = "steam") -> Optional[Dict[str, Any]]:
        """Fetch gametools /bf6/profile/ keyed by nucleus/player id."""
        params = {
            **self.params,
            "playerid":  str(player_id),
            "nucleus_id": str(player_id),
            "platform":  platform,
        }
        try:
            r = requests.get(self.base_url_profile, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[GametoolsClient.fetch_profile_by_id] Error: {e}")
            return None

    def fetch_full(self, name: str, platform: str = "steam") -> Dict[str, Any]:
        """Fetch both stats + profile in one call (by name); either may be None on error."""
        return {
            "stats": self.fetch_stats(name, platform),
            "profile": self.fetch_profile(name, platform),
        }

    def fetch_full_by_id(self, player_id: Union[str, int], platform: str = "steam") -> Dict[str, Any]:
        """Fetch both stats + profile in one call (by id); either may be None on error."""
        return {
            "stats": self.fetch_stats_by_id(player_id, platform),
            "profile": self.fetch_profile_by_id(player_id, platform),
        }
        
    def fetch_stats_mock_old(self, name: str, platform: str = "steam") -> Optional[Dict[str, Any]]:
        data = json.load(open("old.json", "r", encoding="utf-8"))
        return data
    
    def fetch_stats_mock_new(self, name: str, platform: str = "steam") -> Optional[Dict[str, Any]]:
        data = json.load(open("new.json", "r", encoding="utf-8"))
        return data
    
    # --- canonical fields used to decide "did the player play more?" ---------
    HASH_COUNTER_FIELDS = [
        "kills", "deaths", "wins", "loses", "matchesPlayed", "secondsPlayed",
        "shotsFired", "shotsHit", "killAssists", "revives", "heals",
        "resupplies", "repairs", "vehiclesDestroyed", "enemiesSpotted",
        "headShots", "damage", "saviorKills", "thrownThrowables",
        "gadgetsDestoyed", "playerTakeDowns", "squadmateRevive",
    ]

    def generate_update_hash(self, data: Dict[str, Any]) -> Optional[str]:
        """Canonical sha1 of every counter we care about — any real movement
        invalidates the hash so upsert_with_delta will save a new snapshot +
        produce a delta match. Truncated to 8 chars for readability."""
        if not data:
            return None
        parts = [f"{k}={int(data.get(k, 0) or 0)}" for k in self.HASH_COUNTER_FIELDS]
        # nested bits that also change independently
        obj = data.get("objective") or {}
        t = obj.get("time") or {}
        parts += [
            f"obj.time.total={int(t.get('total', 0) or 0)}",
            f"obj.captured={int(obj.get('captured', 0) or 0)}",
            f"obj.defused={int(obj.get('defused', 0) or 0)}",
            f"sector.captured={int((data.get('sector') or {}).get('captured', 0) or 0)}",
        ]
        dk = data.get("dividedKills") or {}
        for k in ("human", "ads", "hipfire", "melee", "multiKills", "vehicle", "roadkills"):
            parts.append(f"dk.{k}={int(dk.get(k, 0) or 0)}")
        blob = "|".join(parts)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


# ============================================================
# 2) Delta + TRN-like matches builder
#    - key uses gametools item["id"]
#    - imageUrl uses item["image"] or item["altImage"]
# ============================================================

def _int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def _float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def _sub_nonneg(new_v, old_v) -> int:
    d = _int(new_v) - _int(old_v)
    return d if d > 0 else 0

def safe_ratio(n, d, ndigits=3) -> float:
    n = _float(n, 0.0)
    d = _float(d, 0.0)
    if d <= 0:
        return round(n, ndigits)
    return round(n / d, ndigits)

def safe_kpm(kills, seconds_played, ndigits=2) -> float:
    k = _float(kills, 0.0)
    sp = _float(seconds_played, 0.0)
    if sp <= 0:
        return 0.0
    return round(k / (sp / 60.0), ndigits)

def trn_stat(display_name: str, display_category: str, category: str, value, display_type: str = "Number") -> Dict[str, Any]:
    return {
        "displayName": display_name,
        "displayCategory": display_category,
        "category": category,
        "metadata": {},
        "value": value,
        "displayValue": str(value),
        "displayType": display_type,
    }

def _list_to_map_by_id(arr: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    m: Dict[str, Dict[str, Any]] = {}
    for it in arr or []:
        if isinstance(it, dict) and "id" in it:
            m[str(it["id"])] = it
    return m

def _diff_item(old_it: Dict[str, Any], new_it: Dict[str, Any], field_map: Dict[str, Union[str, Tuple[str, str]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for out_f, fields in field_map.items():
        if isinstance(fields, (tuple, list)):
            new_f = fields[0]
            old_f = fields[1] if len(fields) > 1 else fields[0]
        else:
            new_f = old_f = fields
        out[out_f] = _sub_nonneg(new_it.get(new_f, 0), old_it.get(old_f, 0))
    return out

def _item_name(it: Dict[str, Any], name_fields: List[str]) -> str:
    for f in name_fields:
        v = it.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(it.get("id", ""))

def _item_image(it: Dict[str, Any]) -> str:
    if not isinstance(it, dict):
        return ""
    
    img = it.get("image") or it.get("altImage")
    if img:
        return img
    
    item_id = str(it.get("id", "")).strip().lower()
    name = str(it.get("name", "")).strip()
    class_name = str(it.get("className", "")).strip()
    map_name = str(it.get("mapName", "")).strip()
    
    if item_id == "lvllvlmpsubsurface" or map_name == "Hagental Base":
        return "https://image.battlefield.su/bf6/maps/hagental_base.jpg"
    if item_id == "kit_kit_engineer" or class_name == "Engineer" or name == "Engineer":
        return "https://image.battlefield.su/bf6/classes/white/Engineer.svg"
    
    return ""


# ---- overview delta ----
TOP_COUNTER_FIELDS = [
    "kills", "deaths", "wins", "loses", "matchesPlayed", "secondsPlayed",
    "shotsFired", "shotsHit",
    "killAssists",
    "revives", "heals", "resupplies", "repairs",
    "vehiclesDestroyed", "enemiesSpotted",
    "damage",
    "headshots", "headshotKills",
    "distanceTraveled",
    "objective", "sector", "XP",
    "squadmateRevive",
    "thrownThrowables",
    "gadgetsDestoyed",
    "playerTakeDowns",
    "saviorKills",
]

def compute_overview_delta(old_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, Any]:
    old_data = old_data or {}
    new_data = new_data or {}

    d = {f: _sub_nonneg(new_data.get(f, 0), old_data.get(f, 0)) for f in TOP_COUNTER_FIELDS}
    d["killsPerMinute"] = safe_kpm(d.get("kills", 0), d.get("secondsPlayed", 0), ndigits=2)
    d["kdRatio"] = safe_ratio(d.get("kills", 0), max(1, d.get("deaths", 0)), ndigits=3)

    wins = _int(d.get("wins", 0))
    loses = _int(d.get("loses", 0))
    total = wins + loses
    d["winPercent"] = round((wins / total) * 100.0, 2) if total > 0 else 0.0
    return d


# ---- group delta engine ----
def diff_list_by_id(
    old_list: Optional[List[Dict[str, Any]]],
    new_list: Optional[List[Dict[str, Any]]],
    *,
    name_fields: List[str],
    field_map: Dict[str, Union[str, Tuple[str, str]]],
    derive_kpm: Optional[Tuple[str, str]] = None,
    derive_kd: Optional[Tuple[str, str]] = None,
    keep_zero_rows: bool = False
) -> List[Dict[str, Any]]:
    old_map = _list_to_map_by_id(old_list or [])
    new_map = _list_to_map_by_id(new_list or [])
    ids = sorted(set(old_map.keys()) | set(new_map.keys()))

    out: List[Dict[str, Any]] = []
    for _id in ids:
        o = old_map.get(_id, {}) or {}
        n = new_map.get(_id, {}) or {}

        base_item = n if n else o
        row: Dict[str, Any] = {
            "id": base_item.get("id", _id),
            "name": _item_name(base_item, name_fields),
            "imageUrl": _item_image(base_item),
        }

        deltas = _diff_item(o, n, field_map)
        row.update(deltas)

        if derive_kpm:
            kf, sf = derive_kpm
            row["killsPerMinute"] = safe_kpm(row.get(kf, 0), row.get(sf, 0), ndigits=2)
        if derive_kd:
            kf, df = derive_kd
            row["kdRatio"] = safe_ratio(row.get(kf, 0), max(1, row.get(df, 0)), ndigits=3)

        changed = any(_int(v) != 0 for v in deltas.values())
        if changed or keep_zero_rows:
            out.append(row)

    return out


# ---- field maps (match your gametools shape; missing fields become 0) ----
FIELD_MAP_GAME_MODE = {
    "matchesPlayed": "matches",
    "matchesWon": "wins",
    "matchesLost": "losses",
    "timePlayed": "secondsPlayed",
    "kills": "kills",
    "deaths": "deaths",
    "assists": "killAssists",
    "repairs": "repairs",
    "revives": "revives",
    "spots": "spots",
    "objectiveTime": "objectiveTime",
    "objectivesCaptured": "objectivesCaptured",
    "objectivesDefended": "objectivesDefended",
    "score": "scoreIn",
    "headshotKills": "headshotKills",
    # ⚠️ dpm 是“每分钟”不是累计；建议不放 delta（默认注释掉）
    # "damage": "dpm",
}

FIELD_MAP_MAP = {
    "matchesPlayed": "matches",
    "matchesWon": "wins",
    "matchesLost": "losses",
    "timePlayed": "secondsPlayed",
}

FIELD_MAP_CLASS = {
    "kills": "kills",
    "deaths": "deaths",
    "assists": "assists",
    "timePlayed": "secondsPlayed",
    "score": "score",
    "spawns": "spawns",
}

FIELD_MAP_WEAPON = {
    "kills": "kills",
    "headshotKills": "headshotKills",
    "bodyKills": "bodyKills",
    "hipfireKills": "hipfireKills",
    "scopedKills": "scopedKills",
    "multiKills": "multiKills",
    "damage": "damage",
    "assistsDamage": "assistsDamage",
    "shotsFired": "shotsFired",
    "shotsHit": "shotsHit",
    "timeEquipped": "timeEquipped",
    "spawns": "spawns",
}

FIELD_MAP_VEHICLE = {
    "kills": "kills",
    "damage": "damage",
    "spawns": "spawns",
    "roadKills": "roadKills",
    "multiKills": "multiKills",
    "distanceTraveled": "distanceTraveled",
    "driverAssists": "driverAssists",
    "passengerAssists": "passengerAssists",
    "assists": "assists",
    "vehiclesDestroyedWith": "vehiclesDestroyedWith",
    "destroyed": "destroyed",
    "timeIn": "timeIn",
    "damageTo": "damageTo",
}

FIELD_MAP_GADGET = {
    "kills": "kills",
    "damage": "damage",
    "uses": "uses",
    "assists": "assists",
    "assistsDamage": "assistsDamage",
    "spots": "spots",
    "spotAssists": "spotAssists",
    "vehiclesDestroyedWith": "vehiclesDestroyedWith",
    "multiKills": "multiKills",
    "secondsPlayed": "secondsPlayed",
}

def compute_all_group_deltas(old_data: Dict[str, Any], new_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    old_data = old_data or {}
    new_data = new_data or {}
    return {
        "gamemodes": diff_list_by_id(
            old_data.get("gameModes"), new_data.get("gameModes"),
            name_fields=["gamemodeName", "name", "title"],
            field_map=FIELD_MAP_GAME_MODE,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "gamemodeGroups": diff_list_by_id(
            old_data.get("gameModeGroups"), new_data.get("gameModeGroups"),
            name_fields=["groupName", "name", "title", "gamemodeName"],
            field_map=FIELD_MAP_GAME_MODE,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "maps": diff_list_by_id(
            old_data.get("maps"), new_data.get("maps"),
            name_fields=["mapName", "name", "title"],
            field_map=FIELD_MAP_MAP,
        ),
        "kits": diff_list_by_id(
            old_data.get("classes"), new_data.get("classes"),
            name_fields=["className", "name", "title"],
            field_map=FIELD_MAP_CLASS,
            derive_kpm=("kills", "timePlayed"),
            derive_kd=("kills", "deaths"),
        ),
        "weapons": diff_list_by_id(
            old_data.get("weapons"), new_data.get("weapons"),
            name_fields=["weaponName", "name", "title"],
            field_map=FIELD_MAP_WEAPON,
            derive_kpm=("kills", "timeEquipped"),
        ),
        "weaponGroups": diff_list_by_id(
            old_data.get("weaponGroups"), new_data.get("weaponGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_WEAPON,
            derive_kpm=("kills", "timeEquipped"),
        ),
        "vehicles": diff_list_by_id(
            old_data.get("vehicles"), new_data.get("vehicles"),
            name_fields=["vehicleName", "name", "title"],
            field_map=FIELD_MAP_VEHICLE,
            derive_kpm=("kills", "timeIn"),
        ),
        "vehicleGroups": diff_list_by_id(
            old_data.get("vehicleGroups"), new_data.get("vehicleGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_VEHICLE,
            derive_kpm=("kills", "timeIn"),
        ),
        "gadgets": diff_list_by_id(
            old_data.get("gadgets"), new_data.get("gadgets"),
            name_fields=["gadgetName", "name", "title"],
            field_map=FIELD_MAP_GADGET,
            derive_kpm=("kills", "secondsPlayed"),
        ),
        "gadgetGroups": diff_list_by_id(
            old_data.get("gadgetGroups"), new_data.get("gadgetGroups"),
            name_fields=["groupName", "name", "title"],
            field_map=FIELD_MAP_GADGET,
            derive_kpm=("kills", "secondsPlayed"),
        ),
    }


def _meta_row(key_prefix: str, row: Dict[str, Any], stats_map: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": f"{key_prefix}{row.get('id')}",
        "metadata": {
            "name": row.get("name", str(row.get("id", ""))),
            "imageUrl": row.get("imageUrl", ""),
        },
        "stats": {k: row.get(k, v) for k, v in stats_map.items()},
    }


def build_trn_like_delta_match(
    account_id: Union[str, int],
    old_data: Dict[str, Any],
    new_data: Dict[str, Any],
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Thin wrapper so legacy callers still work; real work lives in
    converter.build_trn_match which produces the full TRN match shape."""
    try:
        from .converter import build_trn_match
    except ImportError:
        from app.converter import build_trn_match  # type: ignore
    return build_trn_match(
        old_stats=old_data,
        new_stats=new_data,
        account_id=str(account_id) if account_id is not None else None,
        timestamp=timestamp,
    )


def _legacy_build_trn_like_delta_match(
    account_id: Union[str, int],
    old_data: Dict[str, Any],
    new_data: Dict[str, Any],
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Original inline builder, kept for reference only."""
    ts = timestamp or _utc_iso()

    overview = compute_overview_delta(old_data, new_data)
    groups = compute_all_group_deltas(old_data, new_data)

    segment_stats = {
        "matchesPlayed": trn_stat("Matches Played", "General", "general", overview.get("matchesPlayed", 0)),
        "matchesWon": trn_stat("Matches Won", "General", "general", overview.get("wins", 0)),
        "matchesLost": trn_stat("Matches Lost", "General", "general", overview.get("loses", 0)),
        "timePlayed": trn_stat("Time Played", "General", "general", overview.get("secondsPlayed", 0), display_type="TimeSeconds"),

        "kills": trn_stat("Kills", "Combat", "combat", overview.get("kills", 0)),
        "deaths": trn_stat("Deaths", "Combat", "combat", overview.get("deaths", 0)),
        "assists": trn_stat("Assists", "Combat", "combat", overview.get("killAssists", 0)),
        "kdRatio": trn_stat("K/D Ratio", "Combat", "combat", overview.get("kdRatio", 0.0), display_type="Ratio"),
        "killsPerMinute": trn_stat("Kills/Min", "Combat", "combat", overview.get("killsPerMinute", 0.0), display_type="Number"),

        "shotsFired": trn_stat("Shots Fired", "Combat", "combat", overview.get("shotsFired", 0)),
        "shotsHit": trn_stat("Shots Hit", "Combat", "combat", overview.get("shotsHit", 0)),
        "damage": trn_stat("Damage", "Combat", "combat", overview.get("damage", 0)),
        "revives": trn_stat("Revives", "Support", "support", overview.get("revives", 0)),
        "heals": trn_stat("Heals", "Support", "support", overview.get("heals", 0)),
        "resupplies": trn_stat("Resupplies", "Support", "support", overview.get("resupplies", 0)),
        "repairs": trn_stat("Repairs", "Support", "support", overview.get("repairs", 0)),
        "vehiclesDestroyed": trn_stat("Vehicles Destroyed", "Combat", "combat", overview.get("vehiclesDestroyed", 0)),
        "enemiesSpotted": trn_stat("Enemies Spotted", "Combat", "combat", overview.get("enemiesSpotted", 0)),
        "winPercent": trn_stat("Win %", "General", "general", overview.get("winPercent", 0.0), display_type="Percentage"),
    }

    gm_stats_map = {
        "matchesPlayed": 0, "matchesWon": 0, "matchesLost": 0, "timePlayed": 0,
        "kills": 0, "deaths": 0, "assists": 0, "kdRatio": 0.0, "killsPerMinute": 0.0,
        "score": 0, "repairs": 0, "revives": 0, "spots": 0,
        "objectiveTime": 0, "objectivesCaptured": 0, "objectivesDefended": 0,
        "headshotKills": 0,
    }
    kit_stats_map = {
        "timePlayed": 0, "kills": 0, "deaths": 0, "assists": 0,
        "kdRatio": 0.0, "killsPerMinute": 0.0,
        "score": 0, "spawns": 0,
    }
    weapon_stats_map = {
        "kills": 0, "headshotKills": 0, "bodyKills": 0, "hipfireKills": 0, "scopedKills": 0,
        "multiKills": 0, "damage": 0, "assistsDamage": 0,
        "shotsFired": 0, "shotsHit": 0, "timeEquipped": 0, "spawns": 0,
        "killsPerMinute": 0.0,
    }
    vehicle_stats_map = {
        "kills": 0, "damage": 0, "spawns": 0, "roadKills": 0, "multiKills": 0,
        "distanceTraveled": 0, "driverAssists": 0, "passengerAssists": 0, "assists": 0,
        "vehiclesDestroyedWith": 0, "destroyed": 0, "timeIn": 0,
        "killsPerMinute": 0.0,
    }
    gadget_stats_map = {
        "kills": 0, "damage": 0, "uses": 0, "assists": 0, "assistsDamage": 0,
        "spots": 0, "spotAssists": 0, "vehiclesDestroyedWith": 0, "multiKills": 0,
        "secondsPlayed": 0, "killsPerMinute": 0.0,
    }
    map_stats_map = {
        "matchesPlayed": 0, "matchesWon": 0, "matchesLost": 0, "timePlayed": 0,
    }

    segment_metadata = {
        "gamemodes":       [_meta_row("gm_",  r, gm_stats_map)     for r in groups["gamemodes"]],
        "kits":            [_meta_row("kit_", r, kit_stats_map)    for r in groups["kits"]],
        "weapons":         [_meta_row("w_",   r, weapon_stats_map) for r in groups["weapons"]],
        "vehicles":        [_meta_row("v_",   r, vehicle_stats_map)for r in groups["vehicles"]],
        "gadgets":         [_meta_row("g_",   r, gadget_stats_map) for r in groups["gadgets"]],
        "levels":            [_meta_row("map_", r, map_stats_map)    for r in groups["maps"]],
    }

    return {
        "attributes": {"type": "delta", "id": str(uuid.uuid4())},
        "metadata": {"timestamp": ts},
        "segments": [{
            "type": "overview",
            "attributes": {"accountId": str(account_id)},
            "metadata": segment_metadata,
            "expiryDate": None,
            "stats": segment_stats,
        }],
        "streams": [],
        "expiryDate": None,
    }


def build_matches_response(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"matches": matches}}


# ============================================================
# 3) StatsStorage (SQLite snapshots + matches)
# ============================================================

class StatsStorage:
    """
    SQLite:
    - snapshots: 每次抓到的“纠错后的 profile JSON”
    - matches: old/new snapshot 做差得到的 TRN-like delta match JSON
    """

    DEFAULT_DB_PATH = "data/bf6_stats.db"

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Per-identifier RLock used to serialize the read-existing → compare →
        # save-match → save-profile flow inside upsert_profile_with_delta. This
        # prevents two concurrent /profile (or poller) calls for the same user
        # from inserting duplicate delta-match rows that describe the same
        # H_old → H_new transition. The dict is intentionally never trimmed —
        # one RLock per known platformUserIdentifier is cheap (~few hundred
        # bytes) and dropping locks would re-introduce the race.
        self._user_locks: Dict[str, threading.RLock] = {}
        self._user_locks_guard = threading.Lock()
        self._init_db()

    def _get_user_lock(self, identifier: str) -> threading.RLock:
        """Return (creating on first use) the RLock dedicated to one user."""
        ident = str(identifier)
        with self._user_locks_guard:
            lk = self._user_locks.get(ident)
            if lk is None:
                lk = threading.RLock()
                self._user_locks[ident] = lk
            return lk

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_db(self):
        cur = self.conn.cursor()
        cur.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        -- profiles: one row per player keyed by platformUserIdentifier.
        -- Stores the current converted TRN profile JSON; overwritten on change.
        CREATE TABLE IF NOT EXISTS profiles (
            platform_user_identifier TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            name TEXT NOT NULL,
            update_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            trn_profile_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_profiles_name
        ON profiles(platform, name);

        -- matches: many rows per player, append-only delta log.
        CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY,
            platform_user_identifier TEXT NOT NULL,
            created_at TEXT NOT NULL,
            from_hash TEXT,
            to_hash TEXT NOT NULL,
            match_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_matches_user_time
        ON matches(platform_user_identifier, created_at);

        CREATE INDEX IF NOT EXISTS idx_matches_to_hash
        ON matches(platform_user_identifier, to_hash);

        -- legacy snapshots table preserved for back-compat; unused by new flow.
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            update_hash TEXT NOT NULL,
            account_id TEXT,
            data_json TEXT NOT NULL
        );
        """)
        self.conn.commit()

        # v0.0.4 cleanup pass: remove any duplicate match rows left over from
        # the v0.0.2 race before we try to install the UNIQUE index below.
        # Strategy: per (platform_user_identifier, COALESCE(from_hash,''),
        # to_hash) triple, keep the row with the smallest rowid (the one
        # SQLite committed first in the original race) and delete the rest.
        # This is idempotent — on a clean DB it deletes 0 rows. We only log
        # when something was actually removed so normal startup stays quiet.
        try:
            cur.execute("""
                DELETE FROM matches
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM matches
                    GROUP BY platform_user_identifier,
                             COALESCE(from_hash, ''),
                             to_hash
                )
            """)
            removed = cur.rowcount or 0
            self.conn.commit()
            if removed > 0:
                print(f"[StatsStorage] cleanup: removed {removed} duplicate match row(s) from v0.0.2 race")
        except sqlite3.OperationalError as e:
            print(f"[StatsStorage] duplicate-match cleanup skipped: {e}")

        # v0.0.4 cleanup pass #2: remove "zero-delta" junk matches — rows where
        # every overview counter is 0 and every per-group metadata bucket is
        # empty. These were produced when the gametools update hash flipped
        # without any real gameplay (e.g. per-class secondsPlayed jitter from
        # _apply_corrections), and they're noise on the /matches feed. We scan
        # each match_json in Python rather than relying on SQLite json_extract
        # because we need to walk a full stats dict and the expression would
        # be unwieldy. Idempotent — on a clean DB this deletes 0 rows. We
        # also keep this generous: anything with even one non-zero counter
        # OR a populated per-group bucket survives.
        try:
            cur.execute("SELECT id, match_json FROM matches")
            junk_ids: List[str] = []
            for row in cur.fetchall():
                try:
                    obj = json.loads(row["match_json"])
                except Exception:
                    continue  # leave unparseable rows alone
                if _is_zero_delta_match(obj):
                    junk_ids.append(row["id"])
            if junk_ids:
                cur.executemany(
                    "DELETE FROM matches WHERE id = ?",
                    [(mid,) for mid in junk_ids],
                )
                self.conn.commit()
                print(f"[StatsStorage] cleanup: removed {len(junk_ids)} zero-delta match row(s) (no counter movement)")
        except sqlite3.OperationalError as e:
            print(f"[StatsStorage] zero-delta-match cleanup skipped: {e}")

        # v0.0.4 defense-in-depth: a UNIQUE expression index on
        # (platform_user_identifier, COALESCE(from_hash,''), to_hash) ensures
        # that, even across multiple worker processes (each with its own
        # in-process locks), the same H_old → H_new transition can only be
        # persisted once per user. COALESCE is needed because SQLite treats
        # NULL as distinct in unique indexes and `from_hash` is NULL for the
        # first-seen match. The cleanup pass above guarantees this CREATE
        # succeeds even on databases that originally suffered the race; the
        # try/except remains as a final safety net.
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uidx_matches_transition
                ON matches(platform_user_identifier, COALESCE(from_hash, ''), to_hash)
            """)
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            print(
                "[StatsStorage] could NOT create UNIQUE matches transition index "
                f"(unexpected duplicates remain after cleanup): {e}. "
                "In-process per-user lock will still prevent any NEW duplicates."
            )
        except sqlite3.OperationalError as e:
            print(f"[StatsStorage] matches transition index creation skipped: {e}")

    # ---- snapshots ----
    def get_latest_snapshot(self, name: str, platform: str = "steam") -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT * FROM snapshots
            WHERE name=? AND platform=?
            ORDER BY captured_at DESC
            LIMIT 1
        """, (name, platform))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "platform": row["platform"],
            "captured_at": row["captured_at"],
            "update_hash": row["update_hash"],
            "account_id": row["account_id"],
            "data": json.loads(row["data_json"]),
        }

    def save_snapshot(
        self,
        name: str,
        platform: str,
        update_hash: str,
        data: Dict[str, Any],
        account_id: Optional[str] = None,
        captured_at: Optional[str] = None
    ) -> Dict[str, str]:
        captured_at = captured_at or _utc_iso()
        sid = str(uuid.uuid4())

        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO snapshots(id, name, platform, captured_at, update_hash, account_id, data_json)
            VALUES(?,?,?,?,?,?,?)
        """, (
            sid, name, platform, captured_at, update_hash,
            str(account_id) if account_id is not None else None,
            json.dumps(data, ensure_ascii=False)
        ))
        self.conn.commit()
        return {"id": sid, "captured_at": captured_at}

    # ---- matches ----
    def _match_exists_for_hash(self, name: str, platform: str, to_hash: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT 1 FROM matches
            WHERE name=? AND platform=? AND to_hash=?
            LIMIT 1
        """, (name, platform, to_hash))
        return cur.fetchone() is not None

    def save_match(
        self,
        name: str,
        platform: str,
        match_obj: Dict[str, Any],
        to_hash: str,
        from_hash: Optional[str] = None,
        account_id: Optional[str] = None,
        created_at: Optional[str] = None
    ) -> str:
        created_at = created_at or _utc_iso()
        mid = str(uuid.uuid4())

        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO matches(id, name, platform, created_at, from_hash, to_hash, account_id, match_json)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            mid, name, platform, created_at,
            from_hash, to_hash,
            str(account_id) if account_id is not None else None,
            json.dumps(match_obj, ensure_ascii=False)
        ))
        self.conn.commit()
        return mid

    def count_matches(self, name: str, platform: str = "steam") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE name=? AND platform=?",
            (name, platform),
        )
        row = cur.fetchone()
        return int(row["n"]) if row else 0

    def list_matches(self, name: str, platform: str = "steam", limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT match_json FROM matches
            WHERE name=? AND platform=?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (name, platform, limit, offset))
        rows = cur.fetchall()
        return [json.loads(r["match_json"]) for r in rows]

    def build_matches_response(self, name: str, platform: str = "steam", limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        return {"data": {"matches": self.list_matches(name, platform, limit, offset)}}

    # ---- one-shot: snapshot + delta match ----
    def upsert_with_delta(
        self,
        *,
        name: str,
        platform: str,
        account_id: Optional[Union[str, int]],
        update_hash: str,
        new_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        last = self.get_latest_snapshot(name, platform)
        now_iso = _utc_iso()

        # no change
        if last and last["update_hash"] == update_hash:
            return {
                "changed": False,
                "snapshotSaved": False,
                "matchSaved": False,
                "toHash": update_hash,
                "fromHash": last["update_hash"],
                "createdAt": now_iso,
            }

        # save new snapshot
        self.save_snapshot(
            name=name,
            platform=platform,
            update_hash=update_hash,
            data=new_data,
            account_id=str(account_id) if account_id is not None else None,
            captured_at=now_iso
        )

        match_saved = False
        from_hash = last["update_hash"] if last else None

        # create delta match if we have previous snapshot
        if last and not self._match_exists_for_hash(name, platform, update_hash):
            delta_match = build_trn_like_delta_match(
                account_id=str(account_id) if account_id is not None else (last.get("account_id") or ""),
                old_data=last["data"],
                new_data=new_data,
                timestamp=now_iso
            )
            self.save_match(
                name=name,
                platform=platform,
                match_obj=delta_match,
                from_hash=from_hash,
                to_hash=update_hash,
                account_id=str(account_id) if account_id is not None else None,
                created_at=now_iso
            )
            match_saved = True

        return {
            "changed": True,
            "snapshotSaved": True,
            "matchSaved": match_saved,
            "toHash": update_hash,
            "fromHash": from_hash,
            "createdAt": now_iso,
        }

    # ============================================================
    # NEW: profile-keyed store + delta (TRN-profile shape)
    # ============================================================

    def get_profile(self, platform_user_identifier: str) -> Optional[Dict[str, Any]]:
        """Return the currently-stored TRN profile for this user, or None."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM profiles WHERE platform_user_identifier=? LIMIT 1",
            (str(platform_user_identifier),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "platformUserIdentifier": row["platform_user_identifier"],
            "platform":                row["platform"],
            "name":                    row["name"],
            "updateHash":              row["update_hash"],
            "updatedAt":               row["updated_at"],
            "trnProfile":              json.loads(row["trn_profile_json"]),
        }

    def list_profiles(self) -> List[Dict[str, Any]]:
        """Return every stored profile row — used by the background poller to
        decide who to refresh. Only includes the lightweight header columns;
        skip the full TRN JSON blob."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT platform_user_identifier, platform, name, update_hash, updated_at
            FROM profiles
            ORDER BY updated_at ASC
        """)
        return [
            {
                "platformUserIdentifier": r["platform_user_identifier"],
                "platform":                r["platform"],
                "name":                    r["name"],
                "updateHash":              r["update_hash"],
                "updatedAt":               r["updated_at"],
            }
            for r in cur.fetchall()
        ]

    def upsert_profile(
        self,
        *,
        platform_user_identifier: str,
        platform: str,
        name: str,
        update_hash: str,
        trn_profile: Dict[str, Any],
        updated_at: Optional[str] = None,
    ) -> None:
        """Insert or overwrite the profile row for this identifier."""
        updated_at = updated_at or _utc_iso()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO profiles(platform_user_identifier, platform, name, update_hash, updated_at, trn_profile_json)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(platform_user_identifier) DO UPDATE SET
                platform         = excluded.platform,
                name             = excluded.name,
                update_hash      = excluded.update_hash,
                updated_at       = excluded.updated_at,
                trn_profile_json = excluded.trn_profile_json
        """, (
            str(platform_user_identifier),
            platform,
            name,
            update_hash,
            updated_at,
            json.dumps(trn_profile, ensure_ascii=False),
        ))
        self.conn.commit()

    def save_profile_match(
        self,
        *,
        platform_user_identifier: str,
        match_obj: Dict[str, Any],
        to_hash: str,
        from_hash: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """Append a delta match for this identifier.

        Returns (match_id, inserted) where:
          * match_id  — the row's id (the proposed id if it was inserted, or
                        the pre-existing row's id if dedup kicked in)
          * inserted  — True if a new row was written, False if the unique
                        transition index detected this exact H_old → H_new
                        had already been recorded (race-loser path).

        v0.0.4: switched from INSERT OR REPLACE to INSERT OR IGNORE so a
        concurrent duplicate cannot overwrite the original match's id.
        """
        created_at = created_at or _utc_iso()
        ident = str(platform_user_identifier)
        # prefer the match's own id so list_profile_matches returns the same id
        mid = ((match_obj.get("attributes") or {}).get("id")) or str(uuid.uuid4())
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO matches(id, platform_user_identifier, created_at, from_hash, to_hash, match_json)
            VALUES(?,?,?,?,?,?)
        """, (
            str(mid),
            ident,
            created_at,
            from_hash,
            to_hash,
            json.dumps(match_obj, ensure_ascii=False),
        ))
        inserted = cur.rowcount > 0
        if not inserted:
            # The UNIQUE(platform_user_identifier, COALESCE(from_hash,''), to_hash)
            # index rejected our row — another worker already saved this exact
            # transition. Look up the original match's id so callers still get
            # something to reference. COALESCE on the lookup must mirror the
            # index's COALESCE so a NULL from_hash matches the '' index entry.
            cur.execute("""
                SELECT id FROM matches
                WHERE platform_user_identifier = ?
                  AND COALESCE(from_hash, '') = COALESCE(?, '')
                  AND to_hash = ?
                LIMIT 1
            """, (ident, from_hash, to_hash))
            row = cur.fetchone()
            if row and row["id"]:
                mid = str(row["id"])
        self.conn.commit()
        return str(mid), inserted

    def list_profile_matches(
        self,
        platform_user_identifier: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT match_json FROM matches
            WHERE platform_user_identifier=?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (str(platform_user_identifier), limit, offset))
        return [json.loads(r["match_json"]) for r in cur.fetchall()]

    def count_profile_matches(self, platform_user_identifier: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE platform_user_identifier=?",
            (str(platform_user_identifier),),
        )
        row = cur.fetchone()
        return int(row["n"]) if row else 0

    def upsert_profile_with_delta(
        self,
        *,
        trn_profile: Dict[str, Any],
        update_hash: str,
        platform: str,
        name: str,
    ) -> Dict[str, Any]:
        """One-shot: take a freshly-built TRN profile, compare its hash against
        what's stored for this platformUserIdentifier. If changed (or if this is
        the first time we've ever seen this user), overwrite the stored profile
        and append a delta match built by subtracting the old stored TRN profile
        from the new one. On first-seen, the 'old profile' is treated as empty
        so the first match contains the player's cumulative stats.
        Returns an info dict describing what happened."""
        try:
            from .converter import build_trn_match_from_profiles
        except ImportError:
            from app.converter import build_trn_match_from_profiles  # type: ignore

        pinfo = ((trn_profile or {}).get("data") or {}).get("platformInfo") or {}
        identifier = str(pinfo.get("platformUserIdentifier") or "").strip()
        if not identifier:
            raise ValueError("trn_profile is missing data.platformInfo.platformUserIdentifier")

        # v0.0.4 concurrency fix: serialize the entire read-existing → compare →
        # save-match → save-profile flow per user. Without this, two concurrent
        # /profile (or /refresh, or poller) calls for the same user both observe
        # `existing.updateHash == H_old` and `update_hash == H_new`, both build a
        # delta match with a fresh uuid, and both insert — producing duplicate
        # match rows for the same H_old → H_new transition. Holding the per-user
        # RLock around the whole flow guarantees the second caller re-reads the
        # already-committed H_new and short-circuits to the no-change branch.
        # (FastAPI sync routes run in a threadpool, so concurrent requests are
        # genuinely on different threads — an asyncio Lock would not be enough.)
        user_lock = self._get_user_lock(identifier)
        with user_lock:
            existing = self.get_profile(identifier)
            now_iso = _utc_iso()

            # no change: same hash as stored (this is the branch the race loser
            # falls into once the race winner has committed)
            if existing and existing["updateHash"] == update_hash:
                # v0.0.4 hot fix: gametools changed the platform query from
                # `pc` to `ea/steam`, and we now default to `steam`. Old rows
                # in the DB may still carry stale `platform`/`name` even
                # though the underlying nucleus id is unchanged. When the
                # hash hasn't moved we still patch name+platform so the
                # stored row self-heals to whatever just succeeded against
                # gametools. Keyed by the unique platform_user_identifier.
                if existing["platform"] != platform or existing["name"] != name:
                    cur = self.conn.cursor()
                    cur.execute(
                        "UPDATE profiles SET platform=?, name=? "
                        "WHERE platform_user_identifier=?",
                        (platform, name, identifier),
                    )
                    self.conn.commit()
                return {
                    "identifier":     identifier,
                    "changed":        False,
                    "firstSeen":      False,
                    "profileSaved":   False,
                    "matchSaved":     False,
                    "toHash":         update_hash,
                    "fromHash":       existing["updateHash"],
                    "matchId":        None,
                    "updatedAt":      existing["updatedAt"],
                }

            first_seen = existing is None
            old_profile = existing["trnProfile"] if existing else None
            from_hash = existing["updateHash"] if existing else None

            # build delta match
            match = build_trn_match_from_profiles(
                old_profile=old_profile,
                new_profile=trn_profile,
                account_id=identifier,
                timestamp=now_iso,
            )

            # v0.0.4 zero-delta guard: skip persisting a match that contains no
            # actual counter movement. This happens when the gametools update
            # hash flips for reasons unrelated to gameplay (per-class
            # secondsPlayed jitter, leaderboard recalc, etc.). We still want
            # to overwrite the stored profile so the new updateHash sticks
            # and the next genuine flip computes a correct delta — only the
            # match save is skipped. First-seen always saves so brand-new
            # users get their initial row regardless of counter values.
            zero_delta = (not first_seen) and _is_zero_delta_match(match)
            if zero_delta:
                match_id, match_inserted = None, False
            else:
                match_id, match_inserted = self.save_profile_match(
                    platform_user_identifier=identifier,
                    match_obj=match,
                    to_hash=update_hash,
                    from_hash=from_hash,
                    created_at=now_iso,
                )

            # overwrite profile (always — the hash moved even if the delta is empty)
            self.upsert_profile(
                platform_user_identifier=identifier,
                platform=platform,
                name=name,
                update_hash=update_hash,
                trn_profile=trn_profile,
                updated_at=now_iso,
            )

            return {
                "identifier":     identifier,
                "changed":        True,
                "firstSeen":      first_seen,
                "profileSaved":   True,
                # match_inserted=False means either: (a) zero-delta guard
                # filtered the match out, or (b) the UNIQUE transition index
                # caught a cross-process race. Either way the profile is safe
                # to overwrite.
                "matchSaved":     bool(match_inserted),
                "zeroDelta":      bool(zero_delta),
                "toHash":         update_hash,
                "fromHash":       from_hash,
                "matchId":        match_id,
                "updatedAt":      now_iso,
            }


# ============================================================
# 4) minimal demo
# ============================================================

if __name__ == "__main__":
    PLAYER_NAME = "BiliTV-2524OFM"   # TODO: 改成你的 ID/昵称（和 gametools 要求一致）
    PLATFORM = "steam"

    client = GametoolsClient()
    storage = StatsStorage("bf6_stats.db")

    data = client.fetch_stats(PLAYER_NAME, PLATFORM)
    # data = client.fetch_stats_mock_old(PLAYER_NAME, PLATFORM)
    # data = client.fetch_stats_mock_new(PLAYER_NAME, PLATFORM)
    if not data:
        print("fetch failed")
        raise SystemExit(1)

    # account_id：你可以放 origin/ea id；没有就用 name
    account_id = data.get("userId") or data.get("id") or PLAYER_NAME

    update_hash = client.generate_update_hash(data)
    if not update_hash:
        print("hash failed")
        raise SystemExit(1)

    res = storage.upsert_with_delta(
        name=PLAYER_NAME,
        platform=PLATFORM,
        account_id=str(account_id),
        update_hash=update_hash,
        new_data=data
    )
    print("upsert:", res)

    # print latest matches (TRN-like)
    latest = storage.build_matches_response(PLAYER_NAME, PLATFORM, limit=3, offset=0)
    print(json.dumps(latest, ensure_ascii=False, indent=2))

    storage.close()