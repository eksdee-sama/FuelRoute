"""
Fuel stop optimisation for a US road trip.

Public API
----------
find_stations_on_route(stations, route_geometry, proximity_miles)
    -> list of station dicts enriched with distance_from_start

optimize_fuel_stops(stations_on_route, total_distance, tank_range, mpg,
                    current_fuel_fraction=1.0)
    -> ordered list of {station, gallons, cost} dicts

Edge cases handled
------------------
- Rural dead zones: warns when no stations are reachable; returns partial plan
- Station clusters: collapses pumps within CLUSTER_MI of each other (highway exits)
- Near-empty safety: forces a stop when tank drops below 12% regardless of price
- Micro-stop filter: skips fills < MIN_USEFUL_GAL unless tank is near-empty
- 2-hop bridge: reaches a cheaper distant station via a cheap bridge stop
- Starting fuel: caller can pass current_fuel_fraction < 1 for partial tank
- Zero-price guard: ignores stations with price <= 0 (bad data)
"""

import bisect
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_EARTH_RADIUS_MILES = 3958.8
_LAT_DEG_PER_MILE   = 1.0 / 69.0
_LNG_DEG_PER_MILE   = 1.0 / 53.0

# Cluster radius: pumps within this distance on the route are merged into one.
# US highway exits typically cluster 4-5 stations within 0.5 mi.
_CLUSTER_MI = 3.0

# 2-hop bridge: downstream station must be this much cheaper ($/gal) to
# justify restricting current stop choices to only those that can reach it.
_TWO_HOP_DELTA = 0.05

# Minimum gallons worth stopping for (unless near-empty).
_MIN_USEFUL_GAL = 1.5

# Safety threshold: if tank drops below this fraction, stop regardless of price.
_NEAR_EMPTY_FRAC = 0.12


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return _EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(min(a, 1.0)))


