"""
Routing service with automatic fallback.

Priority:
  1. OpenRouteService (ORS) — if ORS_API_KEY is set, fast (~1-2s)
  2. Public OSRM demo   — free, no key, slower (~5-30s) but always works

Both functions accept an optional `waypoints` list of (lat, lng) tuples for
multi-stop routing (used to re-route through optimised fuel stops).

route_opts keys (all optional):
  preference    : "recommended" | "shortest" | "fastest"  (ORS only)
  avoid_tolls   : bool  — adds "tollways" to ORS avoid_features
  avoid_highways: bool  — adds "highways" to ORS avoid_features
  avoid_terrain : bool  — adds "hills" to ORS avoid_features (flat roads)
"""

import logging
from typing import Dict, List, Optional, Tuple

import aiohttp
from django.conf import settings

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT}
        )
    return _session


async def get_route(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    waypoints: Optional[List[Tuple[float, float]]] = None,
    route_opts: Optional[Dict] = None,
) -> dict:
    api_key = getattr(settings, "ORS_API_KEY", "") or ""
    if api_key:
        try:
            return await _ors_route(
                start_lat, start_lng, end_lat, end_lng,
                api_key, waypoints, route_opts=route_opts,
            )
        except Exception as exc:
            logger.warning("ORS failed (%s), falling back to OSRM", exc)
    return await _osrm_route(start_lat, start_lng, end_lat, end_lng, waypoints)


