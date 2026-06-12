"""
Async geocoding service.

Priority for user location inputs:
  1. In-process dict cache (instant)
  2. DB lookup for "City, ST" format (instant, no HTTP)
  3. Nominatim fallback (HTTP, ~1-2s)

No artificial rate-limit lock here — that is only needed for bulk management
commands, which use their own sync requests.Session with explicit sleeps.
"""

import logging
import re
from typing import Dict, Optional, Tuple

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None
_cache: Dict[str, Optional[Tuple[float, float]]] = {}

_CITY_STATE_RE = re.compile(r"^(?P<city>[^,]+),\s*(?P<state>[A-Za-z]{2})\s*$")
_COORDS_RE = re.compile(r"^\s*(?P<lat>-?\d+\.?\d*)\s*,\s*(?P<lng>-?\d+\.?\d*)\s*$")


def _parse_coords(location: str) -> Optional[Tuple[float, float]]:
    """Return (lat, lng) if the string is a raw coordinate pair, else None."""
    m = _COORDS_RE.match(location)
    if not m:
        return None
    lat, lng = float(m.group("lat")), float(m.group("lng"))
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return (lat, lng)
    return None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT}
        )
    return _session


async def _lookup_city_db(city: str, state: str) -> Optional[Tuple[float, float]]:
    """Query the local DB for average lat/lng of a city — zero network calls."""
    from api.models import FuelStation
    from django.db.models import Avg

    def _q():
        return FuelStation.objects.filter(
            city__iexact=city, state__iexact=state, geocoded=True
        ).aggregate(lat=Avg("latitude"), lng=Avg("longitude"))

    result = await sync_to_async(_q)()
    lat, lng = result.get("lat"), result.get("lng")
    if lat and lng:
        return (float(lat), float(lng))
    return None


async def _nominatim(query: str) -> Optional[Tuple[float, float]]:
    try:
        async with _get_session().get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data:
                return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as exc:
        logger.warning("Nominatim error for %s: %s", query, exc)
    return None


async def geocode_user_location(location: str) -> Optional[Tuple[float, float]]:
    key = location.lower().strip()
    if key in _cache:
        return _cache[key]

    # Raw coordinates — skip all geocoding
    coords = _parse_coords(location)

    if not coords:
        m = _CITY_STATE_RE.match(location.strip())
        if m:
            coords = await _lookup_city_db(
                m.group("city").strip(),
                m.group("state").strip().upper(),
            )

    if not coords:
        coords = await _nominatim(f"{location}, USA")

    _cache[key] = coords
    return coords


async def geocode_city_state(city: str, state: str) -> Optional[Tuple[float, float]]:
    return await geocode_user_location(f"{city}, {state}")
