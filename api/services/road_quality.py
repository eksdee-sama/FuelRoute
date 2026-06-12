"""
Road quality analysis — zero extra API calls.

Uses road references extracted from OSRM step data (already in the route response)
to classify road quality, surface type, and maintenance level.

Public API
----------
analyze_road_quality(steps) -> dict
    steps: list of step dicts from routing._extract_steps()
    Returns:
        quality_index  : float 1-10 (weighted avg quality of roads driven)
        surface_mix    : dict  {label: pct}
        road_type_mix  : dict  {type: pct_miles}
        worst_segments : list  top rough stretches
        quality_label  : str   "Excellent" | "Good" | "Fair" | "Poor"
        road_warning   : str   human-readable warning (blank if good)
        extra_fuel_pct : float estimated extra fuel from road roughness
"""

import re
from typing import Dict, List, Any

# ---------------------------------------------------------------------------
# Road classification by reference pattern
# ---------------------------------------------------------------------------
# US Interstates (I-10, I-40, I-80) — smoothest, newest pavement
# US Highways (US-66, US-1)          — good but variable
# State Routes  (TX-130, CA-1)       — maintained, some variation
# County / Local roads               — rough, stop-signs, variable quality
# ---------------------------------------------------------------------------

_INTERSTATE_RE  = re.compile(r"\b(I[- ]\d+|Interstate\s+\d+)\b", re.I)
_FREEWAY_RE     = re.compile(r"\b(Freeway|Expressway|Beltway)\b", re.I)
_US_HWY_RE      = re.compile(r"\b(US[- ]\d+|U\.S\.\s*\d+)\b", re.I)
_STATE_ROUTE_RE = re.compile(r"\b([A-Z]{2}[- ]\d+|SR[- ]?\d+|SH[- ]?\d+|State\s+Route\s+\d+|State\s+Hwy\s+\d+)\b", re.I)
_TOLL_RE        = re.compile(r"\b(Turnpike|Toll|Pike|Tollway|Thruway)\b", re.I)

# State-level road quality index (1-10) based on TRIP/ARTBA annual reports
# States that historically invest well in road maintenance score higher
_STATE_QUALITY: Dict[str, float] = {
    "ND": 8.5, "SD": 8.2, "MN": 7.8, "WY": 8.0, "MT": 7.9,
    "VA": 7.5, "NC": 7.4, "GA": 7.2, "FL": 7.6, "TX": 7.0,
    "KS": 7.8, "NE": 7.9, "IA": 7.7, "WI": 7.1, "IN": 7.3,
    "OH": 6.8, "PA": 5.8, "NY": 5.5, "NJ": 5.9, "CT": 5.6,
    "RI": 5.0, "MA": 5.2, "IL": 6.2, "MI": 5.7, "MO": 6.5,
    "LA": 5.5, "MS": 5.8, "AL": 6.0, "TN": 6.8, "KY": 6.2,
    "WV": 5.5, "MD": 6.0, "DE": 6.3, "SC": 6.5, "AR": 6.0,
    "OK": 6.3, "NM": 6.0, "AZ": 7.2, "NV": 7.0, "UT": 7.5,
    "CO": 6.9, "ID": 7.2, "OR": 6.8, "WA": 7.0, "CA": 5.8,
    "HI": 5.5, "AK": 5.0, "NH": 5.8, "VT": 6.0, "ME": 5.5,
}

_ROAD_TYPES = {
    "interstate":   {"quality": 9.2, "surface": "Smooth asphalt", "fuel_penalty": 0.0},
    "us_highway":   {"quality": 7.5, "surface": "Asphalt",        "fuel_penalty": 0.01},
    "state_route":  {"quality": 6.5, "surface": "Mixed asphalt",  "fuel_penalty": 0.02},
    "toll_road":    {"quality": 8.8, "surface": "Premium asphalt","fuel_penalty": 0.0},
    "local":        {"quality": 5.0, "surface": "Variable",       "fuel_penalty": 0.05},
    "unknown":      {"quality": 6.0, "surface": "Asphalt",        "fuel_penalty": 0.02},
}

_STATE_ABBR_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|"
    r"UT|VT|VA|WA|WV|WI|WY|DC)\b"
)


