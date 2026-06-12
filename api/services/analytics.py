"""
Rich fuel-route analytics — derived entirely from existing route data.
Zero external API calls. All metrics computable from stations + stops + route.

Public API
----------
compute(result) -> dict
"""

import math
from typing import Any, Dict, List


_CO2_LBS_PER_GALLON = 19.6   # EPA: gasoline combustion
_AVG_STOP_MINUTES   = 8      # pump + walk + pay
_AVG_SPEED_MPH      = 60     # highway average for time estimates


def _stdev(vals: List[float]) -> float:
    if len(vals) < 2:
        return 0.0
    n = len(vals)
    mean = sum(vals) / n
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))


def _price_zone(mile: float, total: float) -> str:
    pct = mile / max(total, 1)
    if pct < 0.25:
        return "Early"
    if pct < 0.5:
        return "Mid-early"
    if pct < 0.75:
        return "Mid-late"
    return "Late"


def compute(result: Dict[str, Any]) -> Dict[str, Any]:
    route       = result.get("route", {})
    stops       = result.get("fuel_stops", [])
    all_st      = result.get("all_route_stations", [])
    summary     = result.get("summary", {})
    analytics   = result.get("_analytics", {})
    terrain     = result.get("_terrain", {})
    road_q      = result.get("_road_quality", {})
    is_direct   = result.get("_is_direct", False)

    total_mi    = route.get("total_distance_miles", 0)
    total_gal   = summary.get("total_gallons_purchased", 0)
    total_cost  = summary.get("total_fuel_cost_usd", 0)
    mpg         = summary.get("vehicle_specs", {}).get("fuel_efficiency_mpg", 20)
    tank_range  = summary.get("vehicle_specs", {}).get("tank_range_miles", 400)

    prices      = [s["price_per_gallon"] for s in all_st if s.get("price_per_gallon", 0) > 0]
    stop_prices = [s["price_per_gallon"] for s in stops if s.get("price_per_gallon", 0) > 0]

    # ── Price statistics ─────────────────────────────────────────────────────
    pr = analytics.get("price_range", {})
    min_p   = pr.get("min", min(prices) if prices else 0)
    max_p   = pr.get("max", max(prices) if prices else 0)
    avg_p   = pr.get("avg", sum(prices) / len(prices) if prices else 0)
    std_p   = _stdev(prices)
    chosen_avg = sum(stop_prices) / len(stop_prices) if stop_prices else avg_p

    # Beat-the-average: how much cheaper per gallon than route average
    savings_per_gal = round(avg_p - chosen_avg, 3)

    # Worst-case: filled entire trip at highest price found
    naive_cost  = round(total_gal * max_p, 2) if max_p else 0
    savings_vs_worst = round(naive_cost - total_cost, 2) if naive_cost else 0

    # Savings vs average price
    savings_vs_avg = round(total_gal * avg_p - total_cost, 2) if avg_p else 0

    # ── Price gradient (trend east/west, north/south) ─────────────────────
    # Use all_st sorted by distance_from_start to detect if prices rise or fall
    sorted_st = sorted(all_st, key=lambda s: s.get("distance_from_start_miles", 0))
    n = len(sorted_st)
    gradient = "Flat"
    if n >= 6:
        first_half = [s["price_per_gallon"] for s in sorted_st[:n//2] if s.get("price_per_gallon", 0) > 0]
        last_half  = [s["price_per_gallon"] for s in sorted_st[n//2:] if s.get("price_per_gallon", 0) > 0]
        if first_half and last_half:
            diff = sum(last_half)/len(last_half) - sum(first_half)/len(first_half)
            if diff >  0.05:
                gradient = "Rising (fuel gets pricier ahead — fill more early)"
            elif diff < -0.05:
                gradient = "Falling (cheaper fuel ahead — hold back, fill later)"

    # ── Zone breakdown: price by route quarter ───────────────────────────
    zones: Dict[str, List[float]] = {"Early": [], "Mid-early": [], "Mid-late": [], "Late": []}
    for s in all_st:
        z = _price_zone(s.get("distance_from_start_miles", 0), total_mi)
        p = s.get("price_per_gallon", 0)
        if p > 0:
            zones[z].append(p)
    zone_avg = {z: round(sum(v)/len(v), 3) for z, v in zones.items() if v}
    cheapest_zone = min(zone_avg, key=zone_avg.get) if zone_avg else "—"

    # ── Environmental impact ─────────────────────────────────────────────
    co2_lbs = round(total_gal * _CO2_LBS_PER_GALLON, 1)
    co2_kg  = round(co2_lbs * 0.453592, 1)
    trees_days = round(co2_kg / 0.06, 0)   # avg tree absorbs ~60g CO2/day

    # ── Time analysis ────────────────────────────────────────────────────
    n_stops = len(stops) if not is_direct else 1
    stop_time_min = n_stops * _AVG_STOP_MINUTES
    drive_time_hr = total_mi / _AVG_SPEED_MPH
    total_time_hr = round(drive_time_hr + stop_time_min / 60, 2)

    # ── Fuel efficiency vs terrain ───────────────────────────────────────
    t_factor = terrain.get("mpg_factor", 1.0)
    rq_penalty = road_q.get("extra_fuel_pct", 0.0) / 100
    effective_mpg = round(mpg * t_factor * (1 - rq_penalty), 1)
    range_loss_mi = round(tank_range - tank_range * t_factor * (1 - rq_penalty), 0)

    # ── Stop efficiency ──────────────────────────────────────────────────
    if stops and not is_direct:
        gal_per_stop = round(total_gal / len(stops), 2)
        cost_per_stop = round(total_cost / len(stops), 2)
        stop_spacing_mi = round(total_mi / (len(stops) + 1), 1)
    else:
        gal_per_stop = total_gal
        cost_per_stop = total_cost
        stop_spacing_mi = total_mi

    # ── Coverage stats ───────────────────────────────────────────────────
    station_density = analytics.get("station_density", 0)
    dead_zones = analytics.get("dead_zones", [])

    return {
        # Price
        "price_min":          round(min_p, 3),
        "price_max":          round(max_p, 3),
        "price_avg":          round(avg_p, 3),
        "price_std":          round(std_p, 3),
        "price_volatility":   "High" if std_p > 0.15 else ("Medium" if std_p > 0.07 else "Low"),
        "chosen_avg_price":   round(chosen_avg, 3),
        "savings_per_gallon": savings_per_gal,
        "savings_vs_worst":   max(0, savings_vs_worst),
        "savings_vs_avg":     max(0, savings_vs_avg),
        "naive_worst_cost":   naive_cost,
        # Gradient
        "price_gradient":     gradient,
        "zone_avg_prices":    zone_avg,
        "cheapest_zone":      cheapest_zone,
        # Environment
        "co2_lbs":            co2_lbs,
        "co2_kg":             co2_kg,
        "trees_days":         int(trees_days),
        # Time
        "stop_time_min":      stop_time_min,
        "total_trip_time_hr": total_time_hr,
        # Efficiency
        "effective_mpg":      effective_mpg,
        "nominal_mpg":        mpg,
        "range_loss_mi":      int(range_loss_mi),
        # Stop quality
        "gal_per_stop":       gal_per_stop,
        "cost_per_stop":      cost_per_stop,
        "stop_spacing_mi":    stop_spacing_mi,
        # Coverage
        "station_density":    station_density,
        "dead_zone_count":    len(dead_zones),
        "dead_zones":         dead_zones,
    }
