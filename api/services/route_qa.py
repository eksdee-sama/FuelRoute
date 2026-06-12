"""
Route Quality Assurance — supervisor checks for algorithmic correctness.

Public API
----------
run_all_checks(result) -> list of finding dicts
    Each finding: {"level": "PASS"|"WARN"|"FAIL"|"INFO", "check": str, "message": str}

Used by:
  - api/management/commands/qa_route.py  (CLI report)
  - api/views.py                          (inline sidebar badge)
"""

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_consecutive_stops(
    fuel_stops: List[Dict], min_spacing_miles: float = 80.0
) -> Dict:
    """FAIL if any two consecutive stops are closer than min_spacing_miles."""
    if len(fuel_stops) < 2:
        return {"level": "PASS", "check": "consecutive_stops",
                "message": f"{len(fuel_stops)} stop(s) — spacing check N/A"}

    gaps = []
    for i in range(1, len(fuel_stops)):
        gap = fuel_stops[i]["distance_from_start_miles"] - fuel_stops[i - 1]["distance_from_start_miles"]
        gaps.append((gap, i))

    min_gap, min_idx = min(gaps, key=lambda x: x[0])
    if min_gap < min_spacing_miles:
        s_prev = fuel_stops[min_idx - 1]
        s_curr = fuel_stops[min_idx]
        return {
            "level": "FAIL",
            "check": "consecutive_stops",
            "message": (
                f"Stops #{min_idx} and #{min_idx + 1} are only {min_gap:.0f} mi apart "
                f"({s_prev['city']}, {s_prev['state']} → {s_curr['city']}, {s_curr['state']}). "
                f"Min recommended: {min_spacing_miles:.0f} mi."
            ),
        }

    min_gap_val = min(g for g, _ in gaps)
    return {"level": "PASS", "check": "consecutive_stops",
            "message": f"{len(fuel_stops)} stops, min spacing {min_gap_val:.0f} mi"}


def check_min_fill(fuel_stops: List[Dict], min_gallons: float = 1.0) -> Dict:
    """
    WARN if any stop purchases fewer than min_gallons.
    Threshold aligns with the optimizer's deliberate cheap top-ups: with the
    exact DP oracle proving cost-optimality, purchases >= 1 gal are intentional
    price plays, not artifacts; only sub-gallon buys signal a bug.
    """
    small = [
        (s["stop_number"], s["gallons_to_add"], s["city"], s["state"])
        for s in fuel_stops
        if s["gallons_to_add"] < min_gallons
    ]
    if small:
        worst_num, worst_gal, worst_city, worst_state = min(small, key=lambda x: x[1])
        return {
            "level": "WARN",
            "check": "min_fill",
            "message": (
                f"Stop #{worst_num} purchases only {worst_gal} gal "
                f"({worst_city}, {worst_state}). "
                f"Stops buying < {min_gallons:.0f} gal are likely micro-stop artifacts."
            ),
        }
    if not fuel_stops:
        return {"level": "PASS", "check": "min_fill", "message": "No stops to check"}
    min_gal = min(s["gallons_to_add"] for s in fuel_stops)
    return {"level": "PASS", "check": "min_fill",
            "message": f"Smallest purchase {min_gal:.1f} gal — all stops substantial"}


def check_detours(
    fuel_stops: List[Dict],
    max_ok_miles: float = 1.0,
    max_warn_miles: float = 3.0,
) -> Dict:
    """FAIL if any stop is > max_warn_miles off route; WARN if > max_ok_miles."""
    if not fuel_stops:
        return {"level": "PASS", "check": "detours", "message": "No stops to check"}

    worst = max(fuel_stops, key=lambda s: s["distance_from_route_miles"])
    d = worst["distance_from_route_miles"]

    if d > max_warn_miles:
        return {
            "level": "FAIL",
            "check": "detours",
            "message": (
                f"Stop #{worst['stop_number']} ({worst['city']}, {worst['state']}) "
                f"is {d:.1f} mi off route — significant detour for a truck."
            ),
        }
    if d > max_ok_miles:
        return {
            "level": "WARN",
            "check": "detours",
            "message": (
                f"Stop #{worst['stop_number']} ({worst['city']}, {worst['state']}) "
                f"is {d:.1f} mi off route."
            ),
        }
    return {"level": "PASS", "check": "detours",
            "message": f"All stops ≤ {d:.1f} mi from route"}


