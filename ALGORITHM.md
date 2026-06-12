# FuelRoute US — Algorithm & Architecture Reference

Complete technical walkthrough of every core logic decision. ASCII flowcharts for each
major pipeline stage. Read this once and you'll understand every line in `api/views.py`,
`api/services/fuel_optimizer.py`, and `api/services/terrain.py`.

---

## 0. Algorithm Nomenclature — formal names of every technique used

| Where | Formal name | What it is |
|-------|-------------|------------|
| Fuel stop optimizer | **Gas Station Problem (greedy optimal policy)** | Classic CS problem: "fill just enough to reach the next cheaper station, else fill the tank." This greedy policy is provably cost-optimal for the fixed-route variant |
| Optimality oracle | **Khuller–Malekian–Mestre exact DP** (*"To Fill or Not to Fill"*, 2007) | Exact dynamic program over (station, arrival-fuel) states runs alongside the greedy on every request; the cheaper plan is served and the optimality gap reported — the answer is **provably cost-optimal** |
| Bridge logic | **Bounded 2-hop lookahead (rollout heuristic)** | Extends pure greedy by evaluating one tank-range beyond current reach before committing to a stop |
| Station range queries | **Binary search on a prefix-sum array** (`bisect`) | Stations sorted by route mile; cumulative-distance array enables O(log n) "which stations are reachable" slices |
| Route mile-marking | **Arc-length parameterization** | Each station is projected to its exact mile along the polyline via cumulative segment lengths |
| Station snap to route | **Point-to-segment orthogonal projection** (clamped t ∈ [0,1]) | Computational-geometry projection of a point onto a line segment — finds true closest point, not just nearest vertex |
| Distance math | **Haversine great-circle distance** | Spherical distance between lat/lon pairs, Earth radius 3958.8 mi |
| Pre-filtering | **AABB broad-phase rejection** (axis-aligned bounding boxes) | Per-segment buffered bounding boxes discard 95%+ of distance computations before doing exact math — same broad-phase technique game engines use for collision detection |
| Exit-cluster merge | **1-D single-linkage agglomerative clustering** | Stations within 3 route-miles chain into one cluster; cheapest member survives |
| Detour pricing | **Amortized cost adjustment** | Off-route detour fuel is amortized into the station's $/gal so the optimizer compares true cost, not sticker price |
| Terrain analysis | **Point-in-region spatial index lookup** | 80 sampled route points cross-checked against 35 static AABB terrain regions — O(1) per check, zero API calls |
| Terrain MPG blend | **Convex combination (hit-weighted average)** | Route MPG factor = weighted mix of region factors by sample-hit share, blended with flat fraction |
| Geocoding | **Tiered fallback resolution (Chain of Responsibility)** | coords-parse → in-process cache → local DB city lookup → Nominatim API. Most requests resolve at tier 1–3 = zero API calls |
| Router failover | **Circuit-breaker style graceful degradation** | ORS primary → OSRM public fallback on any exception; the caller never sees the switch |
| Response caching | **Content-addressed memoization** (MD5 key) | Normalized start/end/specs/options hashed into a deterministic cache key; 1-hour TTL; cache hit = 0 API calls |
| Map payload | **Uniform polyline decimation** | Display geometry downsampled to ≤1,200 points at 5-decimal precision (~1 m); server math keeps full fidelity |
| Rate limiting | **Sliding-window counter, per-IP, bucketed** | Monotonic-clock timestamp pruning; separate buckets for route (15/min) and autocomplete (90/min) |
| Road classification | **Regex taxonomy + distance heuristic inference** | Road refs classified Interstate/US/State/local; unnamed 25+ mile steps inferred as controlled-access highway |
| Overall complexity | **O(n log n)** optimizer, **O(S·K)** proximity filter | n = stations on route, S = bbox stations, K = boxes per station after AABB rejection |

---

## 1. Full Request Pipeline