def _point_to_segment(
    lat: float, lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> Tuple[float, float]:
    dlat = b_lat - a_lat
    dlon = b_lon - a_lon
    seg_sq = dlat * dlat + dlon * dlon
    if seg_sq < 1e-14:
        return _haversine(lat, lon, a_lat, a_lon), 0.0
    t = max(0.0, min(1.0, ((lat - a_lat) * dlat + (lon - a_lon) * dlon) / seg_sq))
    return _haversine(lat, lon, a_lat + t * dlat, a_lon + t * dlon), t


# ---------------------------------------------------------------------------
# Route-proximity filtering  (O(n * k), k << m)
# ---------------------------------------------------------------------------

def find_stations_on_route(
    stations,
    route_geometry: dict,
    proximity_miles: float = 5.0,
) -> List[Dict]:
    coords = route_geometry['coordinates']
    route  = [(c[1], c[0]) for c in coords]
    n_segs = len(route) - 1

    cum_dist: List[float] = [0.0]
    for i in range(n_segs):
        cum_dist.append(cum_dist[-1] + _haversine(*route[i], *route[i + 1]))

    seg_len = [_haversine(*route[i], *route[i + 1]) for i in range(n_segs)]

    lat_buf = proximity_miles * _LAT_DEG_PER_MILE * 1.15
    lng_buf = proximity_miles * _LNG_DEG_PER_MILE * 1.15
    seg_box: List[Tuple[float, float, float, float]] = []
    for i in range(n_segs):
        lats = (route[i][0], route[i + 1][0])
        lons = (route[i][1], route[i + 1][1])
        seg_box.append((
            min(lats) - lat_buf, max(lats) + lat_buf,
            min(lons) - lng_buf, max(lons) + lng_buf,
        ))

    all_lats = [p[0] for p in route]
    all_lons = [p[1] for p in route]
    bbox_lat_min = min(all_lats) - lat_buf
    bbox_lat_max = max(all_lats) + lat_buf
    bbox_lon_min = min(all_lons) - lng_buf
    bbox_lon_max = max(all_lons) + lng_buf

    result: List[Dict] = []

    for station in stations:
        if isinstance(station, dict):
            geocoded = station.get('geocoded')
            slat = station.get('latitude')
            slon = station.get('longitude')
            price = station.get('retail_price', 0)
        else:
            geocoded = station.geocoded
            slat = station.latitude
            slon = station.longitude
            price = station.retail_price

        # Skip ungeocodable or zero-price (bad data)
        if not geocoded or slat is None or slon is None:
            continue
        if not price or price <= 0:
            continue

        if not (bbox_lat_min <= slat <= bbox_lat_max and bbox_lon_min <= slon <= bbox_lon_max):
            continue

        min_dist      = math.inf
        best_route_dist = 0.0

        for i in range(n_segs):
            bmin_lat, bmax_lat, bmin_lon, bmax_lon = seg_box[i]
            if slat < bmin_lat or slat > bmax_lat or slon < bmin_lon or slon > bmax_lon:
                continue
            dist, t = _point_to_segment(
                slat, slon,
                route[i][0], route[i][1],
                route[i + 1][0], route[i + 1][1],
            )
            if dist < min_dist:
                min_dist = dist
                best_route_dist = cum_dist[i] + t * seg_len[i]
                if min_dist < 0.05:
                    break

        if min_dist <= proximity_miles:
            s = station if isinstance(station, dict) else {
                'id': station.id, 'opis_id': station.opis_id,
                'name': station.name, 'address': station.address,
                'city': station.city, 'state': station.state,
                'retail_price': station.retail_price,
                'latitude': slat, 'longitude': slon,
            }
            result.append({
                **s,
                'latitude': slat,
                'longitude': slon,
                'distance_from_start': best_route_dist,
                'distance_from_route': round(min_dist, 3),
            })

    return result


# ---------------------------------------------------------------------------
# Clustering: merge nearby pumps into the cheapest representative
# ---------------------------------------------------------------------------

def _cluster_stations(stations: List[Dict]) -> List[Dict]:
    """
    Collapse stations within _CLUSTER_MI of each other along the route into
    a single entry (the cheapest effective price). Prevents micro-stops at
    highway-exit clusters where 4 pumps share the same exit ramp.
    """
    if not stations:
        return stations

    sorted_s = sorted(stations, key=lambda s: s['distance_from_start'])
    clusters: List[Dict] = []
    current_cluster: List[Dict] = [sorted_s[0]]

    for s in sorted_s[1:]:
        if s['distance_from_start'] - current_cluster[0]['distance_from_start'] <= _CLUSTER_MI:
            current_cluster.append(s)
        else:
            clusters.append(min(current_cluster, key=lambda x: x.get('_ep', x['retail_price'])))
            current_cluster = [s]

    clusters.append(min(current_cluster, key=lambda x: x.get('_ep', x['retail_price'])))
    return clusters


# ---------------------------------------------------------------------------
# Effective price (detour-adjusted)
# ---------------------------------------------------------------------------

def _effective_price(station: Dict, capacity_gal: float, mpg: float) -> float:
    """
    Adds a detour penalty for off-highway stations.
    Stations within 0.1 mi are treated as on-route (polyline fit noise).
    """
    d = max(0.0, station.get('distance_from_route', 0.0) - 0.1)
    if d <= 0.0:
        return station['retail_price']
    detour_penalty = (2.0 * d / mpg) * station['retail_price'] / capacity_gal
    return station['retail_price'] + detour_penalty


# ---------------------------------------------------------------------------
# Gap detection: find stretches with no stations
# ---------------------------------------------------------------------------

def _find_dead_zones(
    stations: List[Dict],
    total_distance: float,
    tank_range: float,
) -> List[Dict]:
    """
    Returns list of gaps where a driver could run dry.
    A gap is a stretch > tank_range miles with no stations.
    """
    if not stations:
        if total_distance > tank_range:
            return [{"from_mile": 0, "to_mile": round(total_distance, 1),
                     "gap_miles": round(total_distance, 1)}]
        return []

    sorted_s = sorted(stations, key=lambda s: s['distance_from_start'])
    gaps = []

    prev = 0.0
    for s in sorted_s:
        gap = s['distance_from_start'] - prev
        if gap > tank_range:
            gaps.append({
                "from_mile": round(prev, 1),
                "to_mile":   round(s['distance_from_start'], 1),
                "gap_miles": round(gap, 1),
            })
        prev = s['distance_from_start']

    # Check gap from last station to destination
    final_gap = total_distance - prev
    if final_gap > tank_range:
        gaps.append({
            "from_mile": round(prev, 1),
            "to_mile":   round(total_distance, 1),
            "gap_miles": round(final_gap, 1),
        })

    return gaps


# ---------------------------------------------------------------------------
# Greedy fuel-cost optimiser  (O(n log n) via bisect)
# ---------------------------------------------------------------------------

def optimize_fuel_stops(
    stations_on_route: List[Dict],
    total_distance: float,
    tank_range: float = 500.0,
    mpg: float = 10.0,
    current_fuel_fraction: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    Args:
        current_fuel_fraction: fraction of tank at trip start (0-1).
                               1.0 = full tank (default).
    """
    capacity_gal = tank_range / mpg
    near_empty   = capacity_gal * _NEAR_EMPTY_FRAC

    for s in stations_on_route:
        s['_ep'] = _effective_price(s, capacity_gal, mpg)

    # Cluster first so we don't micro-stop at same exit
    clustered = _cluster_stations(stations_on_route)

    sorted_stations = sorted(clustered, key=lambda s: s['distance_from_start'])
    dists = [s['distance_from_start'] for s in sorted_stations]

    pos  = 0.0
    fuel = capacity_gal * max(0.0, min(1.0, current_fuel_fraction))
    stops: List[Dict] = []
    EPS = 0.5

    while pos + fuel * mpg < total_distance - EPS:
        can_reach = pos + fuel * mpg

        left  = bisect.bisect_right(dists, pos)
        right = bisect.bisect_right(dists, can_reach + EPS)
        in_range = sorted_stations[left:right]

        if not in_range:
            logger.warning(
                "Dead zone: no stations reachable from mile %.0f (range %.0f mi). "
                "Route may be infeasible for this vehicle.",
                pos, tank_range,
            )
            break

        # 2-hop bridge logic: if there's a meaningfully cheaper station just
        # beyond current range, prefer bridge stops that can reach it.
        right_ext = bisect.bisect_right(dists, can_reach + tank_range + EPS)
        beyond = sorted_stations[right:right_ext]
        if beyond:
            cheapest_beyond = min(beyond, key=lambda s: s['_ep'])
            cheapest_in     = min(in_range, key=lambda s: s['_ep'])
            if cheapest_beyond['_ep'] < cheapest_in['_ep'] - _TWO_HOP_DELTA:
                # Restrict to bridge stations that can reach the cheap target
                target_dist = cheapest_beyond['distance_from_start']
                bridge = [
                    s for s in in_range
                    if s['distance_from_start'] + tank_range >= target_dist - EPS
                ]
                if bridge:
                    in_range = bridge

        # Furthest-first tiebreaker: same price → advance as far as possible
        best = min(in_range, key=lambda s: (round(s['_ep'], 3), -s['distance_from_start']))

        dist_driven = best['distance_from_start'] - pos
        fuel -= dist_driven / mpg
        pos   = best['distance_from_start']

        # How much to buy
        ahead_left  = bisect.bisect_right(dists, pos + EPS)
        ahead_right = bisect.bisect_right(dists, pos + tank_range)
        cheaper_ahead = [
            s for s in sorted_stations[ahead_left:ahead_right]
            if s['_ep'] < best['_ep']
        ]

        remaining_miles = total_distance - pos
        is_near_empty   = fuel <= near_empty

        if cheaper_ahead and not is_near_empty:
            target = min(cheaper_ahead, key=lambda s: s['_ep'])
            need   = (target['distance_from_start'] - pos) / mpg
            buy    = max(0.0, need - fuel)
            # If target is past the destination, just top up enough to arrive
            if target['distance_from_start'] >= total_distance:
                buy = max(0.0, min(capacity_gal, remaining_miles / mpg) - fuel)
        else:
            buy = max(0.0, min(capacity_gal, remaining_miles / mpg) - fuel)

        # Skip micro-fills unless we're near empty
        if buy < _MIN_USEFUL_GAL and not is_near_empty:
            if fuel * mpg >= remaining_miles - EPS:
                break
            # Force minimum buy to reach the next cheaper station
            buy = _MIN_USEFUL_GAL

        # Never exceed physical tank capacity
        buy = min(buy, max(0.0, capacity_gal - fuel))

        if buy > 0.001:
            stops.append({
                'station': best,
                'gallons': round(buy, 2),
                'cost':    round(buy * best['retail_price'], 2),
            })
            fuel += buy

        # Safety: if we somehow can already reach destination, stop
        if fuel * mpg >= remaining_miles - EPS:
            break

    return stops


def get_route_analytics(
    stations_on_route: List[Dict],
    stops: List[Dict],
    total_distance: float,
    tank_range: float,
    mpg: float,
) -> Dict:
    """
    Returns extra analytics: dead zones, savings estimate, coverage stats.
    """
    dead_zones = _find_dead_zones(stations_on_route, total_distance, tank_range)

    # Naive cost: fill full tank at the very first station (worst case)
    total_gal = total_distance / mpg
    prices = [s['retail_price'] for s in stations_on_route if s.get('retail_price', 0) > 0]
    if prices:
        max_price  = max(prices)
        min_price  = min(prices)
        avg_price  = sum(prices) / len(prices)
        naive_cost = round(total_gal * max_price, 2)
        opt_cost   = sum(s['cost'] for s in stops)
        savings    = round(naive_cost - opt_cost, 2)
    else:
        max_price = min_price = avg_price = 0
        naive_cost = savings = 0

    return {
        "dead_zones":          dead_zones,
        "has_coverage_gaps":   len(dead_zones) > 0,
        "price_range":         {"min": round(min_price, 3), "max": round(max_price, 3), "avg": round(avg_price, 3)} if prices else {},
        "naive_worst_cost":    naive_cost,
        "estimated_savings":   max(0, savings),
        "station_density":     round(len(stations_on_route) / max(total_distance, 1) * 100, 1),
    }