def check_fuel_feasibility(
    fuel_stops: List[Dict],
    route_miles: float,
    tank_range: float,
    mpg: float,
) -> Dict:
    """FAIL if the vehicle would theoretically run dry between consecutive stops."""
    if not fuel_stops:
        if route_miles <= tank_range:
            return {"level": "PASS", "check": "fuel_feasibility",
                    "message": f"Direct trip {route_miles:.0f} mi — within {tank_range:.0f} mi tank range"}
        return {"level": "FAIL", "check": "fuel_feasibility",
                "message": f"Route is {route_miles:.0f} mi but no stops planned and tank range is {tank_range:.0f} mi"}

    capacity_gal = tank_range / mpg
    fuel = capacity_gal
    prev_dist = 0.0
    issues = []

    for stop in fuel_stops:
        dist = stop["distance_from_start_miles"]
        seg_miles = dist - prev_dist
        fuel -= seg_miles / mpg
        if fuel < -0.5:
            issues.append(
                f"Run dry before stop #{stop['stop_number']} "
                f"({stop['city']}, {stop['state']}) — needed {abs(fuel):.1f} more gal"
            )
        fuel += stop["gallons_to_add"]
        prev_dist = dist

    # Check final leg to destination
    final_leg = route_miles - prev_dist
    fuel -= final_leg / mpg
    if fuel < -0.5:
        issues.append(f"Run dry on final leg to destination")

    if issues:
        return {"level": "FAIL", "check": "fuel_feasibility",
                "message": "; ".join(issues)}
    return {"level": "PASS", "check": "fuel_feasibility",
            "message": "Tank never runs dry between stops"}


def check_cost_comparison(
    fuel_stops: List[Dict],
    all_stations: List[Dict],
    route_miles: float,
    mpg: float,
) -> Dict:
    """INFO: compare algorithm cost vs. naive 'fill full tank at cheapest single station'."""
    algo_cost = sum(s["cost_usd"] for s in fuel_stops)
    if not all_stations or not algo_cost:
        return {"level": "INFO", "check": "cost_comparison",
                "message": "Not enough data for comparison"}

    cheapest_price = min(s["price_per_gallon"] for s in all_stations)
    gallons_needed = route_miles / mpg
    naive_cost = round(gallons_needed * cheapest_price, 2)
    savings = round(naive_cost - algo_cost, 2)
    pct = round(savings / naive_cost * 100, 1) if naive_cost else 0

    if savings > 0:
        msg = f"Algo: ${algo_cost:.2f} vs. single cheapest fill: ${naive_cost:.2f} (saves ${savings:.2f} / {pct}%)"
    else:
        msg = f"Algo: ${algo_cost:.2f} vs. single cheapest fill: ${naive_cost:.2f}"

    return {"level": "INFO", "check": "cost_comparison", "message": msg}


def check_optimality(analytics: Dict) -> Dict:
    """
    INFO: cross-check against the exact DP oracle (Gas Station Problem,
    Khuller et al. 2007). The served plan is always min(greedy, exact),
    so the user's cost is provably optimal whenever the oracle ran.
    """
    if "optimality_gap_usd" not in analytics:
        return {"level": "INFO", "check": "optimality",
                "message": "Exact DP oracle unavailable for this route"}
    gap  = analytics.get("optimality_gap_usd", 0.0)
    used = analytics.get("optimizer_used", "greedy")
    if used == "exact_dp":
        msg = (f"Exact DP plan served — ${gap:.2f} cheaper than the greedy "
               f"heuristic. Cost is provably optimal.")
    elif gap <= 0.01:
        msg = ("Plan verified against exact DP oracle (Gas Station Problem) — "
               "optimality gap $0.00.")
    else:
        msg = f"Greedy plan within ${gap:.2f} of the exact DP optimum."
    return {"level": "INFO", "check": "optimality", "message": msg}


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_all_checks(result: Dict[str, Any]) -> List[Dict]:
    """Run all QA checks on a computed route result. Returns list of findings."""
    fuel_stops = result.get("fuel_stops", [])
    all_stations = result.get("all_route_stations", [])
    summary = result.get("summary", {})
    route = result.get("route", {})

    route_miles = route.get("total_distance_miles", 0)
    tank_range = summary.get("vehicle_specs", {}).get("tank_range_miles", 500)
    mpg = summary.get("vehicle_specs", {}).get("fuel_efficiency_mpg", 10)

    is_direct = result.get("_is_direct", False)
    stops_to_check = [] if is_direct else fuel_stops

    findings = [
        check_consecutive_stops(stops_to_check),
        check_min_fill(stops_to_check),
        check_detours(fuel_stops),
        check_fuel_feasibility(stops_to_check, route_miles, tank_range, mpg),
        check_cost_comparison(fuel_stops, all_stations, route_miles, mpg),
        check_optimality(result.get("_analytics", {})),
    ]
    return findings


def qa_summary(findings: List[Dict]) -> Dict:
    """Aggregate findings into a summary dict for the UI badge."""
    fails = sum(1 for f in findings if f["level"] == "FAIL")
    warns = sum(1 for f in findings if f["level"] == "WARN")
    return {
        "fail_count": fails,
        "warn_count": warns,
        "overall": "FAIL" if fails else ("WARN" if warns else "PASS"),
    }