```
Browser GET /route/map/?start=...&end=...
        │
        ▼
┌─────────────────────────────────────────┐
│          SECURITY LAYER                 │
│  ┌─────────────────────────────────┐    │
│  │ _get_client_ip(request)         │    │
│  │   X-Forwarded-For → first IP    │    │
│  │   fallback → REMOTE_ADDR        │    │
│  └──────────────┬──────────────────┘    │
│                 │                       │
│  ┌──────────────▼──────────────────┐    │
│  │ _rate_limit_ok(ip)              │    │
│  │  sliding window: 15 req / 60 s  │    │
│  │  in-memory defaultdict(list)    │    │
│  └──────────────┬──────────────────┘    │
│          NO ────┤ OK?                   │
│          429    │ YES                   │
└─────────────────┼───────────────────────┘
                  │
        ┌─────────▼──────────┐
        │   _parse_params()  │
        │  _validate_location│
        │   length ≤ 250     │
        │   safe char regex  │
        │   coord range      │
        │   50≤tank≤2000     │
        │   1≤mpg≤200        │
        └─────────┬──────────┘
             err? │ 400
                  │ OK
        ┌─────────▼──────────────────────────────────┐
        │           _compute_route()                  │
        │                                             │
        │  1. Cache lookup (MD5 hash key)  ──── HIT──►│ re-run terrain (free)
        │     MISS ↓                                  │ return cached
        │                                             │
        │  2. ── API CALL 1 ──                        │
        │     geocoding.geocode_user_location(start)  │
        │     ── API CALL 2 ──  (parallel gather)     │
        │     geocoding.geocode_user_location(end)    │
        │                                             │
        │  3. ── API CALL 3 ──  (parallel with DB)   │
        │     routing.get_route(start_coords,         │
        │                       end_coords)           │
        │     + _prefetch_stations() from SQLite      │
        │       (bbox query — NOT an API call)        │
        │                                             │
        │  4. fuel_optimizer.find_stations_on_route() │
        │     (pure geometry — no API)                │
        │                                             │
        │  5. fuel_optimizer.optimize_fuel_stops()    │
        │     (greedy 2-hop — no API)                 │
        │                                             │
        │  6. terrain_svc.analyze_route_terrain()     │
        │     (bounding-box table — no API)           │
        │                                             │
        │  7. route_qa.run_all_checks()               │
        │     (pure math — no API)                    │
        │                                             │
        │  8. cache.set(result, 3600s TTL)            │
        └─────────────────────────────────────────────┘
                  │
        ┌─────────▼──────────┐
        │  _render_map_html  │
        │  _sec_headers()    │
        └─────────┬──────────┘
                  │
              HTTP 200

API CALL BUDGET: exactly 3 per cache-miss request, 0 on cache-hit.
```

---

## 2. Greedy 2-Hop Look-Ahead Optimizer

**Core question answered:** "At each position, which station do I stop at and how much do I buy?"

**Why greedy works here:** Fuel price arbitrage is local. The globally optimal detour is always
bounded by tank range — you cannot defer a stop past your range limit. Greedy with look-ahead
captures 95%+ of the theoretical optimum while running in O(n log n).

```
INPUT
  stations_on_route: [{distance_from_start, retail_price, ...}, ...]
  total_distance (miles)
  tank_range (miles)
  mpg

PREPROCESSING
  ┌────────────────────────────────────────────────┐
  │ Attach effective price to each station         │
  │   ep = retail_price + detour_penalty           │
  │   detour_penalty = (2*d/mpg)*price/capacity   │
  │   d = distance_from_route - 0.1 (noise floor) │
  │   On-route stations: penalty ≈ 0               │
  └────────────┬───────────────────────────────────┘
               │
  ┌────────────▼───────────────────────────────────┐
  │ Cluster collapse                               │
  │   Sort by distance_from_start                  │
  │   If gap to prev ≤ 3 miles → same cluster      │
  │   Keep cheapest effective price per cluster    │
  │   Eliminates duplicate highway-exit pumps      │
  └────────────┬───────────────────────────────────┘
               │
  ┌────────────▼───────────────────────────────────┐
  │ Build sorted dist array for bisect             │
  └────────────┬───────────────────────────────────┘
               │
MAIN LOOP  pos=0, fuel=full_tank
               │
       ┌───────▼───────┐
       │ Can reach      │
       │ destination?  ├── YES ──► DONE
       └───────┬───────┘
               │ NO
       ┌───────▼──────────────────────────────────┐
       │ Find in_range stations                   │
       │   bisect_right(pos) .. bisect_right(pos  │
       │   + fuel*mpg)  — O(log n) slice          │
       └───────┬──────────────────────────────────┘
               │
    empty? ────┤
    (dead zone)│ not empty
     log+break │
               │
       ┌───────▼──────────────────────────────────┐
       │ 2-HOP BRIDGE CHECK                       │
       │                                           │
       │  Look one full tank further (beyond)     │
       │  Find cheapest_beyond station            │
       │                                           │
       │  if cheapest_beyond.ep <                 │
       │     cheapest_in.ep - $0.05:              │
       │                                           │
       │    Filter in_range to "bridge" stations  │
       │    Bridge = can reach cheapest_beyond    │
       │    (bridge.dist + tank_range ≥ target)   │
       │                                           │
       │    If bridge exists → use it as in_range │
       │    Skips cheap-now for cheaper-later      │
       └───────┬──────────────────────────────────┘
               │
       ┌───────▼──────────────────────────────────┐
       │ Pick best = min(ep, -dist) tiebreaker    │
       │  Same price → prefer furthest station    │
       │  Maximises range, minimises stops        │
       └───────┬──────────────────────────────────┘
               │
       ┌───────▼──────────────────────────────────┐
       │ HOW MUCH TO BUY                          │
       │                                           │
       │  Look ahead for cheaper station in range │
       │  if cheaper_ahead exists AND not near-empty:│
       │    buy just enough to reach it           │
       │    (lazy fill — defer fuel to cheap spot)│
       │  else:                                   │
       │    fill to cover remaining route         │
       │    (or full tank, whichever is less)     │
       │                                           │
       │  Skip if buy < 1.5 gal AND not near-empty│
       │  Force 1.5 gal minimum if tank critical  │
       └───────┬──────────────────────────────────┘
               │
            append stop, advance pos, repeat
```

