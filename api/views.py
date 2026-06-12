import asyncio
import hashlib
import html
import json
import logging
import re
import time
from collections import defaultdict

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.views import View

from .models import FuelStation
from .services import (fuel_optimizer, geocoding, routing, route_qa,
                        terrain as terrain_svc, road_quality as rq_svc, analytics as analytics_svc)

# ── Rate limiting (per-IP, in-memory sliding window) ──────────────────────
_RATE_STORE: dict = defaultdict(list)
_RATE_WINDOW = 60    # seconds
_RATE_MAX    = 15    # route requests per window

_MAX_LOC_LEN = 250   # max chars for a location input
# Allowed chars: letters (incl. accented), digits, spaces, and common address punct
_SAFE_LOC_RE = re.compile(r"^[\w\s,.\-\'À-ɏ/#&()]+$")


def _get_client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "0.0.0.0")


def _rate_limit_ok(ip: str, bucket: str = "route", max_hits: int = _RATE_MAX) -> bool:
    key  = f"{bucket}:{ip}"
    now  = time.monotonic()
    cutoff = now - _RATE_WINDOW
    hist = [t for t in _RATE_STORE[key] if t > cutoff]
    if len(hist) >= max_hits:
        _RATE_STORE[key] = hist
        return False
    hist.append(now)
    _RATE_STORE[key] = hist
    return True


def _validate_location(s: str) -> str:
    """Return error string if input is invalid, else empty string."""
    if len(s) > _MAX_LOC_LEN:
        return f"Location too long (max {_MAX_LOC_LEN} chars)."
    m = _COORD_RE.match(s.strip())
    if m:
        try:
            lat, lon = float(m.group(1)), float(m.group(2))
        except ValueError:
            return "Invalid coordinate format."
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return "Coordinates out of valid range (lat -90…90, lon -180…180)."
        return ""
    if not _SAFE_LOC_RE.match(s.strip()):
        return "Location contains invalid characters."
    return ""


