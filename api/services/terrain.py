"""
Local US terrain analysis — zero external API calls.

Uses a static bounding-box spatial index of named terrain regions.
Samples up to 80 points from the route geometry and cross-checks each
against the region table to characterise elevation, grade, and fuel impact.

Public API
----------
analyze_route_terrain(geometry, avoid_terrain) -> dict
    geometry    : GeoJSON LineString dict from OSRM
    avoid_terrain: bool — if True the user already requested flat routing
    Returns:
        regions          : list[str]   — significant region names crossed
        max_grade_pct    : int         — worst estimated road grade %
        mpg_factor       : float       — multiplier on flat-highway MPG
                                         (0.80 → mountain burns 25 % more)
        severity         : str         — "none" | "low" | "medium" | "high"
        terrain_warning  : str         — human-readable warning (blank if none)
        fuel_impact_pct  : float       — % extra fuel vs. flat route
        avoid_terrain    : bool        — echoed back for UI use
"""

from typing import Dict, List, Any

# ---------------------------------------------------------------------------
# Static US terrain region table
# Each entry covers a lat/lon bounding box with:
#   grade      : typical max road grade %
#   mpg_factor : fraction of highway MPG actually realised in this terrain
#                (accounts for climbing + net grade asymmetry for ICE vehicles)
# ---------------------------------------------------------------------------
_REGIONS: List[Dict[str, Any]] = [
    # ── FLAT / PLAINS ─────────────────────────────────────────────────────
    dict(name="Great Plains",            lat=(30, 50), lon=(-104, -96),  type="plain",       grade=1, mpg_factor=1.00),
    dict(name="Mississippi Lowlands",    lat=(29, 44), lon=(-92,  -87),  type="plain",       grade=1, mpg_factor=1.00),
    dict(name="Atlantic Coastal Plain",  lat=(30, 38), lon=(-82,  -74),  type="plain",       grade=1, mpg_factor=0.98),
    dict(name="Gulf Coastal Plain",      lat=(29, 34), lon=(-98,  -88),  type="plain",       grade=1, mpg_factor=0.99),
    dict(name="Central Valley (CA)",     lat=(35, 40), lon=(-122, -119), type="plain",       grade=1, mpg_factor=0.99),
    dict(name="Willamette Valley",       lat=(44, 46), lon=(-124, -122), type="plain",       grade=2, mpg_factor=0.99),
    dict(name="Florida Peninsula",       lat=(25, 31), lon=(-88,  -80),  type="plain",       grade=1, mpg_factor=1.00),
    dict(name="Midwest Corn Belt",       lat=(38, 48), lon=(-96,  -83),  type="plain",       grade=2, mpg_factor=0.99),

    # ── PLATEAUS ──────────────────────────────────────────────────────────
    dict(name="Texas Hill Country",      lat=(29, 32), lon=(-101, -97),  type="plateau",     grade=4, mpg_factor=0.93),
    dict(name="Ozark Plateau",           lat=(35, 38), lon=(-95,  -89),  type="plateau",     grade=5, mpg_factor=0.91),
    dict(name="Columbia Plateau",        lat=(44, 48), lon=(-122, -115), type="plateau",     grade=4, mpg_factor=0.93),
    dict(name="Colorado Plateau",        lat=(36, 40), lon=(-112, -107), type="plateau",     grade=6, mpg_factor=0.89),
    dict(name="Mojave Desert",           lat=(34, 36), lon=(-118, -113), type="plateau",     grade=4, mpg_factor=0.94),
    dict(name="Sonoran Desert",          lat=(31, 35), lon=(-115, -109), type="plateau",     grade=3, mpg_factor=0.96),
    dict(name="Chihuahuan Desert",       lat=(29, 33), lon=(-108, -103), type="plateau",     grade=4, mpg_factor=0.93),
    dict(name="Llano Estacado",          lat=(31, 35), lon=(-104, -100), type="plateau",     grade=2, mpg_factor=0.97),
    dict(name="Cumberland Plateau",      lat=(34, 38), lon=(-86,  -83),  type="plateau",     grade=5, mpg_factor=0.91),

    # ── BASIN AND RANGE ───────────────────────────────────────────────────
    dict(name="Great Basin (NV/UT)",     lat=(37, 42), lon=(-118, -113), type="basin_range", grade=7, mpg_factor=0.86),
    dict(name="Basin & Range (AZ-NM)",   lat=(31, 35), lon=(-114, -104), type="basin_range", grade=6, mpg_factor=0.89),
    dict(name="Snake River Plain",       lat=(42, 45), lon=(-116, -111), type="basin_range", grade=4, mpg_factor=0.93),

    # ── MOUNTAINS ─────────────────────────────────────────────────────────
    dict(name="Sierra Nevada",           lat=(36, 42), lon=(-122, -118), type="mountain",    grade=9, mpg_factor=0.75),
    dict(name="Cascades (WA/OR)",        lat=(43, 49), lon=(-122, -119), type="mountain",    grade=8, mpg_factor=0.79),
    dict(name="Coast Ranges (CA)",       lat=(34, 40), lon=(-124, -121), type="mountain",    grade=7, mpg_factor=0.83),
    dict(name="Olympic Mountains",       lat=(47, 48), lon=(-124, -122), type="mountain",    grade=8, mpg_factor=0.79),
    dict(name="Colorado Rockies",        lat=(37, 41), lon=(-109, -104), type="mountain",    grade=9, mpg_factor=0.73),
    dict(name="Wyoming Rockies",         lat=(41, 45), lon=(-112, -104), type="mountain",    grade=8, mpg_factor=0.79),
    dict(name="Montana Rockies",         lat=(45, 49), lon=(-116, -110), type="mountain",    grade=8, mpg_factor=0.77),
    dict(name="Idaho Mountains",         lat=(44, 47), lon=(-116, -113), type="mountain",    grade=8, mpg_factor=0.78),
    dict(name="Wasatch Range (UT)",      lat=(40, 42), lon=(-112, -111), type="mountain",    grade=9, mpg_factor=0.74),
    dict(name="Appalachians (Blue Ridge)",lat=(34,40), lon=(-83,  -78),  type="mountain",    grade=8, mpg_factor=0.81),
    dict(name="Allegheny Mountains",     lat=(37, 42), lon=(-82,  -76),  type="mountain",    grade=8, mpg_factor=0.80),
    dict(name="Great Smoky Mountains",   lat=(35, 36), lon=(-84,  -82),  type="mountain",    grade=9, mpg_factor=0.74),
    dict(name="White Mountains (NH)",    lat=(43, 45), lon=(-72,  -70),  type="mountain",    grade=8, mpg_factor=0.79),
    dict(name="Adirondacks (NY)",        lat=(43, 45), lon=(-75,  -73),  type="mountain",    grade=7, mpg_factor=0.82),
    dict(name="Green Mountains (VT)",    lat=(43, 45), lon=(-73,  -71),  type="mountain",    grade=7, mpg_factor=0.82),
    dict(name="Catskills (NY)",          lat=(41, 43), lon=(-75,  -73),  type="mountain",    grade=6, mpg_factor=0.85),
    dict(name="Black Hills (SD)",        lat=(43, 45), lon=(-104, -102), type="mountain",    grade=6, mpg_factor=0.85),
]