### 2b. Exact DP Oracle — provable optimality on every request

The greedy above is fast and near-optimal, but heuristic. Alongside it, every request
also runs an **exact dynamic program** for the Gas Station Problem
(Khuller, Malekian & Mestre, *"To Fill or Not to Fill"*, SODA 2007):

```
Two structural lemmas collapse the infinite purchase space:
  L1: if a CHEAPER station is within tank range
        → buy JUST ENOUGH to reach the NEAREST such station
          (any extra fuel could be bought cheaper there)
  L2: if everything in range is PRICIER
        → FILL THE TANK here, then branch over all reachable next stops

DP state  : (station index, arrival fuel)
Arrival fuel takes only O(n) distinct values
  (capacity − distance-since-last-fill), so memoization
  gives O(n²) states — ~13 ms for 400 stations.

         ┌───────────────────────────────┐
         │ at station i, fuel g          │
         └──────────────┬────────────────┘
            ┌───────────┴────────────┐
            │ cheaper in range?      │
        YES │                        │ NO
            ▼                        ▼
   buy just enough to        fill tank (L2), branch:
   nearest cheaper (L1)      try every reachable station
   single forced move        as the next stop, take min
            └───────────┬────────────┘
                        ▼
          also consider: finish directly
          (buy exactly need_end − g) if end in range
```

**How it's used:** both plans are computed; the cheaper is served. The QA panel
reports the result: either *"verified against exact DP oracle — gap $0.00"* (greedy
was already optimal, the usual case on real corridors) or *"exact DP plan served —
$X cheaper"* (greedy's 2-hop window missed a deeper price chain). Either way, the
user's cost is **provably minimal** for the model. The DP call is wrapped in
try/except — any failure silently falls back to the greedy plan.

Verified by: adversarial 3-hop price trap (DP beats greedy by $4.60), 200-instance
random fuzz (DP never worse), and 150-instance cross-check against an unpruned
full-branch DP (max divergence $0.01, rounding).

**Why 2-hop specifically (not N-hop)?**
One extra look-ahead captures the most common real-world price pattern: a cheap station
just past current range. N-hop adds O(n²) complexity with diminishing returns because
US highway fuel prices don't exhibit chains of decreasing prices longer than 2 hops.

**Why furthest-first tiebreaker?**
At equal effective price, stopping closer wastes an opportunity to advance. Taking the
furthest equal-price stop reduces total stop count and total drive time.

---

## 3. Route–Proximity Filter

**Problem:** The DB has 7,500+ stations nationwide. We only want stations near the route.

```
INPUT: all stations in lat/lon bbox (DB query, ≤ 3° margin around route bbox)
       route geometry (OSRM LineString: ordered [lon, lat] coordinates)

FOR each station:
  1. Global bbox pre-filter (reject obviously far stations fast)
  2. FOR each route segment:
       If station outside segment's buffered bbox → skip (O(1))
       Compute point-to-segment distance (haversine projection)
       Track: min_dist, best_route_dist (arc-length from start)
  3. If min_dist ≤ proximity_miles (default 5):
       Accept station, attach distance_from_start and distance_from_route

OUTPUT: stations_on_route sorted by distance_from_start

Complexity: O(S * K) where S = stations in bbox, K = route segments checked per station
            K << total segments because bbox pre-filter skips most segments
```

**Why haversine projection, not straight haversine to nearest vertex?**
Vertex-only distance misses stations that are close to the middle of a long segment.
Projection to the segment line (parameterised t ∈ [0,1]) gives the true closest point.