async def _ors_route(
    start_lat: float, start_lng: float,
    end_lat: float, end_lng: float,
    api_key: str,
    waypoints: Optional[List[Tuple[float, float]]] = None,
    route_opts: Optional[Dict] = None,
) -> dict:
    """OpenRouteService — always POST so we can pass options body."""
    opts = route_opts or {}
    coordinates = (
        [[start_lng, start_lat]]
        + [[lng, lat] for lat, lng in (waypoints or [])]
        + [[end_lng, end_lat]]
    )
    body: Dict = {
        "coordinates": coordinates,
        "preference": opts.get("preference", "recommended"),
    }

    avoid = []
    if opts.get("avoid_tolls"):
        avoid.append("tollways")
    if opts.get("avoid_highways"):
        avoid.append("highways")
    if opts.get("avoid_terrain"):
        avoid.append("hills")
    if avoid:
        body["options"] = {"avoid_features": avoid}

    async with _get_session().post(
        "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status == 404:
            raise ValueError("ORS 404 for this coordinate set")
        resp.raise_for_status()
        data = await resp.json()

    if not data.get("features"):
        raise ValueError("ORS returned no route")

    feature = data["features"][0]
    summary = feature["properties"]["summary"]
    return {
        "geometry": feature["geometry"],
        "distance_miles": summary["distance"] / 1609.344,
        "duration_hours": summary["duration"] / 3600,
        "steps": _extract_ors_steps(feature["properties"]),
    }


# ORS instruction type → icon (docs: openrouteservice directions step types)
_ORS_TYPE_ICONS = {
    0: "↰", 1: "↱", 2: "⬅", 3: "➡", 4: "↖", 5: "↗",
    6: "↑", 7: "↻", 8: "↗", 9: "↩", 10: "⬛", 11: "▶", 12: "↖", 13: "↗",
}
_ORS_TYPE_NAMES = {
    0: "turn", 1: "turn", 2: "turn", 3: "turn", 4: "turn", 5: "turn",
    6: "continue", 7: "roundabout", 8: "exit roundabout", 9: "turn",
    10: "arrive", 11: "depart", 12: "fork", 13: "fork",
}


def _extract_ors_steps(properties: dict) -> List[Dict]:
    steps = []
    cum_miles = 0.0
    for seg in properties.get("segments", []):
        for step in seg.get("steps", []):
            t = step.get("type", 6)
            name = step.get("name", "") or ""
            if name == "-":
                name = ""
            dist_mi = step.get("distance", 0) / 1609.344
            mtype = _ORS_TYPE_NAMES.get(t, "continue")

            if dist_mi < 0.05 and mtype not in ("depart", "arrive"):
                continue

            steps.append({
                "icon": _ORS_TYPE_ICONS.get(t, "→"),
                "instruction": step.get("instruction", "Continue"),
                "road": name,
                "ref": name if any(ch.isdigit() for ch in name) else "",
                "dist_mi": round(dist_mi, 2),
                "dur_min": round(step.get("duration", 0) / 60, 1),
                "cum_mi": round(cum_miles, 1),
                "type": mtype,
            })
            cum_miles += dist_mi
    return steps


async def _osrm_route(
    start_lat: float, start_lng: float,
    end_lat: float, end_lng: float,
    waypoints: Optional[List[Tuple[float, float]]] = None,
) -> dict:
    """Public OSRM demo server — no key required. Supports waypoints natively."""
    all_points = (
        [(start_lat, start_lng)]
        + (waypoints or [])
        + [(end_lat, end_lng)]
    )
    coords_str = ";".join(f"{lng},{lat}" for lat, lng in all_points)
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords_str}"

    async with _get_session().get(
        url,
        params={"overview": "full", "geometries": "geojson", "steps": "true", "annotations": "false"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"OSRM error: {data.get('message', data.get('code'))}")

    route = data["routes"][0]
    steps = _extract_steps(route)
    return {
        "geometry": route["geometry"],
        "distance_miles": route["distance"] / 1609.344,
        "duration_hours": route["duration"] / 3600,
        "steps": steps,
    }


_MANEUVER_ICONS = {
    "turn":             {"left": "↰", "right": "↱", "slight left": "↖", "slight right": "↗",
                         "sharp left": "⬅", "sharp right": "➡", "uturn": "↩"},
    "new name":         {"": "→"},
    "depart":           {"": "▶"},
    "arrive":           {"": "⬛"},
    "merge":            {"left": "↰", "right": "↱", "": "↳"},
    "on ramp":          {"left": "↰", "right": "↱", "": "↗"},
    "off ramp":         {"left": "↲", "right": "↳", "": "↳"},
    "fork":             {"left": "↰", "right": "↱", "": "⑂"},
    "end of road":      {"left": "↰", "right": "↱", "": "↱"},
    "continue":         {"": "↑"},
    "roundabout":       {"": "↻"},
    "rotary":           {"": "↻"},
    "roundabout turn":  {"left": "↰", "right": "↱", "": "↑"},
    "notification":     {"": "ℹ"},
    "exit roundabout":  {"": "↗"},
    "exit rotary":      {"": "↗"},
}

_MANEUVER_VERBS = {
    "depart":       "Depart",
    "arrive":       "Arrive",
    "turn":         "Turn",
    "new name":     "Continue",
    "merge":        "Merge",
    "on ramp":      "Take ramp",
    "off ramp":     "Take exit",
    "fork":         "Keep",
    "end of road":  "Turn",
    "continue":     "Continue",
    "roundabout":   "Enter roundabout",
    "rotary":       "Enter rotary",
    "exit roundabout": "Exit roundabout",
    "exit rotary":  "Exit rotary",
}


def _extract_steps(route: dict) -> List[Dict]:
    steps = []
    cum_miles = 0.0
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            m = step.get("maneuver", {})
            mtype = m.get("type", "")
            mod   = m.get("modifier", "")
            name  = step.get("name", "") or ""
            ref   = step.get("ref", "") or ""
            dist_m   = step.get("distance", 0)
            dur_s    = step.get("duration", 0)
            dist_mi  = dist_m / 1609.344

            icon_map = _MANEUVER_ICONS.get(mtype, {})
            icon = icon_map.get(mod) or icon_map.get("") or "→"
            verb = _MANEUVER_VERBS.get(mtype, "Continue")
            road = ref if ref else name
            direction = f" {mod}" if mod and mtype not in ("depart", "arrive") else ""
            instruction = f"{verb}{direction} on {road}" if road else f"{verb}{direction}"

            if dist_mi < 0.05 and mtype not in ("depart", "arrive"):
                continue

            steps.append({
                "icon": icon,
                "instruction": instruction,
                "road": road,
                "ref": ref,
                "dist_mi": round(dist_mi, 2),
                "dur_min": round(dur_s / 60, 1),
                "cum_mi": round(cum_miles, 1),
                "type": mtype,
            })
            cum_miles += dist_mi

    return steps