_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

_TYPE_SEVERITY = {
    "plain":       "none",
    "plateau":     "low",
    "basin_range": "medium",
    "mountain":    "high",
}

_SAMPLE_MAX = 80   # max geometry points to evaluate


def _sample_coords(geometry: dict) -> List[tuple]:
    """Return up to _SAMPLE_MAX (lat, lon) pairs from the route geometry."""
    coords = geometry.get("coordinates", [])
    n = len(coords)
    if n == 0:
        return []
    step = max(1, n // _SAMPLE_MAX)
    return [(coords[i][1], coords[i][0]) for i in range(0, n, step)]


def _in_region(lat: float, lon: float, r: dict) -> bool:
    lat_lo, lat_hi = r["lat"]
    lon_lo, lon_hi = r["lon"]
    return lat_lo <= lat <= lat_hi and lon_lo <= lon <= lon_hi


def analyze_route_terrain(geometry: dict, avoid_terrain: bool = False) -> dict:
    """
    Cross-check sampled route points against the static terrain table.
    Returns characterisation with no external calls.
    """
    sampled = _sample_coords(geometry)
    n = len(sampled)

    if n == 0:
        return _empty(avoid_terrain)

    # Count how many sampled points fall inside each region
    hits: Dict[str, int] = {}
    for lat, lon in sampled:
        for r in _REGIONS:
            if _in_region(lat, lon, r):
                hits[r["name"]] = hits.get(r["name"], 0) + 1

    if not hits:
        return _empty(avoid_terrain)

    # Keep only regions with at least 5 % of sampled points
    threshold = max(1, int(n * 0.05))
    significant = [r for r in _REGIONS if hits.get(r["name"], 0) >= threshold]

    if not significant:
        # Fall back to at least the most-hit region
        best_name = max(hits, key=hits.get)
        significant = [r for r in _REGIONS if r["name"] == best_name]

    # Weighted MPG factor (by hit count)
    total_hits = sum(hits.get(r["name"], 0) for r in significant)
    weighted_mpg = (
        sum(r["mpg_factor"] * hits.get(r["name"], 0) for r in significant) / total_hits
    )

    # Proportion of route NOT in any named region → treated as flat (factor = 1.0)
    matched_pts = sum(hits[k] for k in hits if any(r["name"] == k for r in significant))
    flat_frac   = max(0.0, n - matched_pts) / n
    route_factor = weighted_mpg * (1 - flat_frac) + 1.0 * flat_frac

    max_grade  = max(r["grade"] for r in significant)
    worst      = min(significant, key=lambda r: r["mpg_factor"])
    severity   = max(
        (_TYPE_SEVERITY.get(r["type"], "none") for r in significant),
        key=lambda s: _SEVERITY_RANK[s],
    )
    impact_pct = round((1.0 / route_factor - 1.0) * 100, 1)
    region_names = [r["name"] for r in sorted(significant, key=lambda r: r["mpg_factor"])[:4]]

    if avoid_terrain:
        warning = ""
    elif severity == "high":
        warning = (
            f"Route crosses {worst['name']} (est. {max_grade}% grade). "
            f"Expect ~{impact_pct}% more fuel — consider Flat Roads in Advanced Options."
        )
    elif severity == "medium":
        warning = (
            f"Route includes basin-range terrain ({worst['name']}). "
            f"~{impact_pct}% extra fuel vs. flat highway."
        )
    elif severity == "low":
        warning = f"Route crosses elevated plateau — minor fuel impact (~{impact_pct}%)."
    else:
        warning = ""

    return {
        "regions":         region_names,
        "max_grade_pct":   max_grade,
        "mpg_factor":      round(route_factor, 3),
        "severity":        severity,
        "terrain_warning": warning,
        "fuel_impact_pct": impact_pct,
        "avoid_terrain":   avoid_terrain,
    }


def _empty(avoid_terrain: bool) -> dict:
    return {
        "regions": [], "max_grade_pct": 1, "mpg_factor": 1.0,
        "severity": "none", "terrain_warning": "",
        "fuel_impact_pct": 0.0, "avoid_terrain": avoid_terrain,
    }