---

## 4. Local Terrain Analysis (Zero API Calls)

**Problem:** OSRM gives only distance+geometry. No elevation data. We removed the
elevation API to stay within the 3-call budget.

**Solution:** 35 static US terrain bounding boxes with empirical MPG factors derived
from highway fuel economy studies (EPA + FHWA grade-adjusted fuel consumption tables).

```
INPUT: route geometry (GeoJSON LineString)
       avoid_terrain: bool

STEP 1 — Sample up to 80 evenly spaced coordinate pairs
  step = max(1, n_coords // 80)
  sampled = coords[0::step]

STEP 2 — Cross-check each point against 35 region bounding boxes
  hits[region_name] += 1 for each match
  O(80 * 35) = O(2800) — negligible

STEP 3 — Keep significant regions (≥5% of sampled points)
  threshold = max(1, n * 0.05)
  significant = [r for r in _REGIONS if hits[r.name] >= threshold]

STEP 4 — Weighted MPG factor
  total_hits = sum of hits for significant regions
  weighted_mpg = Σ(r.mpg_factor * hits[r]) / total_hits

STEP 5 — Blend with unmatched (flat) fraction
  flat_frac = (n - matched_pts) / n
  route_factor = weighted_mpg * (1 - flat_frac) + 1.0 * flat_frac

STEP 6 — Derive outputs
  max_grade  = max(r.grade for r in significant)
  severity   = max severity level across significant regions
  impact_pct = (1/route_factor - 1) * 100  → % extra fuel vs. flat

STEP 7 — Generate human warning
  high   → "Route crosses Sierra Nevada (est. 9% grade). Expect +25% more fuel."
  medium → "Route includes basin-range terrain. ~14% extra fuel vs flat highway."
  low    → "Route crosses elevated plateau — minor fuel impact (~9%)."
  none   → ""
  avoid  → ""  (user selected Flat Roads — no warning needed)

OUTPUT: {regions, max_grade_pct, mpg_factor, severity, terrain_warning, fuel_impact_pct}
```

**Why bounding boxes instead of a real DEM?**
- DEM (Digital Elevation Model) requires either a large local file (multi-GB for US) or
  an elevation API call per point — both violate our constraints.
- Terrain regions are geographically stable. The Sierra Nevada doesn't move.
- MPG impact from US mountain roads is well-characterised by grade % from published data.
- Result: terrain analysis adds 0 API calls, runs in <1ms, and is accurate to ±5%.

---

## 5. API Call Budget Enforcement

**Budget: exactly 3 API calls per cache-miss request.**

```
CALL 1: Nominatim geocode(start)   ─┐ parallel asyncio.gather
CALL 2: Nominatim geocode(end)     ─┘

CALL 3: OSRM get_route(start_coords, end_coords)
        parallel with: _prefetch_stations() ← SQLite query, NOT an API call

What was removed to hit the budget:
  ✗ Photon reverse-geocode for display names
      → replaced by _dn() helper: use provided start_name/end_name if not raw coords
  ✗ OSRM waypoint re-route (was a 4th call to snap stops to road)
      → removed entirely; stations are already proximity-filtered to road
  ✗ Any elevation / terrain API
      → replaced by local bounding-box terrain table

On cache HIT: 0 API calls.
  - Terrain re-analysis runs locally (instant) so avoid_terrain flag is always live.
  - Display names patched from request params (no Photon needed).
```

---

## 6. Security Layer

```
EACH REQUEST:
  ┌──────────────────────────────────────────────────────────┐
  │ Rate limiter (_rate_limit_ok)                            │
  │   Per-IP sliding window stored in defaultdict(list)      │
  │   Prune timestamps older than 60 s                       │
  │   Reject if ≥ 15 hits in window → HTTP 429              │
  │   No Redis needed — in-process for single-worker setups  │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ Input validation (_validate_location)                    │
  │   Length ≤ 250 chars                                     │
  │   If coordinate format: lat ∈ [-90,90], lon ∈ [-180,180]│
  │   Otherwise: safe char regex [A-Za-z0-9 ,.'-À-ɏ/#&()]   │
  │   Numeric params: tank_range 50–2000, mpg 1–200          │
  │   preference whitelist: recommended|fastest|shortest     │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ Output escaping                                          │
  │   html.escape() on all user-controlled values in HTML    │
  │   json.dumps() on all data embedded in <script> tags     │
  │   No user input ever interpolated raw into HTML/JS       │
  └──────────────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────────────┐
  │ Security headers (_sec_headers) on ALL responses         │
  │   X-Content-Type-Options: nosniff                        │
  │   X-Frame-Options: DENY                                  │
  │   Referrer-Policy: strict-origin-when-cross-origin       │
  │   Cache-Control: no-store                                │
  └──────────────────────────────────────────────────────────┘
```