def _classify_road(ref: str, road_name: str, dist_mi: float = 0.0) -> str:
    text = f"{ref} {road_name}"
    if _TOLL_RE.search(text):
        return "toll_road"
    if _INTERSTATE_RE.search(text) or _FREEWAY_RE.search(text):
        return "interstate"
    if _US_HWY_RE.search(text):
        return "us_highway"
    if _STATE_ROUTE_RE.search(text):
        return "state_route"
    # Routers (ORS) collapse long controlled-access stretches into unnamed
    # "Keep right" steps — nobody drives 25+ uninterrupted miles on a local road.
    if dist_mi >= 25.0:
        return "interstate"
    if dist_mi >= 8.0:
        return "us_highway"
    if ref:
        return "us_highway"   # numbered but unclassified → treat as US hwy
    return "local"


def analyze_road_quality(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not steps:
        return _empty()

    total_miles = sum(s["dist_mi"] for s in steps)
    if total_miles < 0.01:
        return _empty()

    type_miles: Dict[str, float] = {}
    weighted_quality = 0.0
    weighted_penalty = 0.0
    state_hints: Dict[str, float] = {}

    for step in steps:
        d = step["dist_mi"]
        if d <= 0:
            continue
        rtype = _classify_road(step.get("ref", ""), step.get("road", ""), d)
        type_miles[rtype] = type_miles.get(rtype, 0.0) + d

        rinfo = _ROAD_TYPES[rtype]
        weighted_quality += rinfo["quality"] * d
        weighted_penalty += rinfo["fuel_penalty"] * d

        # Detect state abbreviations in road names to blend in state quality
        m = _STATE_ABBR_RE.search(step.get("ref", "") + " " + step.get("road", ""))
        if m:
            st = m.group(1)
            state_hints[st] = state_hints.get(st, 0.0) + d

    avg_quality = weighted_quality / total_miles
    avg_penalty = weighted_penalty / total_miles

    # Blend in state quality (up to 20% weight)
    if state_hints:
        dominant_state = max(state_hints, key=state_hints.get)
        sq = _STATE_QUALITY.get(dominant_state, 6.5)
        avg_quality = avg_quality * 0.8 + sq * 0.2

    avg_quality = round(max(1.0, min(10.0, avg_quality)), 1)

    # Road type mix (% of miles)
    type_mix = {k: round(v / total_miles * 100, 1) for k, v in type_miles.items()}

    # Surface mix for display
    surface_miles: Dict[str, float] = {}
    for rtype, miles in type_miles.items():
        surf = _ROAD_TYPES[rtype]["surface"]
        surface_miles[surf] = surface_miles.get(surf, 0.0) + miles
    surface_mix = {k: round(v / total_miles * 100, 1) for k, v in surface_miles.items()}

    # Worst segments (local roads are the rough ones)
    worst = []
    for step in steps:
        if _classify_road(step.get("ref", ""), step.get("road", ""), step["dist_mi"]) == "local" and step["dist_mi"] > 0.5:
            worst.append({
                "road": step["road"] or "Unnamed local road",
                "dist_mi": step["dist_mi"],
                "at_mile": step["cum_mi"],
            })
    worst = sorted(worst, key=lambda x: x["dist_mi"], reverse=True)[:3]

    # Quality label
    if avg_quality >= 8.5:
        label = "Excellent"
    elif avg_quality >= 7.0:
        label = "Good"
    elif avg_quality >= 5.5:
        label = "Fair"
    else:
        label = "Poor"

    extra_fuel = round(avg_penalty * 100, 1)

    # Road warning
    local_pct = type_mix.get("local", 0.0)
    if label in ("Poor",) or local_pct > 40:
        warning = (
            f"Route uses significant local/secondary roads ({local_pct:.0f}% of distance). "
            f"Expect rougher surfaces and ~{extra_fuel}% higher fuel use."
        )
    elif local_pct > 20:
        warning = f"Route mixes highway and local roads — minor fuel impact (~{extra_fuel}%)."
    else:
        warning = ""

    return {
        "quality_index":  avg_quality,
        "quality_label":  label,
        "surface_mix":    surface_mix,
        "road_type_mix":  type_mix,
        "worst_segments": worst,
        "road_warning":   warning,
        "extra_fuel_pct": extra_fuel,
        "interstate_pct": type_mix.get("interstate", 0.0),
        "local_pct":      local_pct,
        "total_miles":    round(total_miles, 1),
    }


def _empty() -> Dict:
    return {
        "quality_index": 7.0, "quality_label": "Good",
        "surface_mix": {}, "road_type_mix": {},
        "worst_segments": [], "road_warning": "",
        "extra_fuel_pct": 0.0, "interstate_pct": 0.0,
        "local_pct": 0.0, "total_miles": 0.0,
    }