def _sec_headers(response):
    """Attach security headers to any Django response."""
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"]        = "DENY"
    response["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response["Cache-Control"]          = "no-store"
    return response

_COORD_RE = re.compile(r"^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$")

VEHICLE_PRESETS = {
    "car":    {"label": "Car",    "icon": "&#128663;", "tank_range": 400,  "mpg": 28},
    "suv":    {"label": "SUV",    "icon": "&#128665;", "tank_range": 450,  "mpg": 22},
    "pickup": {"label": "Pickup", "icon": "&#128667;", "tank_range": 500,  "mpg": 18},
    "truck":  {"label": "Truck",  "icon": "&#128666;", "tank_range": 800,  "mpg": 18},
    "semi":   {"label": "Semi",   "icon": "&#128667;", "tank_range": 1400, "mpg": 6},
    "rv":     {"label": "RV",     "icon": "&#128656;", "tank_range": 400,  "mpg": 10},
}


def _cache_key(start: str, end: str, tank_range: float, mpg: float,
               route_opts: dict = None) -> str:
    def _norm(loc: str) -> str:
        m = _COORD_RE.match(loc.strip())
        if m:
            return f"{round(float(m.group(1)), 3)},{round(float(m.group(2)), 3)}"
        return loc.lower().strip()
    o = route_opts or {}
    # Options that change the actual road geometry must be part of the key,
    # otherwise a "No Tolls" request could be served a cached tolled route.
    opts_sig = f"{o.get('preference','recommended')}|{int(o.get('avoid_tolls',False))}|" \
               f"{int(o.get('avoid_highways',False))}|{int(o.get('avoid_terrain',False))}"
    raw = f"{_norm(start)}|{_norm(end)}|{tank_range}|{mpg}|{opts_sig}"
    return "route:" + hashlib.md5(raw.encode()).hexdigest()


async def _prefetch_stations(start_coords, end_coords):
    lat_min = min(start_coords[0], end_coords[0]) - 3.0
    lat_max = max(start_coords[0], end_coords[0]) + 3.0
    lng_min = min(start_coords[1], end_coords[1]) - 3.0
    lng_max = max(start_coords[1], end_coords[1]) + 3.0
    return await sync_to_async(list)(
        FuelStation.objects.filter(
            geocoded=True,
            latitude__gte=lat_min, latitude__lte=lat_max,
            longitude__gte=lng_min, longitude__lte=lng_max,
        ).values("id", "opis_id", "name", "address", "city", "state",
                 "retail_price", "latitude", "longitude", "geocoded")
    )


async def _resolve_display_name(user_input: str, lat: float, lng: float) -> str:
    """Return a friendly place name. Reverse-geocodes only when input is raw coordinates."""
    if user_input and not _COORD_RE.match(user_input.strip()):
        return user_input
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "fuel-route-api/1.0"}) as s:
            async with s.get(
                "https://photon.komoot.io/reverse",
                params={"lat": lat, "lon": lng},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
        p = (data.get("features") or [{}])[0].get("properties", {})
        parts = []
        for k in ("name", "city", "town", "village", "county"):
            v = p.get(k)
            if v:
                parts.append(v)
                break
        if p.get("state") and (not parts or p["state"] != parts[0]):
            parts.append(p["state"])
        return ", ".join(parts) if parts else f"{lat:.4f}, {lng:.4f}"
    except Exception:
        return f"{lat:.4f}, {lng:.4f}"


logger = logging.getLogger(__name__)


async def _compute_route(start: str, end: str, tank_range: float, mpg: float,
                          start_name: str = "", end_name: str = "",
                          route_opts: dict = None) -> dict:
    route_opts = route_opts or {}
    key = _cache_key(start, end, tank_range, mpg, route_opts)
    cached = await sync_to_async(cache.get)(key)
    if cached:
        def _dn(name, raw):
            return name if name and not _COORD_RE.match(name.strip()) else raw
        cached["route"]["start_display"] = _dn(start_name, start)
        cached["route"]["end_display"] = _dn(end_name, end)
        cached["_from_cache"] = True
        cached["_terrain"] = terrain_svc.analyze_route_terrain(
            cached["route"]["geometry"],
            avoid_terrain=route_opts.get("avoid_terrain", False),
        )
        cached["_rich_analytics"] = analytics_svc.compute(cached)
        return cached

    start_coords, end_coords = await asyncio.gather(
        geocoding.geocode_user_location(start),
        geocoding.geocode_user_location(end),
    )
    if not start_coords:
        return {"error": f"Could not geocode start location: '{start}'", "status": 400}
    if not end_coords:
        return {"error": f"Could not geocode end location: '{end}'", "status": 400}

    try:
        route_data, bbox_stations = await asyncio.gather(
            routing.get_route(start_coords[0], start_coords[1],
                              end_coords[0], end_coords[1],
                              route_opts=route_opts),
            _prefetch_stations(start_coords, end_coords),
        )
    except Exception as exc:
        logger.error("Routing failed: %s", exc)
        return {"error": f"Routing service error: {exc}", "status": 502}

    total_miles = route_data["distance_miles"]
    stations_on_route = fuel_optimizer.find_stations_on_route(
        bbox_stations, route_data["geometry"],
        proximity_miles=settings.STATION_ROUTE_PROXIMITY_MILES,
    )
    if not stations_on_route:
        return {"error": "No fuel stations found near this route.", "status": 422}

    stops = fuel_optimizer.optimize_fuel_stops(
        stations_on_route, total_distance=total_miles,
        tank_range=tank_range, mpg=mpg,
    )
    analytics = fuel_optimizer.get_route_analytics(
        stations_on_route, stops, total_miles, tank_range, mpg
    )

    # Trip fits in one tank → optimizer returns [].
    # Inject the globally cheapest station on the route as a single real stop.
    # Driver fills exactly trip_miles/mpg gallons there — this is the cost global minimum.
    is_direct = not stops
    if is_direct:
        # Use effective price (detour-adjusted) so direct-trip also prefers on-route stations
        best = min(stations_on_route, key=lambda s: s.get("_ep", s["retail_price"]))
        gallons_exact = round(total_miles / mpg, 2)
        stops = [{"station": best, "gallons": gallons_exact,
                  "cost": round(gallons_exact * best["retail_price"], 2)}]

    refuel_cost = sum(s["cost"] for s in stops)
    refuel_gallons = sum(s["gallons"] for s in stops)

    if is_direct:
        # Single stop covers the whole trip — no separate "initial fill at start"
        first_tank_gal = 0.0
        initial_fill_cost = 0.0
    else:
        first_tank_gal = min(tank_range, total_miles) / mpg
        first_seg = [s for s in stations_on_route if s["distance_from_start"] <= tank_range]
        cheapest_start_price = (
            min(s["retail_price"] for s in first_seg) if first_seg
            else (min(s["retail_price"] for s in stations_on_route) if stations_on_route else 0.0)
        )
        initial_fill_cost = round(first_tank_gal * cheapest_start_price, 2)

    total_cost = round(initial_fill_cost + refuel_cost, 2)
    total_gallons = round(first_tank_gal + refuel_gallons, 2)
    stop_ids = {s["station"]["id"] for s in stops}

    def _dn(name, raw):
        return name if name and not _COORD_RE.match(name.strip()) else raw
    sd = _dn(start_name, start)
    ed = _dn(end_name, end)

    terrain_analysis = terrain_svc.analyze_route_terrain(
        route_data["geometry"],
        avoid_terrain=route_opts.get("avoid_terrain", False),
    )
    road_quality = rq_svc.analyze_road_quality(route_data.get("steps", []))

    result = {
        "route": {
            "start": start, "end": end,
            "start_display": sd, "end_display": ed,
            "start_coordinates": {"lat": start_coords[0], "lng": start_coords[1]},
            "end_coordinates": {"lat": end_coords[0], "lng": end_coords[1]},
            "total_distance_miles": round(total_miles, 1),
            "total_distance_km": round(total_miles * 1.60934, 1),
            "estimated_duration_hours": round(route_data["duration_hours"], 1),
            "geometry": route_data["geometry"],
        },
        "fuel_stops": [
            {
                "stop_number": idx + 1,
                "station_name": s["station"]["name"],
                "address": s["station"]["address"],
                "city": s["station"]["city"],
                "state": s["station"]["state"],
                "price_per_gallon": round(s["station"]["retail_price"], 3),
                "gallons_to_add": s["gallons"],
                "cost_usd": s["cost"],
                "latitude": s["station"]["latitude"],
                "longitude": s["station"]["longitude"],
                "distance_from_start_miles": round(s["station"]["distance_from_start"], 1),
                "distance_from_route_miles": s["station"]["distance_from_route"],
            }
            for idx, s in enumerate(stops)
        ],
        "all_route_stations": [
            {
                "id": s["id"], "name": s["name"], "city": s["city"], "state": s["state"],
                "price_per_gallon": round(s["retail_price"], 3),
                "latitude": s["latitude"], "longitude": s["longitude"],
                "distance_from_start_miles": round(s["distance_from_start"], 1),
                "is_optimal_stop": s["id"] in stop_ids,
            }
            for s in sorted(stations_on_route, key=lambda x: x["distance_from_start"])
        ],
        "summary": {
            "total_fuel_stops": len(stops),
            "initial_fill_cost_usd": initial_fill_cost,
            "refuel_stops_cost_usd": round(refuel_cost, 2),
            "total_fuel_cost_usd": total_cost,
            "total_gallons_purchased": total_gallons,
            "average_price_per_gallon": round(total_cost / total_gallons, 3) if total_gallons else 0,
            "vehicle_specs": {"tank_range_miles": tank_range, "fuel_efficiency_mpg": mpg},
            "total_stations_near_route": len(stations_on_route),
        },
        "_is_direct": is_direct,
        "_route_opts": route_opts,
        "_analytics": analytics,
        "_terrain": terrain_analysis,
        "_road_quality": road_quality,
        "_steps": route_data.get("steps", []),
    }
    result["_rich_analytics"] = analytics_svc.compute(result)
    await sync_to_async(cache.set)(key, result, settings.ROUTE_CACHE_TIMEOUT)
    return result


def _parse_params(request):
    start = request.GET.get("start", "").strip()
    end   = request.GET.get("end",   "").strip()
    start_name = request.GET.get("start_name", "").strip()[:300]
    end_name   = request.GET.get("end_name",   "").strip()[:300]

    # ── Input validation ──────────────────────────────────────────────────
    if start:
        err = _validate_location(start)
        if err:
            return start, end, 500, 10, start_name, end_name, {}, f"Start location: {err}"
    if end:
        err = _validate_location(end)
        if err:
            return start, end, 500, 10, start_name, end_name, {}, f"End location: {err}"

    preset_key = request.GET.get("preset", "")[:32]  # cap length
    preset = VEHICLE_PRESETS.get(preset_key)

    try:
        default_range = preset["tank_range"] if preset else settings.VEHICLE_TANK_RANGE_MILES
        default_mpg   = preset["mpg"]        if preset else settings.VEHICLE_MPG
        tank_range = float(request.GET.get("tank_range", default_range))
        mpg        = float(request.GET.get("mpg",        default_mpg))
    except ValueError:
        return start, end, 500, 10, start_name, end_name, {}, "tank_range and mpg must be numeric."
    if not (50 <= tank_range <= 2000):
        return start, end, tank_range, mpg, start_name, end_name, {}, "tank_range must be 50–2000 miles."
    if not (1 <= mpg <= 200):
        return start, end, tank_range, mpg, start_name, end_name, {}, "mpg must be 1–200."

    preference = request.GET.get("preference", "recommended")
    if preference not in ("recommended", "fastest", "shortest"):
        preference = "recommended"

    route_opts = {
        "preference":     preference,
        "avoid_tolls":    request.GET.get("avoid_tolls",    "0") == "1",
        "avoid_highways": request.GET.get("avoid_highways", "0") == "1",
        "avoid_terrain":  request.GET.get("avoid_terrain",  "0") == "1",
        "preset":         preset_key,
    }
    return start, end, tank_range, mpg, start_name, end_name, route_opts, None


class RouteView(View):
    async def get(self, request):
        ip = _get_client_ip(request)
        if not _rate_limit_ok(ip):
            return _sec_headers(JsonResponse(
                {"error": "Too many requests. Please wait a minute and try again."},
                status=429,
            ))
        start, end, tank_range, mpg, start_name, end_name, route_opts, err = _parse_params(request)
        if err:
            return _sec_headers(JsonResponse({"error": err}, status=400))
        if not start or not end:
            return _sec_headers(JsonResponse({"error": "Both 'start' and 'end' are required."}, status=400))
        t0 = time.perf_counter()
        result = await _compute_route(start, end, tank_range, mpg, start_name, end_name, route_opts)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if "error" in result:
            return _sec_headers(JsonResponse({"error": result["error"]}, status=result.get("status", 400)))
        resp = JsonResponse(result)
        resp["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
        resp["X-Cache"] = "HIT" if result.get("_from_cache") else "MISS"
        return _sec_headers(resp)


class RouteMapView(View):
    async def get(self, request):
        ip = _get_client_ip(request)
        if not _rate_limit_ok(ip):
            return _sec_headers(HttpResponse(
                "<p style='font-family:sans-serif;padding:20px'>Rate limit exceeded. Please wait a moment.</p>",
                content_type="text/html", status=429,
            ))
        start, end, tank_range, mpg, start_name, end_name, route_opts, err = _parse_params(request)
        if err:
            return _sec_headers(HttpResponse(f"<p>{html.escape(err)}</p>", content_type="text/html", status=400))
        if not start or not end:
            return _sec_headers(HttpResponse("<p>Both start and end are required.</p>", content_type="text/html", status=400))
        t0 = time.perf_counter()
        result = await _compute_route(start, end, tank_range, mpg, start_name, end_name, route_opts)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if "error" in result:
            return _sec_headers(HttpResponse(
                f"<h2 style='font-family:sans-serif;padding:20px;color:#ef4444'>Error: {html.escape(result['error'])}</h2>",
                content_type="text/html", status=result.get("status", 400),
            ))
        resp = HttpResponse(_render_map_html(result), content_type="text/html")
        resp["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
        resp["X-Cache"] = "HIT" if result.get("_from_cache") else "MISS"
        return _sec_headers(resp)


def _render_map_html(result: dict) -> str:
    route    = result["route"]
    stops    = result["fuel_stops"]
    summary  = result["summary"]
    all_stations = result["all_route_stations"]
    analytics = result.get("_analytics", {})
    sc = route["start_coordinates"]
    ec = route["end_coordinates"]
    start_display = html.escape(route.get("start_display") or route["start"])
    end_display   = html.escape(route.get("end_display")   or route["end"])
    start_coord_tip = f"{sc['lat']:.5f}°, {sc['lng']:.5f}°"
    end_coord_tip   = f"{ec['lat']:.5f}°, {ec['lng']:.5f}°"
    start_coords = [sc["lat"], sc["lng"]]
    end_coords   = [ec["lat"], ec["lng"]]

    is_direct = result.get("_is_direct", False)

    stop_cards = ""
    for s in stops:
        detour = s["distance_from_route_miles"]
        if detour > 1.0:
            detour_chip = f'<span class="chip" style="background:rgba(239,68,68,.12);color:#F87171;border:1px solid rgba(239,68,68,.2)">&#9888; {detour} mi detour</span>'
        elif detour > 0.3:
            detour_chip = f'<span class="chip" style="background:rgba(234,179,8,.12);color:#FCD34D;border:1px solid rgba(234,179,8,.2)">{detour} mi off route</span>'
        else:
            detour_chip = '<span class="chip" style="background:rgba(16,185,129,.1);color:#6EE7B7;border:1px solid rgba(16,185,129,.15)">on route</span>'

        if is_direct:
            badge = '<div class="stop-num" style="background:linear-gradient(135deg,#D97706,#F59E0B);box-shadow:0 2px 8px rgba(245,158,11,.35)">&#9733;</div>'
            card_extra = ' style="border-color:rgba(245,158,11,.3)"'
            gal_chip = f'<span class="chip c-blue">{s["gallons_to_add"]} gal</span>'
            subloc = f'<div class="stop-loc">{html.escape(s["city"])}, {html.escape(s["state"])} &bull; mile {s["distance_from_start_miles"]}</div><div class="stop-loc" style="color:#F59E0B;font-size:10px">&#9733; Cheapest on route &mdash; fill here</div>'
        else:
            badge = f'<div class="stop-num">{s["stop_number"]}</div>'
            card_extra = ''
            gal_chip = f'<span class="chip c-blue">+{s["gallons_to_add"]} gal</span>'
            subloc = f'<div class="stop-loc">{html.escape(s["city"])}, {html.escape(s["state"])} &bull; mile {s["distance_from_start_miles"]}</div>'
        stop_cards += f"""<div class="stop-card" onclick="zoomTo({s['stop_number']-1})"{card_extra}>
          <div class="stop-row">
            {badge}
            <div class="stop-body">
              <div class="stop-name">{html.escape(s['station_name'])}</div>
              {subloc}
              <div class="stop-chips">
                <span class="chip c-green">${s['price_per_gallon']}/gal</span>
                {gal_chip}
                <span class="chip c-purple">${s['cost_usd']}</span>
                {detour_chip}
              </div>
            </div>
          </div>
        </div>"""
    if is_direct:
        stop_cards += '<div style="text-align:center;padding:8px 8px 4px;color:var(--mut);font-size:11px;line-height:1.7">Direct trip &mdash; fill once, no other stops needed.</div>'

    # QA badge
    qa_findings = route_qa.run_all_checks(result)
    qa_agg = route_qa.qa_summary(qa_findings)
    if qa_agg["overall"] == "PASS":
        qa_badge = '<div style="display:flex;align-items:center;gap:5px;margin-top:8px;padding:5px 8px;background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.18);border-radius:7px;font-size:11px;font-weight:600;color:#6EE7B7">&#10003; Route Quality: All checks passed</div>'
    elif qa_agg["overall"] == "WARN":
        issues = [f["message"] for f in qa_findings if f["level"] == "WARN"]
        tip = html.escape("; ".join(issues))
        qa_badge = f'<div title="{tip}" style="display:flex;align-items:center;gap:5px;margin-top:8px;padding:5px 8px;background:rgba(234,179,8,.08);border:1px solid rgba(234,179,8,.2);border-radius:7px;font-size:11px;font-weight:600;color:#FCD34D;cursor:help">&#9888; Route Quality: {qa_agg["warn_count"]} warning(s)</div>'
    else:
        issues = [f["message"] for f in qa_findings if f["level"] == "FAIL"]
        tip = html.escape("; ".join(issues))
        qa_badge = f'<div title="{tip}" style="display:flex;align-items:center;gap:5px;margin-top:8px;padding:5px 8px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:7px;font-size:11px;font-weight:600;color:#F87171;cursor:help">&#10008; Route Quality: {qa_agg["fail_count"]} issue(s)</div>'

    # Terrain badge
    terrain = result.get("_terrain", {})
    t_sev  = terrain.get("severity", "none")
    t_warn = terrain.get("terrain_warning", "")
    t_imp  = terrain.get("fuel_impact_pct", 0.0)
    t_regions = ", ".join(terrain.get("regions", [])[:2])
    t_avoid = terrain.get("avoid_terrain", False)
    if t_avoid:
        terrain_badge = '<div style="display:flex;align-items:center;gap:5px;margin-top:6px;padding:5px 8px;background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.18);border-radius:7px;font-size:11px;font-weight:600;color:#6EE7B7">&#9899; Terrain: Flat roads preferred</div>'
    elif t_sev == "high":
        tip = html.escape(t_warn)
        terrain_badge = f'<div title="{tip}" style="display:flex;align-items:center;gap:5px;margin-top:6px;padding:5px 8px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:7px;font-size:11px;font-weight:600;color:#F87171;cursor:help">&#9968; Mountain terrain &mdash; +{t_imp}% fuel &bull; {html.escape(t_regions)}</div>'
    elif t_sev == "medium":
        tip = html.escape(t_warn)
        terrain_badge = f'<div title="{tip}" style="display:flex;align-items:center;gap:5px;margin-top:6px;padding:5px 8px;background:rgba(234,179,8,.08);border:1px solid rgba(234,179,8,.2);border-radius:7px;font-size:11px;font-weight:600;color:#FCD34D;cursor:help">&#9956; Rolling terrain &mdash; +{t_imp}% fuel &bull; {html.escape(t_regions)}</div>'
    elif t_sev == "low":
        terrain_badge = f'<div style="display:flex;align-items:center;gap:5px;margin-top:6px;padding:5px 8px;background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.18);border-radius:7px;font-size:11px;font-weight:600;color:#93C5FD">&#9651; Plateau terrain &mdash; +{t_imp}% fuel</div>'
    else:
        terrain_badge = '<div style="display:flex;align-items:center;gap:5px;margin-top:6px;padding:5px 8px;background:rgba(16,185,129,.05);border:1px solid rgba(16,185,129,.12);border-radius:7px;font-size:11px;font-weight:600;color:#6EE7B7">&#9654; Flat terrain &mdash; optimal fuel efficiency</div>'

    # Route options chips shown in sidebar
    opts = result.get("_route_opts", {})
    preset_key = opts.get("preset", "")
    preset_info = VEHICLE_PRESETS.get(preset_key)
    opt_chips = ""
    if preset_info:
        opt_chips += f'<span class="chip c-blue">{preset_info["icon"]} {preset_info["label"]}</span>'
    pref = opts.get("preference", "recommended")
    if pref != "recommended":
        opt_chips += f'<span class="chip c-blue">&#128337; {pref.title()}</span>'
    if opts.get("avoid_tolls"):
        opt_chips += '<span class="chip" style="background:rgba(239,68,68,.1);color:#F87171;border:1px solid rgba(239,68,68,.2)">No Tolls</span>'
    if opts.get("avoid_highways"):
        opt_chips += '<span class="chip" style="background:rgba(234,179,8,.1);color:#FCD34D;border:1px solid rgba(234,179,8,.2)">No Highways</span>'
    if opts.get("avoid_terrain"):
        opt_chips += '<span class="chip" style="background:rgba(16,185,129,.1);color:#6EE7B7;border:1px solid rgba(16,185,129,.2)">Flat Roads</span>'
    opt_chips_html = f'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px">{opt_chips}</div>' if opt_chips else ""

    # ── Directions panel ──────────────────────────────────────────────────
    steps = result.get("_steps", [])
    dir_cards = ""
    for i, step in enumerate(steps):
        is_dep = step["type"] == "depart"
        is_arr = step["type"] == "arrive"
        bg = "rgba(16,185,129,.08)" if is_dep else ("rgba(239,68,68,.08)" if is_arr else "var(--card)")
        bc = "rgba(16,185,129,.2)" if is_dep else ("rgba(239,68,68,.2)" if is_arr else "var(--brd)")
        road_esc = html.escape(step["road"]) if step["road"] else "Unnamed road"
        instr_esc = html.escape(step["instruction"])
        dist_txt = f'{step["dist_mi"]} mi' if step["dist_mi"] >= 0.1 else f'{round(step["dist_mi"]*5280)} ft'
        dir_cards += f"""<div style="background:{bg};border:1px solid {bc};border-radius:10px;padding:9px 11px;margin-bottom:5px;display:flex;align-items:flex-start;gap:9px">
  <div style="min-width:28px;height:28px;border-radius:8px;background:var(--sur);border:1px solid var(--brd);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0">{step['icon']}</div>
  <div style="flex:1;min-width:0">
    <div style="font-size:12px;font-weight:700;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{instr_esc}</div>
    <div style="font-size:10px;color:var(--mut);margin-top:2px">{road_esc}</div>
  </div>
  <div style="font-size:10px;font-weight:700;color:var(--mut);white-space:nowrap;text-align:right;min-width:42px">{dist_txt}<br><span style="font-size:9px;font-weight:400">mi {step['cum_mi']}</span></div>
</div>"""

    if not dir_cards:
        dir_cards = '<div style="text-align:center;padding:20px;color:var(--mut);font-size:12px">Directions available after first route calculation.</div>'

    # ── Analytics panel ───────────────────────────────────────────────────
    ra  = result.get("_rich_analytics", {})
    rq  = result.get("_road_quality", {})
    rqi = rq.get("quality_index", 7.0)
    rql = rq.get("quality_label", "Good")
    rq_color = ("#34D399" if rqi >= 8 else ("#FCD34D" if rqi >= 6 else "#F87171"))
    rq_interstate = rq.get("interstate_pct", 0)
    rq_local = rq.get("local_pct", 0)

    grad_color = "#FCD34D" if "Rising" in ra.get("price_gradient","") else ("#34D399" if "Falling" in ra.get("price_gradient","") else "#93C5FD")
    vol_color  = "#F87171" if ra.get("price_volatility") == "High" else ("#FCD34D" if ra.get("price_volatility") == "Medium" else "#34D399")
    veh_mpg = summary.get("vehicle_specs", {}).get("fuel_efficiency_mpg", 10)
    eff_mpg = ra.get("effective_mpg", veh_mpg)
    eff_color = "#34D399" if eff_mpg >= veh_mpg * 0.9 else ("#FCD34D" if eff_mpg >= veh_mpg * 0.75 else "#F87171")

    zone_rows = ""
    for zname, zprice in ra.get("zone_avg_prices", {}).items():
        is_cheap = zname == ra.get("cheapest_zone")
        star = " &#9733;" if is_cheap else ""
        zc = "#34D399" if is_cheap else "var(--mut)"
        zone_rows += f'<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid var(--brd);font-size:11px"><span style="color:var(--mut)">{zname}{star}</span><span style="color:{zc};font-weight:700">${zprice}/gal</span></div>'

    dead_html = ""
    for dz in ra.get("dead_zones", []):
        dead_html += f'<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:7px;padding:6px 9px;margin-bottom:4px;font-size:11px;color:#F87171">&#9888; No stations mi {dz["from_mile"]}–{dz["to_mile"]} ({dz["gap_miles"]} mi gap)</div>'

    road_type_html = ""
    type_labels = {"interstate": "Interstate", "us_highway": "US Highway", "state_route": "State Route", "toll_road": "Toll Road", "local": "Local Road", "unknown": "Road"}
    for rt, pct in sorted(rq.get("road_type_mix", {}).items(), key=lambda x: -x[1]):
        bar_w = max(4, int(pct))
        rt_color = ("#34D399" if rt == "interstate" else ("#93C5FD" if rt in ("us_highway","toll_road") else ("#FCD34D" if rt == "state_route" else "#F87171")))
        road_type_html += f'<div style="margin-bottom:5px"><div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px"><span style="color:var(--mut)">{type_labels.get(rt, rt)}</span><span style="color:{rt_color};font-weight:700">{pct}%</span></div><div style="height:5px;background:var(--sur);border-radius:3px"><div style="width:{bar_w}%;height:100%;background:{rt_color};border-radius:3px"></div></div></div>'

    analytics_panel = f"""<div style="padding:8px 7px 12px">
  <div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;padding:4px 6px 8px">Fuel Economics</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:10px">
    <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px"><div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px">Saved vs Worst</div><div style="font-size:16px;font-weight:800;color:#34D399;margin-top:2px">${ra.get('savings_vs_worst',0)}</div><div style="font-size:10px;color:var(--mut)">vs always-max price</div></div>
    <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px"><div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px">Saved vs Avg</div><div style="font-size:16px;font-weight:800;color:#34D399;margin-top:2px">${ra.get('savings_vs_avg',0)}</div><div style="font-size:10px;color:var(--mut)">${ra.get('savings_per_gallon',0)}/gal below avg</div></div>
    <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px"><div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px">Price Spread</div><div style="font-size:16px;font-weight:800;color:var(--txt);margin-top:2px">${ra.get('price_min',0)}–${ra.get('price_max',0)}</div><div style="font-size:10px;color:{vol_color}">{ra.get('price_volatility','')} volatility &#963;{ra.get('price_std',0)}</div></div>
    <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px"><div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.6px">Trip Time</div><div style="font-size:16px;font-weight:800;color:#93C5FD;margin-top:2px">{ra.get('total_trip_time_hr',0)}h</div><div style="font-size:10px;color:var(--mut)">{ra.get('stop_time_min',0)} min at pumps</div></div>
  </div>

  <div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;padding:4px 6px 6px">Price by Route Zone</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px;margin-bottom:10px">
    {zone_rows}
    <div style="font-size:10px;color:{grad_color};font-weight:600;margin-top:6px">&#9650; {html.escape(ra.get('price_gradient',''))}</div>
  </div>

  <div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;padding:4px 6px 6px">Fuel Efficiency</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px;margin-bottom:10px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:12px;color:var(--mut)">Rated MPG</span><span style="font-size:14px;font-weight:800;color:var(--txt)">{ra.get('nominal_mpg',0)} mpg</span></div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:12px;color:var(--mut)">Effective MPG</span><span style="font-size:14px;font-weight:800;color:{eff_color}">{eff_mpg} mpg</span></div>
    <div style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:12px;color:var(--mut)">Terrain+road range loss</span><span style="font-size:13px;font-weight:700;color:#FCD34D">-{ra.get('range_loss_mi',0)} mi</span></div>
  </div>

  <div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;padding:4px 6px 6px">Road Quality — {rql} ({rqi}/10)</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px;margin-bottom:10px">
    <div style="height:8px;background:var(--sur);border-radius:4px;margin-bottom:8px;overflow:hidden"><div style="width:{int(rqi*10)}%;height:100%;background:linear-gradient(90deg,{rq_color},{rq_color}aa);border-radius:4px;transition:width .5s"></div></div>
    {road_type_html}
    {'<div style="font-size:10px;color:#F87171;margin-top:4px">'+html.escape(rq.get("road_warning",""))+'</div>' if rq.get("road_warning") else ''}
  </div>

  <div style="font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;padding:4px 6px 6px">Environmental Impact</div>
  <div style="background:var(--card);border:1px solid var(--brd);border-radius:9px;padding:8px 10px;margin-bottom:10px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px"><span style="font-size:12px;color:var(--mut)">CO&#8322; emitted</span><span style="font-size:13px;font-weight:700;color:#F87171">{ra.get('co2_lbs',0)} lbs / {ra.get('co2_kg',0)} kg</span></div>
    <div style="font-size:10px;color:var(--mut)">&#127807; Equivalent to {ra.get('trees_days',0)} tree-days of absorption</div>
  </div>

  {dead_html}
</div>"""

    tog_label = "&#x2714; Direct Trip" if is_direct else f"&#x2714; Optimal ({summary['total_fuel_stops']} stops)"
    is_direct_json = json.dumps(is_direct)

    # Downsample geometry for DISPLAY only (server-side math used full fidelity).
    # Full OSRM overview can be 10k+ points × 13 decimals → multi-MB HTML → browser lag.
    _coords = route["geometry"].get("coordinates", [])
    _step = max(1, len(_coords) // 1200)
    _disp = [[round(c[0], 5), round(c[1], 5)] for c in _coords[::_step]]
    if _coords:
        _last = [round(_coords[-1][0], 5), round(_coords[-1][1], 5)]
        if _disp[-1] != _last:
            _disp.append(_last)
    geometry_json = json.dumps({"type": "LineString", "coordinates": _disp})

    # Round station coords for the embedded payload (5 dp ≈ 1 m)
    _slim_stops = [{**s, "latitude": round(s["latitude"], 5), "longitude": round(s["longitude"], 5)} for s in stops]
    _slim_all = [{**s, "latitude": round(s["latitude"], 5), "longitude": round(s["longitude"], 5)} for s in all_stations]
    stops_json = json.dumps(_slim_stops)
    all_stations_json = json.dumps(_slim_all)
    start_coords_json = json.dumps(start_coords)
    end_coords_json = json.dumps(end_coords)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FuelRoute &mdash; {start_display} &rarr; {end_display}</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    :root{{--bg:#070C18;--sur:#0C1524;--card:#0F1C2E;--brd:rgba(255,255,255,.07);--txt:#EEF2FF;--mut:#4E6180;--grn:#10B981;--blu:#3B82F6}}
    body{{font-family:'Inter',-apple-system,sans-serif;display:flex;height:100vh;background:var(--bg);overflow:hidden;color:var(--txt)}}
    #sidebar{{width:350px;min-width:290px;display:flex;flex-direction:column;background:var(--bg);border-right:1px solid var(--brd);overflow:hidden}}
    #map-wrap{{flex:1;position:relative}}
    #map{{width:100%;height:100%}}
    .hdr{{padding:14px 14px 12px;background:var(--card);border-bottom:1px solid var(--brd);flex-shrink:0}}
    .hdr-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
    .brand{{display:flex;align-items:center;gap:7px}}
    .brand-ico{{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,#1D4ED8,#0EA5E9);display:flex;align-items:center;justify-content:center}}
    .brand-txt{{font-size:13px;font-weight:800;color:var(--txt)}}
    .brand-txt em{{font-style:normal;color:var(--blu)}}
    .back-link{{font-size:11px;color:var(--blu);text-decoration:none;font-weight:600;opacity:.8}}
    .back-link:hover{{opacity:1}}
    .route-loc{{margin-bottom:10px}}
    .rl-row{{display:flex;align-items:center;gap:9px;padding:4px 0}}
    .rl-dot{{width:9px;height:9px;border-radius:50%;flex-shrink:0}}
    .rl-dot-s{{background:#10B981;box-shadow:0 0 7px rgba(16,185,129,.55)}}
    .rl-dot-e{{background:#EF4444;box-shadow:0 0 7px rgba(239,68,68,.45)}}
    .rl-sep{{width:1px;height:13px;background:var(--brd);margin-left:4px}}
    .rl-name{{font-size:13px;font-weight:700;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:270px;cursor:default;position:relative}}
    .rl-name:hover{{color:#93C5FD}}
    .rl-tip{{font-size:10px;color:var(--mut);margin-left:3px;font-weight:400;font-family:monospace}}
    .stats{{display:grid;grid-template-columns:1fr 1fr;gap:5px}}
    .stat{{background:var(--sur);border-radius:9px;padding:8px 10px;border:1px solid var(--brd)}}
    .stat-l{{font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.7px}}
    .stat-v{{font-size:17px;font-weight:800;letter-spacing:-.4px;margin-top:2px}}
    .stat-s{{font-size:10px;color:var(--mut);margin-top:1px}}
    .cg{{color:#34D399}}.cb{{color:#60A5FA}}.cw{{color:var(--txt)}}.cp{{color:#C4B5FD}}
    .tog-bar{{padding:7px 9px;display:flex;gap:5px;background:var(--card);border-bottom:1px solid var(--brd);flex-shrink:0}}
    .tog{{flex:1;padding:7px 5px;border-radius:8px;border:1px solid var(--brd);background:var(--sur);color:var(--mut);font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s}}
    .tog.on{{background:rgba(59,130,246,.14);color:#93C5FD;border-color:rgba(59,130,246,.35)}}
    .sec-hdr{{padding:9px 13px 5px;font-size:9px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.8px;display:flex;justify-content:space-between;flex-shrink:0}}
    .sec-sub{{opacity:.5;font-weight:400;text-transform:none;letter-spacing:0;font-size:10px}}
    .list{{flex:1;overflow-y:auto;padding:3px 7px 12px}}
    .stop-card{{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:11px 12px;margin-bottom:7px;cursor:pointer;transition:border-color .15s,background .15s,transform .1s;position:relative;overflow:hidden}}
    .stop-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,#10B981,#3B82F6);border-radius:3px 0 0 3px}}
    .stop-card:hover{{background:#121E30;border-color:rgba(59,130,246,.3);transform:translateX(2px)}}
    .stop-row{{display:flex;align-items:flex-start;gap:9px}}
    .stop-num{{display:flex;align-items:center;justify-content:center;min-width:28px;height:28px;background:linear-gradient(135deg,#059669,#10B981);border-radius:8px;font-weight:800;font-size:13px;color:#fff;flex-shrink:0;box-shadow:0 2px 8px rgba(16,185,129,.3)}}
    .stop-body{{flex:1;min-width:0}}
    .stop-name{{font-size:13px;font-weight:700;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .stop-loc{{font-size:11px;color:var(--mut);margin:3px 0 6px}}
    .stop-chips{{display:flex;gap:4px;flex-wrap:wrap}}
    .chip{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px}}
    .c-green{{background:rgba(16,185,129,.12);color:#34D399;border:1px solid rgba(16,185,129,.2)}}
    .c-blue{{background:#1E3A5F;color:#93C5FD}}
    .c-purple{{background:#2D1B69;color:#C4B5FD}}
    .map-ctrl{{position:absolute;top:12px;right:12px;z-index:1000;padding:8px 15px;background:rgba(7,12,24,.88);color:var(--txt);border:1px solid var(--brd);border-radius:9px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit;box-shadow:0 4px 20px rgba(0,0,0,.5);backdrop-filter:blur(8px);transition:all .15s}}
    .map-ctrl.on{{background:rgba(59,130,246,.2);border-color:rgba(59,130,246,.5);color:#93C5FD}}
    .leaflet-popup-content-wrapper{{background:#0F1C2E;border:1px solid rgba(255,255,255,.1);border-radius:11px;box-shadow:0 8px 32px rgba(0,0,0,.6);color:#EEF2FF}}
    .leaflet-popup-tip{{background:#0F1C2E}}
    .leaflet-popup-content{{margin:12px 14px;font-family:'Inter',sans-serif}}
    ::-webkit-scrollbar{{width:4px}}
    ::-webkit-scrollbar-track{{background:transparent}}
    ::-webkit-scrollbar-thumb{{background:#1A2C3F;border-radius:2px}}
    @media (max-width:820px){{
      body{{flex-direction:column}}
      #sidebar{{width:100%;min-width:0;max-height:48vh;border-right:none;border-bottom:1px solid var(--brd)}}
      #map-wrap{{flex:1;min-height:52vh}}
      .stats{{grid-template-columns:repeat(4,1fr)}}
      .stat-v{{font-size:14px}}
      .rl-name{{max-width:75vw}}
    }}
    @media (max-width:520px){{
      .stats{{grid-template-columns:1fr 1fr}}
      .tog{{font-size:10px;padding:6px 3px}}
    }}
  </style>
</head>
<body>
  <div id="sidebar">
    <div class="hdr">
      <div class="hdr-top">
        <div class="brand">
          <div class="brand-ico"><svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M20 7.5L18 5.5L19.5 4L22 6.5L20.5 8L20 7.5ZM20 7.5V16C20 17.1 19.1 18 18 18C16.9 18 16 17.1 16 16V13H14V5C14 3.9 13.1 3 12 3H5C3.9 3 3 3.9 3 5V21H14V15H16V16C16 18.21 17.79 20 20 20C22.21 20 24 18.21 24 16V6L20 7.5Z" fill="white"/></svg></div>
          <div class="brand-txt">Fuel<em>Route</em></div>
        </div>
        <a class="back-link" href="/">&#8592; New Route</a>
      </div>
      <div class="route-loc">
        <div class="rl-row">
          <span class="rl-dot rl-dot-s"></span>
          <span class="rl-name" title="{start_coord_tip}">{start_display}</span>
        </div>
        <div class="rl-sep" style="margin-left:4px"></div>
        <div class="rl-row">
          <span class="rl-dot rl-dot-e"></span>
          <span class="rl-name" title="{end_coord_tip}">{end_display}</span>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-l">Distance</div><div class="stat-v cb">{route['total_distance_miles']}<span style="font-size:11px;font-weight:500"> mi</span></div><div class="stat-s">{route['total_distance_km']} km</div></div>
        <div class="stat"><div class="stat-l">Est. Drive</div><div class="stat-v cb">{route['estimated_duration_hours']}<span style="font-size:11px;font-weight:500"> hrs</span></div><div class="stat-s">highway avg</div></div>
        <div class="stat"><div class="stat-l">Total Fuel Cost</div><div class="stat-v cg">$<span>{summary['total_fuel_cost_usd']}</span></div><div class="stat-s">{summary['total_gallons_purchased']} gal total</div></div>
        <div class="stat"><div class="stat-l">Avg Price</div><div class="stat-v cw">$<span>{summary['average_price_per_gallon']}</span></div><div class="stat-s">per gallon</div></div>
      </div>
      {qa_badge}
      {terrain_badge}
      {opt_chips_html}
    </div>
    <div class="tog-bar" style="flex-wrap:wrap;gap:4px">
      <button class="tog on" id="btn-s" onclick="switchTab('stops')">{tog_label}</button>
      <button class="tog" id="btn-a" onclick="switchTab('all')">&#x25CE; {summary['total_stations_near_route']} Stations</button>
      <button class="tog" id="btn-d" onclick="switchTab('dir')">&#x27A1; Directions</button>
      <button class="tog" id="btn-x" onclick="switchTab('analytics')">&#x1F4CA; Analytics</button>
    </div>
    <div id="panel-stops" class="list">{stop_cards}</div>
    <div id="panel-all" class="list" style="display:none"><div style="text-align:center;padding:16px;color:var(--mut);font-size:11px">&#x25CE; Toggle "Show All Stations" on the map to view all {summary['total_stations_near_route']} nearby stations with price markers.</div></div>
    <div id="panel-dir" class="list" style="display:none">{dir_cards}</div>
    <div id="panel-analytics" class="list" style="display:none">{analytics_panel}</div>
  </div>
  <div id="map-wrap">
    <button class="map-ctrl" id="map-tog" onclick="toggleAll()">&#x25CE; Show All Stations</button>
    <div id="map"></div>
  </div>
  <script>
    var map=L.map('map',{{zoomControl:false}});
    L.control.zoom({{position:'bottomright'}}).addTo(map);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',{{
      attribution:'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, DeLorme, NAVTEQ, TomTom, USGS, NPS',
      maxZoom:19
    }}).addTo(map);
    var geo={geometry_json},stopsD={stops_json},allD={all_stations_json};
    var sc={start_coords_json},ec={end_coords_json},isDirect={is_direct_json};
    var aCnt={summary['total_stations_near_route']};
    var terrainSev={json.dumps(terrain.get('severity','none'))};
    var terrainRegions={json.dumps(terrain.get('regions',[]))};

    // Route glow + terrain-colored line
    L.geoJSON(geo,{{style:{{color:'rgba(37,99,235,.28)',weight:14,opacity:1}}}}).addTo(map);
    var routeColor = terrainSev==='high'?'#EF4444':(terrainSev==='medium'?'#F59E0B':'#2563EB');
    L.geoJSON(geo,{{style:{{color:routeColor,weight:4,opacity:1}}}}).addTo(map);

    // Price heatmap: color all-stations by price (green=cheap, red=expensive)
    var allPrices=allD.map(function(s){{return s.price_per_gallon;}}).filter(Boolean);
    var minP=Math.min.apply(null,allPrices)||0,maxP=Math.max.apply(null,allPrices)||1;
    function priceColor(p){{
      var t=(p-minP)/(maxP-minP||1);
      var r=Math.round(t*220),g=Math.round((1-t)*180);
      return 'rgb('+r+','+g+',60)';
    }}
    function stopIco(n){{return L.divIcon({{className:'',html:'<div style="background:linear-gradient(135deg,#059669,#10B981);color:#fff;border-radius:8px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;border:2px solid rgba(255,255,255,.5);box-shadow:0 3px 12px rgba(5,150,105,.45)">'+n+'</div>',iconSize:[30,30],iconAnchor:[15,15],popupAnchor:[0,-17]}});}}
    function pumpSvg(color){{
      return '<svg width="18" height="24" viewBox="0 0 18 24" xmlns="http://www.w3.org/2000/svg">'
        +'<rect x="2" y="6" width="11" height="16" rx="2" fill="'+color+'" stroke="rgba(255,255,255,.4)" stroke-width="1"/>'
        +'<rect x="4" y="9" width="7" height="5" rx="1" fill="rgba(255,255,255,.25)"/>'
        +'<rect x="5" y="3" width="2" height="5" fill="'+color+'"/>'
        +'<path d="M7 3 Q13 3 13 8 L15 8 L15 12 L13 12" stroke="'+color+'" stroke-width="1.5" fill="none" stroke-linecap="round"/>'
        +'<rect x="14" y="10" width="3" height="4" rx="1" fill="'+color+'"/>'
        +'</svg>';
    }}
    function priceIco(p){{
      var c=priceColor(p);
      var h='<div style="display:flex;flex-direction:column;align-items:center;gap:1px">'
        +pumpSvg(c)
        +'<div style="background:#fff;color:#111;border:1px solid rgba(0,0,0,.25);border-radius:5px;padding:1px 6px;font-size:11px;font-weight:700;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,.25)">$'+p+'</div>'
        +'</div>';
      return L.divIcon({{className:'',html:h,iconSize:[28,46],iconAnchor:[14,46],popupAnchor:[0,-48]}});
    }}
    function endIco(t,c){{return L.divIcon({{className:'',html:'<div style="background:'+c+';color:#fff;border-radius:7px;padding:4px 10px;font-weight:700;font-size:12px;white-space:nowrap;border:1.5px solid rgba(255,255,255,.3);box-shadow:0 3px 12px rgba(0,0,0,.5)">'+t+'</div>',iconSize:[null,null],iconAnchor:[26,14],popupAnchor:[0,-16]}});}}
    var goldIco=L.divIcon({{className:'',html:'<div style="background:linear-gradient(135deg,#D97706,#F59E0B);color:#fff;border-radius:9px;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:17px;border:2px solid rgba(255,255,255,.5);box-shadow:0 3px 12px rgba(245,158,11,.45)">&#9733;</div>',iconSize:[32,32],iconAnchor:[16,16],popupAnchor:[0,-19]}});
    var sMarkers=[];
    stopsD.forEach(function(s,i){{
      var galTxt=isDirect?s.gallons_to_add+' gal':'+'+s.gallons_to_add+' gal';
      var extra=isDirect?'<div style="font-size:11px;color:#F59E0B;font-weight:600;margin-bottom:6px">&#9733; Cheapest on route &mdash; fill here</div>':'';
      var det=s.distance_from_route_miles;
      var detColor=det>1.0?'#F87171':(det>0.3?'#FCD34D':'#6EE7B7');
      var detTxt=det>0.3?(det+' mi off route'):'on route';
      var p='<div style="min-width:195px"><div style="font-size:14px;font-weight:700;margin-bottom:4px">'+s.station_name+'</div>'
        +'<div style="color:#4E6180;font-size:11px;margin-bottom:7px">'+s.address+', '+s.city+', '+s.state+'</div>'
        +extra
        +'<div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:7px">'
        +'<span style="background:rgba(16,185,129,.14);color:#34D399;padding:3px 8px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid rgba(16,185,129,.25)">$'+s.price_per_gallon+'/gal</span>'
        +'<span style="background:#1E3A5F;color:#93C5FD;padding:3px 8px;border-radius:99px;font-size:12px;font-weight:700">'+galTxt+'</span>'
        +'<span style="background:#2D1B69;color:#C4B5FD;padding:3px 8px;border-radius:99px;font-size:12px;font-weight:700">$'+s.cost_usd+'</span>'
        +'</div>'
        +'<div style="display:flex;justify-content:space-between;font-size:10px;color:#4E6180">'
        +'<span>Mile '+s.distance_from_start_miles+' from start</span>'
        +'<span style="color:'+detColor+'">'+detTxt+'</span>'
        +'</div></div>';
      var ico=(isDirect&&i===0)?goldIco:stopIco(i+1);
      sMarkers.push(L.marker([s.latitude,s.longitude],{{icon:ico}}).bindPopup(p).addTo(map));
    }});
    var aMarkers=[],aVis=false,aLoading=false;
    allD.forEach(function(s){{
      if(s.is_optimal_stop)return;
      var p='<div><div style="font-size:13px;font-weight:700;margin-bottom:3px">'+s.name+'</div>'
        +'<div style="color:#4E6180;font-size:11px;margin-bottom:6px">'+s.city+', '+s.state+'</div>'
        +'<div style="font-size:18px;font-weight:800;color:#34D399">$'+s.price_per_gallon+'<span style="font-size:11px;color:#4E6180;font-weight:400">/gal</span></div>'
        +'<div style="font-size:10px;color:#4E6180;margin-top:3px">Mile '+s.distance_from_start_miles+'</div></div>';
      aMarkers.push(L.marker([s.latitude,s.longitude],{{icon:priceIco(s.price_per_gallon)}}).bindPopup(p));
    }});
    L.marker(sc,{{icon:endIco('START','#1D4ED8'),zIndexOffset:2000}}).addTo(map);
    L.marker(ec,{{icon:endIco('END','#DC2626'),zIndexOffset:2000}}).addTo(map);
    var ll=geo.coordinates.map(function(c){{return[c[1],c[0]];}});
    map.fitBounds(L.polyline(ll).getBounds(),{{padding:[40,40]}});
    function zoomTo(i){{map.setView([stopsD[i].latitude,stopsD[i].longitude],14,{{animate:true}});sMarkers[i].openPopup();}}
    var curTab='stops';
    function switchTab(tab){{
      if(aLoading)return;
      curTab=tab;
      ['stops','all','dir','analytics'].forEach(function(t){{
        document.getElementById('panel-'+t).style.display=(t===tab?'':'none');
        document.getElementById('btn-'+(t==='stops'?'s':t==='all'?'a':t==='dir'?'d':'x')).classList.toggle('on',t===tab);
      }});
      if(tab==='all'&&!aVis) _loadAllMarkers();
      if(tab!=='all'&&aVis) _hideAllMarkers();
    }}
    function _loadAllMarkers(){{
      if(aLoading||aVis)return;
      aLoading=true;
      var b=document.getElementById('map-tog');
      b.textContent='Loading 0…';b.classList.remove('on');
      var i=0,chunk=60;
      function addNext(){{
        var end=Math.min(i+chunk,aMarkers.length);
        for(;i<end;i++)aMarkers[i].addTo(map);
        if(i<aMarkers.length){{requestAnimationFrame(addNext);b.textContent='Loading '+Math.round(i/aMarkers.length*100)+'%…';}}
        else{{aLoading=false;aVis=true;b.textContent='Hide All Stations';b.classList.add('on');}}
      }}
      requestAnimationFrame(addNext);
    }}
    function _hideAllMarkers(){{
      aMarkers.forEach(function(m){{map.removeLayer(m);}});
      aVis=false;
      var b=document.getElementById('map-tog');
      b.textContent='◎ Show All Stations';b.classList.remove('on');
    }}
    function toggleAll(){{
      if(aVis)_hideAllMarkers(); else _loadAllMarkers();
      switchTab(aVis?'stops':'all');
    }}
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Place search proxy
# ---------------------------------------------------------------------------

_photon_session = None

def _get_photon_session():
    global _photon_session
    if _photon_session is None or _photon_session.closed:
        _photon_session = aiohttp.ClientSession(headers={"User-Agent": "fuel-route-api/1.0"})
    return _photon_session


class PlaceSearchView(View):
    async def get(self, request):
        ip = _get_client_ip(request)
        # Autocomplete fires per keystroke — generous but bounded (90/min)
        if not _rate_limit_ok(ip, bucket="search", max_hits=90):
            return _sec_headers(JsonResponse([], safe=False, status=429))
        q = request.GET.get("q", "").strip()[:120]
        if len(q) < 2:
            return _sec_headers(JsonResponse([], safe=False))
        try:
            async with _get_photon_session().get(
                "https://photon.komoot.io/api/",
                params={"q": q, "limit": 8, "lang": "en", "bbox": "-130,24,-65,50"},
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                data = await resp.json()
        except Exception:
            return _sec_headers(JsonResponse([], safe=False))
        results = []
        for f in data.get("features", []):
            p = f.get("properties", {})
            lng, lat = f["geometry"]["coordinates"]
            parts = [p.get("name", "")]
            if p.get("street") and p.get("name") != p.get("street"):
                parts.append(p["street"])
            if p.get("city") and p.get("city") != p.get("name"):
                parts.append(p["city"])
            if p.get("state"):
                parts.append(p["state"])
            label = ", ".join(x for x in parts if x)
            if not label:
                continue
            results.append({
                "label": label,
                "type": p.get("osm_value") or p.get("type") or "place",
                "lat": round(lat, 6), "lng": round(lng, 6),
                "coords": f"{round(lat,6)},{round(lng,6)}",
            })
        return _sec_headers(JsonResponse(results, safe=False))


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------

class HomeView(View):
    def get(self, request):
        resp = HttpResponse(_render_home_html(), content_type="text/html")
        resp["X-Content-Type-Options"] = "nosniff"
        resp["X-Frame-Options"]        = "DENY"
        resp["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        return resp


def _render_home_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FuelRoute &mdash; Cheapest US Road Trip Fuel Planner</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,400&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --bg:#04080F;--bg2:#070D1A;--sur:#0A1322;--card:#0D1829;
      --brd:rgba(255,255,255,.06);--brd2:rgba(255,255,255,.11);
      --txt:#EEF2FF;--txt2:#7A95B8;--mut:#3D5570;
      --blu:#3B82F6;--blu2:#60A5FA;--blu3:#93C5FD;
      --grn:#10B981;--grn2:#34D399;--grn3:#6EE7B7;
      --pur:#8B5CF6;--pur2:#A78BFA;
      --ora:#F59E0B;--red:#EF4444;
      --grd:linear-gradient(135deg,#1D4ED8 0%,#0EA5E9 100%);
    }
    html{scroll-behavior:smooth}
    body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;overflow-x:hidden;position:relative}

    /* ─── ANIMATED MESH BACKGROUND ─── */
    .bg-mesh{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
    .orb{position:absolute;border-radius:50%;filter:blur(100px);will-change:transform}
    .orb1{width:700px;height:700px;background:radial-gradient(circle,rgba(37,99,235,.14) 0%,transparent 65%);top:-200px;left:-150px;animation:drift1 28s ease-in-out infinite}
    .orb2{width:600px;height:600px;background:radial-gradient(circle,rgba(16,185,129,.10) 0%,transparent 65%);bottom:-150px;right:-100px;animation:drift2 34s ease-in-out infinite}
    .orb3{width:500px;height:500px;background:radial-gradient(circle,rgba(139,92,246,.08) 0%,transparent 65%);top:35%;left:45%;animation:drift1 22s 5s ease-in-out infinite}
    @keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(40px,-40px) scale(1.06)}66%{transform:translate(-25px,30px) scale(.94)}}
    @keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}40%{transform:translate(-35px,25px) scale(1.04)}75%{transform:translate(30px,-20px) scale(.97)}}
    .grid-dots{position:fixed;inset:0;z-index:0;background-image:radial-gradient(rgba(255,255,255,.028) 1px,transparent 1px);background-size:32px 32px;pointer-events:none}

    /* ─── LAYOUT ─── */
    .page{position:relative;z-index:1;min-height:100vh;display:flex;flex-direction:column}

    /* ─── NAV ─── */
    nav{
      display:flex;align-items:center;justify-content:space-between;
      padding:16px 32px;
      border-bottom:1px solid var(--brd);
      background:rgba(4,8,15,.75);
      backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
      position:sticky;top:0;z-index:200;
    }
    .nav-brand{display:flex;align-items:center;gap:10px;text-decoration:none}
    .nav-ico{
      width:34px;height:34px;border-radius:9px;
      background:var(--grd);
      display:flex;align-items:center;justify-content:center;
      box-shadow:0 0 20px rgba(14,165,233,.28);
      flex-shrink:0;
    }
    .nav-logo{font-size:16px;font-weight:800;letter-spacing:-.4px;color:var(--txt)}
    .nav-logo em{font-style:normal;color:var(--blu2)}
    .nav-badge{
      padding:4px 10px;border-radius:6px;
      background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);
      font-size:11px;font-weight:700;color:var(--grn2);
      display:flex;align-items:center;gap:5px;
    }
    .live-dot{width:6px;height:6px;border-radius:50%;background:var(--grn);box-shadow:0 0 7px var(--grn);animation:blink 2s ease-in-out infinite}
    @keyframes blink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}

    /* ─── HERO ─── */
    .hero{
      flex:1;display:flex;flex-direction:column;align-items:center;
      padding:64px 20px 32px;text-align:center;
    }
    .hero-eyebrow{
      display:inline-flex;align-items:center;gap:7px;
      padding:6px 16px;border-radius:99px;
      border:1px solid rgba(59,130,246,.25);
      background:rgba(59,130,246,.07);
      font-size:12px;font-weight:600;color:var(--blu3);
      margin-bottom:28px;
      animation:fadeUp .5s ease both;
    }
    .hero-eyebrow-dot{width:5px;height:5px;border-radius:50%;background:var(--grn2);box-shadow:0 0 8px var(--grn);animation:blink 2s ease-in-out infinite}
    h1.hero-h{
      font-size:clamp(38px,6.5vw,72px);
      font-weight:900;letter-spacing:-3px;line-height:1.03;
      background:linear-gradient(155deg,#EEF2FF 20%,#93C5FD 55%,#6EE7B7 90%);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
      margin-bottom:20px;max-width:820px;
      animation:fadeUp .6s .08s ease both;
    }
    .hero-sub{
      font-size:16px;color:var(--txt2);line-height:1.7;
      max-width:500px;margin-bottom:48px;
      animation:fadeUp .6s .16s ease both;
    }
    .hero-sub b{color:var(--grn2);font-weight:600}
    @keyframes fadeUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}

    /* ─── MAIN CARD ─── */
    .card{
      width:100%;max-width:570px;
      background:rgba(13,24,41,.88);
      border:1px solid var(--brd2);
      border-radius:26px;
      padding:28px 28px 24px;
      box-shadow:0 50px 120px rgba(0,0,0,.65),0 0 0 1px rgba(255,255,255,.035);
      backdrop-filter:blur(28px);-webkit-backdrop-filter:blur(28px);
      animation:fadeUp .7s .24s ease both;
    }

    /* ─── LOCATION INPUTS ─── */
    .loc-group{
      background:var(--sur);border:1px solid var(--brd);
      border-radius:18px;margin-bottom:14px;overflow:visible;
      transition:border-color .2s,box-shadow .2s;
    }
    .loc-group:focus-within{border-color:rgba(59,130,246,.35);box-shadow:0 0 0 3px rgba(59,130,246,.08)}
    .loc-row{
      display:flex;align-items:center;gap:12px;
      padding:13px 14px;position:relative;
    }
    .loc-row+.loc-row{border-top:1px solid var(--brd)}
    .loc-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .loc-dot-s{background:var(--grn);box-shadow:0 0 9px rgba(16,185,129,.6)}
    .loc-dot-e{background:var(--red);box-shadow:0 0 9px rgba(239,68,68,.5)}
    .loc-body{flex:1;min-width:0}
    .loc-lbl{font-size:9.5px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:3px}
    .loc-inp{
      width:100%;background:none;border:none;outline:none;
      color:var(--txt);font-size:14px;font-weight:500;font-family:inherit;
    }
    .loc-inp::placeholder{color:#1C2E44;font-weight:400}
    .loc-actions{display:flex;gap:4px;flex-shrink:0}
    .ico-btn{
      width:26px;height:26px;border-radius:7px;
      border:1px solid var(--brd);background:var(--card);
      color:var(--mut);display:flex;align-items:center;justify-content:center;
      cursor:pointer;transition:all .18s;flex-shrink:0;
    }
    .ico-btn:hover{border-color:var(--blu);color:var(--blu2);background:rgba(59,130,246,.08)}
    .loc-ok{
      display:none;width:7px;height:7px;border-radius:50%;
      background:var(--grn);box-shadow:0 0 8px var(--grn);flex-shrink:0;
    }
    .swap-row{display:flex;justify-content:flex-end;padding:0 14px;margin:-7px 0;z-index:10;position:relative}
    .swap-btn{
      width:26px;height:26px;border-radius:7px;
      border:1px solid var(--brd);background:var(--sur);
      color:var(--mut);display:flex;align-items:center;justify-content:center;
      cursor:pointer;transition:all .22s;
    }
    .swap-btn:hover{border-color:var(--blu);color:var(--blu2);transform:rotate(180deg)}

    /* ─── DROPDOWN ─── */
    .drop{
      position:absolute;top:calc(100% + 9px);left:-1px;right:-1px;
      background:#060F1E;
      border:1px solid rgba(255,255,255,.09);
      border-radius:16px;
      box-shadow:0 28px 70px rgba(0,0,0,.75),0 0 0 1px rgba(255,255,255,.025);
      z-index:500;display:none;overflow:hidden;max-height:280px;overflow-y:auto;
    }
    .drop.show{display:block;animation:dropIn .14s ease}
    @keyframes dropIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
    .di{padding:11px 15px;cursor:pointer;display:flex;align-items:center;gap:12px;border-bottom:1px solid rgba(255,255,255,.035);transition:background .1s}
    .di:last-child{border-bottom:none}
    .di:hover,.di.active{background:rgba(59,130,246,.09)}
    .di-ico{font-size:17px;width:26px;text-align:center;flex-shrink:0}
    .di-main{flex:1;min-width:0}
    .di-lbl{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .di-crd{font-size:10px;color:var(--blu3);font-family:'SF Mono',monospace;margin-top:2px}

    /* ─── SECTION LABEL ─── */
    .sec-lbl{font-size:10px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:9px}

    /* ─── VEHICLE PRESETS ─── */
    .preset-wrap{margin-bottom:14px}
    .preset-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:7px}
    .pcard{
      display:flex;flex-direction:column;align-items:center;gap:5px;
      padding:11px 6px 9px;
      border-radius:13px;border:1px solid var(--brd);background:var(--sur);
      cursor:pointer;transition:all .2s;position:relative;overflow:hidden;
    }
    .pcard::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(59,130,246,.1),transparent);opacity:0;transition:opacity .2s}
    .pcard:hover{border-color:rgba(59,130,246,.3);transform:translateY(-1px)}
    .pcard:hover::before{opacity:1}
    .pcard.on{border-color:rgba(59,130,246,.55);background:rgba(59,130,246,.1);box-shadow:0 0 18px rgba(59,130,246,.12)}
    .pcard.on::before{opacity:1}
    .pcard-ico{font-size:22px;line-height:1;filter:drop-shadow(0 2px 4px rgba(0,0,0,.4))}
    .pcard-name{font-size:9.5px;font-weight:700;color:var(--txt2);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
    .pcard.on .pcard-name{color:var(--blu2)}
    .pcard-mpg{font-size:9px;color:var(--mut);font-weight:500}
    .pcard.on .pcard-mpg{color:var(--blu3)}

    /* ─── SPECS ─── */
    .specs-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:14px}
    .spec-box{
      background:var(--sur);border:1px solid var(--brd);border-radius:13px;
      padding:12px 14px;transition:border-color .2s;
    }
    .spec-box:focus-within{border-color:rgba(59,130,246,.35)}
    .spec-lbl{font-size:9.5px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:5px}
    .spec-row{display:flex;align-items:baseline;gap:5px}
    .spec-inp{background:none;border:none;outline:none;color:var(--txt);font-size:20px;font-weight:800;font-family:inherit;width:80px}
    .spec-unit{font-size:12px;color:var(--mut);font-weight:500}

    /* ─── ROUTE OPTIONS ─── */
    .opts-wrap{margin-bottom:16px}
    .opts-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
    .opt-tog{
      display:flex;align-items:center;gap:6px;
      padding:7px 12px;border-radius:10px;
      border:1px solid var(--brd);background:var(--sur);
      color:var(--txt2);font-size:12px;font-weight:600;
      cursor:pointer;transition:all .18s;font-family:inherit;
      user-select:none;
    }
    .opt-tog:hover{border-color:rgba(255,255,255,.12)}
    .opt-tog.on{border-color:rgba(59,130,246,.4);background:rgba(59,130,246,.09);color:var(--blu2)}
    .opt-tog svg{flex-shrink:0}
    .pref-row{display:flex;align-items:center;gap:8px}
    .pref-lbl{font-size:10px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:1px}
    .pref-sel{
      flex:1;background:var(--sur);border:1px solid var(--brd);
      color:var(--txt);font-size:12px;font-weight:600;
      padding:7px 12px;border-radius:10px;font-family:inherit;
      cursor:pointer;outline:none;transition:border-color .2s;
      appearance:none;
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%233D5570' stroke-width='1.8' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
      background-repeat:no-repeat;background-position:right 10px center;padding-right:28px;
    }
    .pref-sel:focus{border-color:rgba(59,130,246,.4)}

    /* ─── SUBMIT BUTTON ─── */
    .go-btn{
      width:100%;padding:16px;
      background:var(--grd);color:#fff;
      border:none;border-radius:14px;
      font-size:15px;font-weight:800;font-family:inherit;
      cursor:pointer;letter-spacing:.3px;
      position:relative;overflow:hidden;
      transition:transform .2s,box-shadow .2s,opacity .2s;
      box-shadow:0 8px 30px rgba(14,165,233,.22);
    }
    .go-btn::after{
      content:'';position:absolute;inset:0;
      background:linear-gradient(135deg,rgba(255,255,255,.14) 0%,transparent 60%);
      pointer-events:none;
    }
    .go-btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 18px 50px rgba(14,165,233,.38)}
    .go-btn:active:not(:disabled){transform:translateY(0);box-shadow:0 6px 20px rgba(14,165,233,.2)}
    .go-btn:disabled{opacity:.2;cursor:not-allowed;transform:none}
    .go-btn-inner{position:relative;z-index:1;display:flex;align-items:center;justify-content:center;gap:8px}
    .go-btn-ico{transition:transform .2s}
    .go-btn:hover:not(:disabled) .go-btn-ico{transform:translateX(4px)}

    /* ─── HINT ─── */
    .hint{font-size:11px;color:#1E3050;margin-top:10px;text-align:center;line-height:1.6}
    .hint code{background:var(--sur);color:var(--blu3);padding:1px 5px;border-radius:4px;font-size:10px}

    /* ─── STATS STRIP ─── */
    .stats-strip{
      display:flex;justify-content:center;align-items:center;
      gap:0;padding:36px 20px 48px;
      animation:fadeUp .8s .4s ease both;
    }
    .stat{text-align:center;padding:0 28px;position:relative}
    .stat+.stat::before{content:'';position:absolute;left:0;top:15%;bottom:15%;width:1px;background:var(--brd)}
    .stat-val{
      font-size:32px;font-weight:900;letter-spacing:-1.5px;
      background:linear-gradient(135deg,var(--blu2),var(--grn2));
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
      line-height:1;
    }
    .stat-lbl{font-size:10.5px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:.9px;margin-top:5px}

    /* ─── FEATURES ROW ─── */
    .feat-row{
      display:flex;justify-content:center;gap:8px;flex-wrap:wrap;
      padding:0 20px 20px;
      animation:fadeUp .8s .5s ease both;
    }
    .feat-chip{
      display:flex;align-items:center;gap:7px;
      padding:7px 14px;
      background:rgba(255,255,255,.025);
      border:1px solid rgba(255,255,255,.055);
      border-radius:99px;
      font-size:11.5px;font-weight:600;color:var(--mut);
      transition:all .2s;cursor:default;
    }
    .feat-chip:hover{border-color:rgba(255,255,255,.1);color:var(--txt2)}
    .feat-chip-ico{font-size:14px}

    /* ─── SCROLLBAR ─── */
    ::-webkit-scrollbar{width:4px}
    ::-webkit-scrollbar-track{background:transparent}
    ::-webkit-scrollbar-thumb{background:#152030;border-radius:2px}

    /* ─── RESPONSIVE ─── */
    @media(max-width:520px){
      .preset-grid{grid-template-columns:repeat(3,1fr)}
      nav{padding:14px 18px}
      .card{padding:20px 18px 18px;border-radius:20px}
      h1.hero-h{letter-spacing:-2px}
      .stats-strip{gap:0;flex-wrap:wrap}
      .stat{padding:16px 18px}
    }
    @media(max-width:360px){
      .preset-grid{grid-template-columns:repeat(2,1fr)}
    }

    /* ─── ADVANCED BUTTON ─── */
    .adv-btn{display:flex;align-items:center;gap:7px;padding:8px 16px;border-radius:11px;border:1px solid var(--brd);background:var(--sur);color:var(--txt2);font-size:12px;font-weight:600;cursor:pointer;transition:all .18s;font-family:inherit;letter-spacing:.1px}
    .adv-btn:hover{border-color:rgba(255,255,255,.14);color:var(--txt)}
    .adv-btn.active{border-color:rgba(59,130,246,.45);background:rgba(59,130,246,.08);color:var(--blu2)}
    .adv-count{background:var(--blu);color:#fff;border-radius:99px;font-size:9px;font-weight:800;padding:1px 7px;margin-left:1px}

    /* ─── ADVANCED PANEL ─── */
    .adv-overlay{position:fixed;inset:0;z-index:800;display:none;align-items:flex-end;justify-content:center}
    .adv-overlay.show{display:flex;animation:fadeIn .15s ease}
    .adv-backdrop{position:absolute;inset:0;background:rgba(4,8,15,.55);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
    .adv-panel{position:relative;z-index:1;width:100%;max-width:570px;background:rgba(6,12,28,.98);border:1px solid rgba(255,255,255,.09);border-bottom:none;border-radius:22px 22px 0 0;padding:18px 22px 36px;box-shadow:0 -24px 80px rgba(0,0,0,.75),inset 0 1px 0 rgba(255,255,255,.05);animation:slideUp .22s ease}
    @keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
    .adv-handle{width:36px;height:3px;border-radius:2px;background:rgba(255,255,255,.12);margin:0 auto 18px}
    .adv-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
    .adv-title{font-size:14px;font-weight:700;color:var(--txt)}
    .adv-close-btn{width:28px;height:28px;border-radius:8px;border:1px solid var(--brd);background:var(--sur);color:var(--mut);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px;transition:all .15s;font-family:inherit;line-height:1}
    .adv-close-btn:hover{border-color:var(--red);color:var(--red)}
    .pref-opt{padding:7px 14px;border-radius:10px;border:1px solid var(--brd);background:var(--sur);color:var(--txt2);font-size:12px;font-weight:600;cursor:pointer;transition:all .18s;font-family:inherit}
    .pref-opt.on{border-color:rgba(59,130,246,.45);background:rgba(59,130,246,.09);color:var(--blu2)}
    .pref-opt:hover:not(.on){border-color:rgba(255,255,255,.12);color:var(--txt)}

    /* ─── LOADING OVERLAY ─── */
    .load-overlay{position:fixed;inset:0;z-index:9999;background:rgba(4,8,15,.9);backdrop-filter:blur(28px);-webkit-backdrop-filter:blur(28px);display:none;align-items:center;justify-content:center}
    .load-overlay.show{display:flex;animation:fadeIn .35s ease}
    .load-glass{background:linear-gradient(135deg,rgba(12,22,42,.97),rgba(7,14,28,.99));border:1px solid rgba(255,255,255,.08);border-radius:28px;padding:36px 48px 30px;display:flex;flex-direction:column;align-items:center;gap:14px;box-shadow:0 50px 120px rgba(0,0,0,.85),0 0 0 1px rgba(59,130,246,.05),inset 0 1px 0 rgba(255,255,255,.04);max-width:340px;width:90vw}
    .truck-scene{width:200px;height:100px}
    .truck-svg{filter:drop-shadow(0 6px 28px rgba(59,130,246,.35))}
    /* fuel liquid fill */
    .fl{animation:fuelFill 2.8s ease-in-out infinite}
    @keyframes fuelFill{0%{height:0;y:82}55%{height:42;y:40}80%{height:42;y:40}100%{height:0;y:82}}
    .fl-wave{animation:waveSlide 2.8s ease-in-out infinite;opacity:.6}
    @keyframes waveSlide{0%{transform:translateX(0)}50%{transform:translateX(8px)}100%{transform:translateX(0)}}
    /* cyborg eye */
    .eye{animation:eyePulse 1.3s ease-in-out infinite}
    @keyframes eyePulse{0%,100%{opacity:1}50%{opacity:.2}}
    /* smoke */
    .smk{animation:smokeRise 2s ease-out infinite}
    .smk.s1{animation-delay:0s}.smk.s2{animation-delay:.35s}.smk.s3{animation-delay:.7s}
    @keyframes smokeRise{0%{opacity:.5;transform:translateY(0) scale(1)}100%{opacity:0;transform:translateY(-18px) scale(1.8)}}
    /* fuel drop */
    .drp{animation:dropFall 2.8s ease-in-out infinite}
    @keyframes dropFall{0%,38%{cy:64;opacity:1}60%,100%{cy:82;opacity:0}}
    /* nozzle arm */
    .arm{animation:nozzleWobble 2.8s ease-in-out infinite;transform-origin:178px 8px}
    @keyframes nozzleWobble{0%,100%{transform:rotate(0deg)}35%{transform:rotate(-4deg)}70%{transform:rotate(2.5deg)}}
    /* speed lines */
    .spd{animation:speedLine .65s linear infinite}
    .spd2{animation:speedLine .8s .12s linear infinite}
    @keyframes speedLine{0%{opacity:.45;transform:translateX(4px)}100%{opacity:0;transform:translateX(-22px)}}
    /* wheel spin */
    .whl{animation:spin .7s linear infinite;transform-box:fill-box;transform-origin:center}
    @keyframes spin{to{transform:rotate(360deg)}}

    .load-title{font-size:15px;font-weight:700;color:var(--txt);letter-spacing:-.2px;margin-top:4px}
    .load-step{font-size:11px;color:var(--mut);text-align:center;min-height:16px;transition:all .3s}
    .load-bar{width:100%;height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden}
    .load-fill{height:100%;width:0;background:linear-gradient(90deg,#1D4ED8,#10B981);border-radius:2px;transition:width .7s ease;box-shadow:0 0 8px rgba(16,185,129,.4)}
    .load-dots{display:flex;gap:6px;margin-top:2px}
    .load-dot{width:5px;height:5px;border-radius:50%;background:var(--mut)}
    .load-dot:nth-child(1){animation:dotPop .85s 0s ease-in-out infinite}
    .load-dot:nth-child(2){animation:dotPop .85s .17s ease-in-out infinite}
    .load-dot:nth-child(3){animation:dotPop .85s .34s ease-in-out infinite}
    @keyframes dotPop{0%,100%{transform:translateY(0);background:var(--mut)}50%{transform:translateY(-7px);background:var(--grn)}}
  </style>
</head>
<body>
  <div class="bg-mesh">
    <div class="orb orb1"></div>
    <div class="orb orb2"></div>
    <div class="orb orb3"></div>
  </div>
  <div class="grid-dots"></div>
  <div class="page">
    <!-- NAV -->
    <nav>
      <a class="nav-brand" href="/">
        <div class="nav-ico">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M20 7.5L18 5.5L19.5 4L22 6.5L20.5 8L20 7.5ZM20 7.5V16C20 17.1 19.1 18 18 18C16.9 18 16 17.1 16 16V13H14V5C14 3.9 13.1 3 12 3H5C3.9 3 3 3.9 3 5V21H14V15H16V16C16 18.21 17.79 20 20 20C22.21 20 24 18.21 24 16V6L20 7.5Z" fill="white"/></svg>
        </div>
        <span class="nav-logo">Fuel<em>Route</em></span>
      </a>
      <div class="nav-badge"><span class="live-dot"></span>Live Prices</div>
    </nav>

    <!-- HERO -->
    <div class="hero">
      <div class="hero-eyebrow"><span class="hero-eyebrow-dot"></span>Cost-optimized road trip planning</div>
      <h1 class="hero-h">The smartest way to<br/>fuel your road trip</h1>
      <p class="hero-sub">
        2-hop lookahead algorithm across <b>7,500+ stations</b>.<br/>
        Enter your route — we find every cheap stop.
      </p>

      <!-- MAIN CARD -->
      <div class="card">
        <!-- LOCATION -->
        <div class="loc-group">
          <div class="loc-row" id="f-start">
            <span class="loc-dot loc-dot-s"></span>
            <div class="loc-body">
              <div class="loc-lbl">From</div>
              <input class="loc-inp" id="start-input" type="text" placeholder="City, address, or lat,lng&hellip;" autocomplete="off"/>
            </div>
            <div class="loc-actions">
              <button class="ico-btn" onclick="useMyLocation('start')" title="Use my location">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
              </button>
              <span class="loc-ok" id="start-badge"></span>
            </div>
            <div class="drop" id="start-drop"></div>
            <input type="hidden" id="start-val"/>
          </div>
          <div class="swap-row">
            <button class="swap-btn" onclick="swapLocations()" title="Swap">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M7 4L3 8L7 12M17 20L21 16L17 12M4 8H20M4 16H20" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          </div>
          <div class="loc-row" id="f-end">
            <span class="loc-dot loc-dot-e"></span>
            <div class="loc-body">
              <div class="loc-lbl">To</div>
              <input class="loc-inp" id="end-input" type="text" placeholder="City, address, or lat,lng&hellip;" autocomplete="off"/>
            </div>
            <div class="loc-actions">
              <button class="ico-btn" onclick="useMyLocation('end')" title="Use my location">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
              </button>
              <span class="loc-ok" id="end-badge"></span>
            </div>
            <div class="drop" id="end-drop"></div>
            <input type="hidden" id="end-val"/>
          </div>
        </div>

        <!-- VEHICLE PRESETS -->
        <div class="preset-wrap">
          <div class="sec-lbl">Vehicle</div>
          <div class="preset-grid">
            <div class="pcard" data-key="car"    data-range="400"  data-mpg="28" onclick="applyPreset(this)">
              <span class="pcard-ico">&#128663;</span><span class="pcard-name">Car</span><span class="pcard-mpg">28 mpg</span>
            </div>
            <div class="pcard" data-key="suv"    data-range="450"  data-mpg="22" onclick="applyPreset(this)">
              <span class="pcard-ico">&#128665;</span><span class="pcard-name">SUV</span><span class="pcard-mpg">22 mpg</span>
            </div>
            <div class="pcard" data-key="pickup" data-range="500"  data-mpg="18" onclick="applyPreset(this)">
              <span class="pcard-ico">&#128667;</span><span class="pcard-name">Pickup</span><span class="pcard-mpg">18 mpg</span>
            </div>
            <div class="pcard" data-key="truck"  data-range="800"  data-mpg="18" onclick="applyPreset(this)">
              <span class="pcard-ico">&#128666;</span><span class="pcard-name">Truck</span><span class="pcard-mpg">18 mpg</span>
            </div>
            <div class="pcard" data-key="semi"   data-range="1400" data-mpg="6"  onclick="applyPreset(this)">
              <span class="pcard-ico">&#128667;</span><span class="pcard-name">Semi</span><span class="pcard-mpg">6 mpg</span>
            </div>
            <div class="pcard" data-key="rv"     data-range="400"  data-mpg="10" onclick="applyPreset(this)">
              <span class="pcard-ico">&#128656;</span><span class="pcard-name">RV</span><span class="pcard-mpg">10 mpg</span>
            </div>
          </div>
        </div>

        <!-- SPECS -->
        <div class="specs-grid">
          <div class="spec-box">
            <div class="spec-lbl">Tank Range</div>
            <div class="spec-row">
              <input class="spec-inp" id="tank-range" type="number" value="500" min="50" max="2000"/>
              <span class="spec-unit">miles</span>
            </div>
          </div>
          <div class="spec-box">
            <div class="spec-lbl">Efficiency</div>
            <div class="spec-row">
              <input class="spec-inp" id="mpg" type="number" value="10" min="1" max="200"/>
              <span class="spec-unit">MPG</span>
            </div>
          </div>
        </div>

        <!-- ADVANCED TOGGLE -->
        <div style="display:flex;align-items:center;justify-content:flex-end;margin-bottom:14px">
          <button class="adv-btn" id="adv-toggle" onclick="toggleAdv()">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/><path d="M19.07 4.93l-1.41 1.41M6.34 17.66l-1.41 1.41M2 12h2M20 12h2M17.66 6.34l1.41-1.41M4.93 19.07l1.41-1.41M12 2v2M12 20v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            Advanced
            <span class="adv-count" id="adv-count" style="display:none"></span>
          </button>
        </div>
        <input type="hidden" id="opt-tolls" value="0"/>
        <input type="hidden" id="opt-hwy" value="0"/>
        <input type="hidden" id="opt-terrain" value="0"/>
        <input type="hidden" id="opt-pref" value="recommended"/>

        <!-- SUBMIT -->
        <button class="go-btn" id="go-btn" onclick="getRoute()" disabled>
          <div class="go-btn-inner">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M12 2C8.13 2 5 5.13 5 9C5 14.25 12 22 12 22C12 22 19 14.25 19 9C19 5.13 15.87 2 12 2ZM12 11.5C10.62 11.5 9.5 10.38 9.5 9C9.5 7.62 10.62 6.5 12 6.5C13.38 6.5 14.5 7.62 14.5 9C14.5 10.38 13.38 11.5 12 11.5Z" fill="currentColor"/></svg>
            Plan My Route
            <span class="go-btn-ico">&#8594;</span>
          </div>
        </button>
        <p class="hint">Any US city, address, landmark &bull; <code>lat,lng</code> also works</p>
      </div>
    </div>

    <!-- STATS -->
    <div class="stats-strip">
      <div class="stat"><div class="stat-val" id="cnt-stat">7,500+</div><div class="stat-lbl">US Stations</div></div>
      <div class="stat"><div class="stat-val">2-Hop</div><div class="stat-lbl">Lookahead AI</div></div>
      <div class="stat"><div class="stat-val">$0</div><div class="stat-lbl">No API Keys</div></div>
      <div class="stat"><div class="stat-val">&lt;3s</div><div class="stat-lbl">Route Time</div></div>
    </div>

    <!-- FEATURE CHIPS -->
    <div class="feat-row">
      <div class="feat-chip"><span class="feat-chip-ico">&#9981;</span> 6 Vehicle Types</div>
      <div class="feat-chip"><span class="feat-chip-ico">&#128337;</span> Real-time Prices</div>
      <div class="feat-chip"><span class="feat-chip-ico">&#128506;</span> Topo Map View</div>
      <div class="feat-chip"><span class="feat-chip-ico">&#127981;</span> Rural Gap Detection</div>
      <div class="feat-chip"><span class="feat-chip-ico">&#128200;</span> Savings Estimate</div>
    </div>
  </div>

  <script>
    var state={start:null,end:null},timers={};
    var TICONS={city:'&#127961;',town:'&#127961;',village:'&#127960;',hamlet:'&#127960;',suburb:'&#127960;',road:'&#128739;',motorway:'&#128739;',trunk:'&#128739;',primary:'&#128739;',secondary:'&#128739;',residential:'&#127968;',house:'&#127968;',hotel:'&#127976;',restaurant:'&#127869;',museum:'&#127963;',park:'&#127795;',school:'&#127979;',fuel:'&#9981;',parking:'&#127359;',hospital:'&#127973;',airport:'&#9992;'};
    function ico(t){return TICONS[t]||'&#128205;'}
    function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

    function setupField(w){
      var inp=document.getElementById(w+'-input'),
          drop=document.getElementById(w+'-drop'),
          val=document.getElementById(w+'-val'),
          badge=document.getElementById(w+'-badge');
      inp.addEventListener('input',function(){
        val.value='';state[w]=null;badge.style.display='none';updateBtn();
        clearTimeout(timers[w]);
        var q=inp.value.trim();
        if(q.length<2){drop.classList.remove('show');return}
        if(/^-?\\d+\\.?\\d*\\s*,\\s*-?\\d+\\.?\\d*$/.test(q)){
          var pts=q.split(','),lat=parseFloat(pts[0]),lng=parseFloat(pts[1]);
          if(lat>=-90&&lat<=90&&lng>=-180&&lng<=180){selPlace(w,q,q,lat,lng);drop.classList.remove('show');return}
        }
        timers[w]=setTimeout(function(){fetchSugg(w,q)},280)
      });
      inp.addEventListener('keydown',function(e){
        var items=drop.querySelectorAll('.di'),act=drop.querySelector('.di.active');
        var idx=act?Array.from(items).indexOf(act):-1;
        if(e.key==='ArrowDown'){e.preventDefault();idx=Math.min(idx+1,items.length-1)}
        else if(e.key==='ArrowUp'){e.preventDefault();idx=Math.max(idx-1,0)}
        else if(e.key==='Enter'){e.preventDefault();if(act)act.click();else if(items[0])items[0].click();return}
        else if(e.key==='Escape'){drop.classList.remove('show');return}
        items.forEach(function(it,i){it.classList.toggle('active',i===idx)});
        if(items[idx])items[idx].scrollIntoView({block:'nearest'})
      });
      document.addEventListener('click',function(e){
        if(!e.target.closest('#f-'+w)&&!e.target.closest('#'+w+'-drop'))
          drop.classList.remove('show')
      });
    }

    async function fetchSugg(w,q){
      var drop=document.getElementById(w+'-drop');
      try{
        var r=await fetch('/api/search/?q='+encodeURIComponent(q));
        renderDrop(w,await r.json())
      }catch(e){drop.classList.remove('show')}
    }

    function renderDrop(w,res){
      var drop=document.getElementById(w+'-drop');
      if(!res.length){drop.classList.remove('show');return}
      drop.innerHTML=res.map(function(r,i){
        return '<div class="di"><span class="di-ico">'+ico(r.type)+'</span>'
          +'<div class="di-main"><div class="di-lbl">'+esc(r.label)+'</div>'
          +'<div class="di-crd">'+r.coords+'</div></div></div>'
      }).join('');
      drop.querySelectorAll('.di').forEach(function(el,i){
        el.addEventListener('click',function(){
          selPlace(w,res[i].label,res[i].coords,res[i].lat,res[i].lng);
          drop.classList.remove('show')
        })
      });
      drop.classList.add('show')
    }

    function selPlace(w,label,coords,lat,lng){
      document.getElementById(w+'-input').value=label;
      document.getElementById(w+'-val').value=coords;
      var b=document.getElementById(w+'-badge');
      b.style.display='block';
      state[w]={label:label,coords:coords,lat:lat,lng:lng};
      updateBtn()
    }

    function updateBtn(){
      var btn=document.getElementById('go-btn');
      btn.disabled=!(state.start&&state.end)
    }

    function swapLocations(){
      var tmp=state.start;state.start=state.end;state.end=tmp;
      document.getElementById('start-input').value=state.start?state.start.label:'';
      document.getElementById('end-input').value=state.end?state.end.label:'';
      document.getElementById('start-val').value=state.start?state.start.coords:'';
      document.getElementById('end-val').value=state.end?state.end.coords:'';
      document.getElementById('start-badge').style.display=state.start?'block':'none';
      document.getElementById('end-badge').style.display=state.end?'block':'none';
      updateBtn()
    }

    function useMyLocation(w){
      if(!navigator.geolocation){alert('Geolocation not supported');return}
      navigator.geolocation.getCurrentPosition(function(p){
        var lat=p.coords.latitude,lng=p.coords.longitude;
        selPlace(w,'My Location ('+lat.toFixed(4)+'\xb0, '+lng.toFixed(4)+'\xb0)',
          lat.toFixed(6)+','+lng.toFixed(6),lat,lng)
      },function(){alert('Could not get location — check permissions')})
    }

    var _activePreset='';
    function applyPreset(el){
      document.querySelectorAll('.pcard').forEach(function(c){c.classList.remove('on')});
      el.classList.add('on');
      _activePreset=el.dataset.key;
      document.getElementById('tank-range').value=el.dataset.range;
      document.getElementById('mpg').value=el.dataset.mpg;
    }

    function togOpt(btn,hidId){
      btn.classList.toggle('on');
      document.getElementById(hidId).value=btn.classList.contains('on')?'1':'0'
    }

    function getRoute(){
      if(!state.start||!state.end)return;
      document.getElementById('go-btn').disabled=true;
      showLoading();
      var s=encodeURIComponent(state.start.coords),e=encodeURIComponent(state.end.coords);
      var sn=encodeURIComponent(state.start.label),en=encodeURIComponent(state.end.label);
      var tr=document.getElementById('tank-range').value||500;
      var mg=document.getElementById('mpg').value||10;
      var tolls=document.getElementById('opt-tolls').value;
      var hwy=document.getElementById('opt-hwy').value;
      var terrain=document.getElementById('opt-terrain').value;
      var pref=document.getElementById('opt-pref').value;
      window.location.href='/api/route/map/?start='+s+'&end='+e+'&start_name='+sn+'&end_name='+en
        +'&tank_range='+tr+'&mpg='+mg+'&preset='+encodeURIComponent(_activePreset)
        +'&avoid_tolls='+tolls+'&avoid_highways='+hwy+'&avoid_terrain='+terrain
        +'&preference='+pref
    }

    function showLoading(){
      document.getElementById('load-overlay').classList.add('show');
      var fill=document.getElementById('load-fill');
      var step=document.getElementById('load-steps');
      var pct=0,mi=0;
      var msgs=['Geocoding locations…','Fetching road geometry…','Scanning 7,500+ stations…','Optimising fuel stops…'];
      step.textContent=msgs[0];
      setInterval(function(){
        pct=Math.min(pct+(Math.random()*6+4),92);
        fill.style.width=pct+'%';
        var ni=Math.min(Math.floor(pct/24),3);
        if(ni!==mi){mi=ni;step.textContent=msgs[ni]}
      },320);
    }

    function toggleAdv(){
      var ol=document.getElementById('adv-overlay');
      var active=ol.classList.toggle('show');
      document.getElementById('adv-toggle').classList.toggle('active',active);
    }
    function closeAdv(){
      document.getElementById('adv-overlay').classList.remove('show');
      document.getElementById('adv-toggle').classList.remove('active');
    }
    function closeAdvOutside(e){
      if(e.target===document.getElementById('adv-overlay')||e.target.classList.contains('adv-backdrop'))closeAdv();
    }
    function togOpt(btn,hid){
      btn.classList.toggle('on');
      document.getElementById(hid).value=btn.classList.contains('on')?'1':'0';
      updateAdvCount();
    }
    function setPref(val){
      document.getElementById('opt-pref').value=val;
      document.querySelectorAll('.pref-opt').forEach(function(b){b.classList.toggle('on',b.dataset.val===val)});
      updateAdvCount();
    }
    function updateAdvCount(){
      var n=document.querySelectorAll('#adv-panel .opt-tog.on').length;
      var pref=document.getElementById('opt-pref').value!='recommended'?1:0;
      var total=n+pref;
      var cnt=document.getElementById('adv-count');
      cnt.textContent=total||'';
      cnt.style.display=total?'inline':'none';
    }

    // Animate station count
    (function(){
      var el=document.getElementById('cnt-stat'),target=7531,dur=1400,start=null;
      function step(ts){
        if(!start)start=ts;
        var p=Math.min((ts-start)/dur,1);
        var ease=1-Math.pow(1-p,3);
        el.textContent=Math.round(ease*target).toLocaleString()+'+';
        if(p<1)requestAnimationFrame(step)
      }
      setTimeout(function(){requestAnimationFrame(step)},600)
    })();

    setupField('start');
    setupField('end');
  </script>

  <!-- ═══ LOADING OVERLAY ═══ -->
  <div class="load-overlay" id="load-overlay">
    <div class="load-glass">
      <div class="truck-scene">
        <svg width="200" height="100" viewBox="0 0 200 100" class="truck-svg">
          <defs>
            <clipPath id="tc"><rect x="6" y="40" width="104" height="42" rx="4"/></clipPath>
            <filter id="glow-r" x="-60%" y="-60%" width="220%" height="220%">
              <feGaussianBlur stdDeviation="2.5" result="b"/>
              <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
            </filter>
          </defs>
          <!-- road -->
          <rect x="0" y="88" width="200" height="12" fill="rgba(10,18,38,.95)"/>
          <line x1="8" y1="94" x2="38" y2="94" stroke="rgba(255,255,255,.15)" stroke-width="2" stroke-dasharray="12,10"/>
          <line x1="58" y1="94" x2="88" y2="94" stroke="rgba(255,255,255,.15)" stroke-width="2" stroke-dasharray="12,10"/>
          <!-- trailer body -->
          <rect x="5" y="38" width="108" height="46" rx="5" fill="rgba(10,20,44,.95)" stroke="rgba(59,130,246,.4)" stroke-width="1.5"/>
          <!-- grid lines on tank -->
          <line x1="30" y1="42" x2="30" y2="80" stroke="rgba(59,130,246,.1)" stroke-width="1"/>
          <line x1="57" y1="42" x2="57" y2="80" stroke="rgba(59,130,246,.1)" stroke-width="1"/>
          <line x1="84" y1="42" x2="84" y2="80" stroke="rgba(59,130,246,.1)" stroke-width="1"/>
          <!-- fuel fill animation -->
          <rect class="fl" x="6" y="82" width="104" height="0" fill="rgba(16,185,129,.5)" clip-path="url(#tc)"/>
          <rect class="fl-wave" x="-10" y="79" width="130" height="6" rx="2" fill="rgba(16,185,129,.3)" clip-path="url(#tc)"/>
          <!-- cab -->
          <rect x="113" y="50" width="50" height="34" rx="5" fill="rgba(12,24,52,.95)" stroke="rgba(59,130,246,.55)" stroke-width="1.5"/>
          <!-- cab window -->
          <rect x="120" y="57" width="28" height="15" rx="3" fill="rgba(59,130,246,.09)" stroke="rgba(147,197,253,.28)" stroke-width="1"/>
          <!-- cyborg eye -->
          <circle class="eye" cx="128" cy="64" r="3.5" fill="#EF4444" filter="url(#glow-r)"/>
          <circle cx="128" cy="64" r="1.3" fill="rgba(255,255,255,.6)"/>
          <!-- exhaust pipe -->
          <rect x="153" y="42" width="5" height="10" rx="2" fill="rgba(59,130,246,.35)"/>
          <!-- smoke puffs -->
          <circle class="smk s1" cx="156" cy="38" r="4" fill="rgba(70,90,130,.4)"/>
          <circle class="smk s2" cx="154" cy="28" r="5.5" fill="rgba(70,90,130,.28)"/>
          <circle class="smk s3" cx="158" cy="17" r="7" fill="rgba(70,90,130,.18)"/>
          <!-- fuel nozzle arm (cyborg arm refuelling) -->
          <path class="arm" d="M178 6 Q190 6 190 22 L190 52" stroke="rgba(245,158,11,.9)" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          <rect x="186" y="50" width="8" height="13" rx="2.5" fill="rgba(245,158,11,.9)"/>
          <!-- fuel drop -->
          <circle class="drp" cx="190" cy="64" r="3.2" fill="rgba(16,185,129,.95)"/>
          <!-- wheels -->
          <circle cx="32" cy="84" r="10" fill="rgba(6,14,34,.95)" stroke="rgba(59,130,246,.5)" stroke-width="1.5"/>
          <circle class="whl" cx="32" cy="84" r="4.5" fill="rgba(18,40,96,.6)" stroke="rgba(59,130,246,.35)" stroke-width="1"/>
          <circle cx="78" cy="84" r="10" fill="rgba(6,14,34,.95)" stroke="rgba(59,130,246,.5)" stroke-width="1.5"/>
          <circle class="whl" cx="78" cy="84" r="4.5" fill="rgba(18,40,96,.6)" stroke="rgba(59,130,246,.35)" stroke-width="1"/>
          <circle cx="138" cy="84" r="10" fill="rgba(6,14,34,.95)" stroke="rgba(59,130,246,.5)" stroke-width="1.5"/>
          <circle class="whl" cx="138" cy="84" r="4.5" fill="rgba(18,40,96,.6)" stroke="rgba(59,130,246,.35)" stroke-width="1"/>
          <!-- speed lines -->
          <line class="spd" x1="0" y1="60" x2="16" y2="60" stroke="rgba(59,130,246,.35)" stroke-width="2"/>
          <line class="spd2" x1="0" y1="68" x2="10" y2="68" stroke="rgba(59,130,246,.22)" stroke-width="1.5"/>
        </svg>
      </div>
      <div class="load-title">Finding cheapest route&hellip;</div>
      <div class="load-step" id="load-steps">Geocoding locations</div>
      <div class="load-bar"><div class="load-fill" id="load-fill"></div></div>
      <div class="load-dots">
        <div class="load-dot"></div>
        <div class="load-dot"></div>
        <div class="load-dot"></div>
      </div>
    </div>
  </div>

  <!-- ═══ ADVANCED OPTIONS PANEL ═══ -->
  <div class="adv-overlay" id="adv-overlay" onclick="closeAdvOutside(event)">
    <div class="adv-backdrop"></div>
    <div class="adv-panel" id="adv-panel">
      <div class="adv-handle"></div>
      <div class="adv-hdr">
        <div class="adv-title">Advanced Options</div>
        <button class="adv-close-btn" onclick="closeAdv()">&times;</button>
      </div>
      <div class="sec-lbl" style="margin-bottom:10px">Route Avoidances</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px">
        <button class="opt-tog" id="tog-tolls" onclick="togOpt(this,'opt-tolls')">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/><path d="M8 12h8M12 8v8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          No Tolls
        </button>
        <button class="opt-tog" id="tog-hwy" onclick="togOpt(this,'opt-hwy')">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          No Highways
        </button>
        <button class="opt-tog" id="tog-terrain" onclick="togOpt(this,'opt-terrain')">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M3 20l5-8 4 5 4-10 5 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          Flat Roads
        </button>
      </div>
      <div class="sec-lbl" style="margin-bottom:10px">Route Priority</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="pref-opt on" data-val="recommended" onclick="setPref('recommended')">&#10003; Recommended</button>
        <button class="pref-opt" data-val="fastest" onclick="setPref('fastest')">&#9654; Fastest</button>
        <button class="pref-opt" data-val="shortest" onclick="setPref('shortest')">&#8596; Shortest</button>
      </div>
    </div>
  </div>

</body>
</html>"""