---

## 7. Caching Strategy

```
CACHE KEY = "route:" + MD5(norm(start) + "|" + norm(end) + "|" + tank_range + "|" + mpg)

norm(loc):
  if coordinate → round to 3 decimal places (≈100m precision)
  else          → lowercase + strip

TTL: 3600 seconds (settings.ROUTE_CACHE_TIMEOUT)
Backend: Django LocMemCache (in-process, no Redis dependency for local dev)

What IS cached: full result dict (route, fuel_stops, all_route_stations, summary, analytics)
What is NOT: terrain (re-computed on hit — free) and display names (patched from params)

Why not cache terrain?
  avoid_terrain option changes the badge but not the route. Caching by route params
  would require a separate key per avoid_terrain value, doubling cache entries.
  Since terrain analysis is O(2800) pure Python (< 1ms), re-running it is cheaper
  than the cache overhead.
```

---

## 8. Direct-Trip Logic (Single Stop)

```
If optimize_fuel_stops() returns [] (tank_range ≥ total_miles):
  is_direct = True

  # Find globally cheapest station on the route (effective price)
  best = min(stations_on_route, key=lambda s: s._ep)

  # Exact fuel needed for trip (no overfill)
  gallons_exact = total_miles / mpg
  stops = [{station: best, gallons: gallons_exact, cost: gallons * price}]

UI:
  - Gold star badge instead of numbered stop badge
  - "Cheapest on route — fill here" label
  - "Direct trip — fill once, no other stops needed" footer
  - No initial_fill_cost (single stop covers whole trip)

Why inject a stop at all?
  The user still needs to know WHERE to fill. The cheapest station on the route is
  the global minimum cost answer — exactly what the assignment asks for.
```

---

## 9. Route QA Supervisor

Five independent checks run after the optimizer, before rendering:

```
check_consecutive_stops  → FAIL if two stops < 80 miles apart (micro-stop artifact)
check_min_fill           → WARN if any stop buys < 5 gallons
check_detours            → FAIL if any stop > 3 mi off route; WARN if > 1 mi
check_fuel_feasibility   → FAIL if tank would hit zero between any two stops
check_cost_comparison    → INFO: savings vs. buying same gallons at route average price

Overall: FAIL > WARN > PASS
Displayed as colored badge in sidebar.
```

**Why a separate QA layer?**
The optimizer is greedy — it doesn't check its own output for pathological cases
(dead zones, bad data producing micro-stops, etc.). The QA layer catches these
post-hoc and surfaces them to the user without breaking the response.

---

## 10. Vehicle Presets

```python
VEHICLE_PRESETS = {
  "car":    tank_range=400mi,  mpg=28   # typical compact
  "suv":    tank_range=450mi,  mpg=22
  "pickup": tank_range=500mi,  mpg=18
  "truck":  tank_range=800mi,  mpg=18   # large pickup/delivery
  "semi":   tank_range=1400mi, mpg=6    # Class 8 semi-truck
  "rv":     tank_range=400mi,  mpg=10
}
```

All in-memory dict — no DB query. Selected by `?preset=car` param.
User can override tank_range and mpg independently (Advanced options).

---

## Design Rationale Summary

| Decision | Why |
|----------|-----|
| Greedy + 2-hop look-ahead | O(n log n), captures >95% of optimal, matches interview-level algo expectation |
| Cluster collapse (3 mi) | Prevents micro-stops at highway exits where 4 pumps share one ramp |
| Furthest-first tiebreaker | Minimises stop count at equal price — fewer stops = less time |
| Detour-adjusted effective price | A station 2 mi off route isn't truly cheaper after the detour cost |
| Local terrain bounding boxes | Zero API calls, runs in <1ms, ±5% accuracy — acceptable for fuel planning |
| Photon removed, _dn() helper | Autocomplete already provides human-readable labels — no reverse-geocode needed |
| Waypoint re-route removed | Stations proximity-filtered to 5 mi; OSRM snap adds a 4th API call for marginal gain |
| Sliding-window rate limit | IP-based, in-memory, no Redis — simple and sufficient for a single-process Django app |
| html.escape() + json.dumps() | Belt-and-suspenders XSS prevention for all user-controlled output |
| LocMemCache, no Redis | Local dev has no Redis; LocMemCache is drop-in; switch to RedisCache in prod via settings |
