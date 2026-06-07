"""
Geocoding (Google Maps or Nominatim fallback) and routing (Google Maps or OSRM fallback).
"""

import logging
import math
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
OSRM = "https://router.project-osrm.org/route/v1/driving"

HEADERS = {"User-Agent": "SimpleTruckingETA/1.0"}

EARTH_RADIUS_MILES = 3958.7613

# Geocoded coordinates are stable, so successful lookups are memoized in-process.
# This cuts repeated Google/Nominatim cost (the same warehouse addresses are
# geocoded over and over) and keeps us within Nominatim's ~1 req/s usage policy.
_GEOCODE_CACHE: dict[str, dict] = {}
_REVERSE_CACHE: dict[tuple, str] = {}
_CACHE_MAX = 5000


def _cache_put(cache: dict, key, value) -> None:
    if len(cache) >= _CACHE_MAX:
        # Cheap eviction: drop a quarter of the entries (no ordering dependency).
        for k in list(cache.keys())[: _CACHE_MAX // 4]:
            cache.pop(k, None)
    cache[key] = value


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle ('as the crow flies') distance in miles between two points.

    Used to pre-filter nearby trucks without any API call before refining the
    top candidates with real driving distance via get_route().
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return EARTH_RADIUS_MILES * 2 * math.asin(min(1.0, math.sqrt(a)))


def _get_gmaps_key(api_key: str | None = None) -> str:
    return (api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")).strip()


def _geocode_google(address: str, api_key: str | None = None) -> dict | None:
    """Geocode using Google Maps API."""
    key = _get_gmaps_key(api_key)
    if not key:
        return None
    resp = httpx.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "key": key},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "OK" and data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lon": loc["lng"]}
    return None


def _geocode_impl(address: str, api_key: str | None, depth: int) -> dict | None:
    if depth > 3 or not address.strip():
        return None

    # Try Google Maps first (much more accurate). A Google error (429/5xx) must
    # NOT abort the whole geocode — fall through to Nominatim instead.
    if depth == 0:
        try:
            result = _geocode_google(address, api_key)
            if result:
                return result
        except Exception as e:
            logger.warning("Google geocode error, falling back to Nominatim: %s", e)

    # Nominatim fallback
    try:
        resp = httpx.get(
            NOMINATIM,
            params={"q": address, "format": "json", "limit": 1},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        logger.warning("Nominatim geocode error: %s", e)
        results = None

    if results:
        return {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}

    # Strip first token and retry
    parts = address.split(", ", 1)
    if len(parts) > 1:
        return _geocode_impl(parts[1], api_key, depth + 1)
    return None


def geocode(address: str, api_key: str | None = None, _depth: int = 0) -> dict | None:
    """Convert an address string to {lat, lon}. Uses Google Maps if a key is
    available, otherwise Nominatim, with a stripped-address retry. Results are
    cached since coordinates don't change."""
    key = (address or "").strip().lower()
    if not key:
        return None
    cached = _GEOCODE_CACHE.get(key)
    if cached is not None:
        return cached
    result = _geocode_impl(address, api_key, 0)
    if result:
        _cache_put(_GEOCODE_CACHE, key, result)
    return result


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Convert lat/lon to a short human-readable address (cached ~11m grid)."""
    rkey = (round(lat, 4), round(lon, 4))
    cached = _REVERSE_CACHE.get(rkey)
    if cached is not None:
        return cached
    try:
        resp = httpx.get(
            NOMINATIM_REVERSE,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Reverse geocode error: %s", e)
        return None
    addr = data.get("address", {})
    parts = [
        (addr.get("house_number", "") + " " + addr.get("road", "")).strip(),
        addr.get("city") or addr.get("town") or addr.get("village") or "",
        addr.get("state", ""),
        addr.get("postcode", ""),
        addr.get("country", ""),
    ]
    result = ", ".join(p for p in parts if p) or data.get("display_name")
    if result:
        _cache_put(_REVERSE_CACHE, rkey, result)
    return result


def _get_route_google(origin: dict, destination: dict, api_key: str) -> dict | None:
    """Calculate route using Google Maps Distance Matrix API."""
    resp = httpx.get(
        "https://maps.googleapis.com/maps/api/distancematrix/json",
        params={
            "origins": f"{origin['lat']},{origin['lon']}",
            "destinations": f"{destination['lat']},{destination['lon']}",
            "mode": "driving",
            "key": api_key,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            return None
        return {
            "duration_seconds": int(element["duration"]["value"]),
            "distance_meters": int(element["distance"]["value"]),
        }
    except (KeyError, IndexError):
        return None


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google Maps encoded polyline string into [(lat, lon), ...]."""
    index = 0
    lat = 0
    lon = 0
    coordinates = []
    while index < len(encoded):
        for unit in ["latitude", "longitude"]:
            shift = 0
            result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if not (byte & 0x20):
                    break
            if result & 1:
                change = ~(result >> 1)
            else:
                change = result >> 1
            if unit == "latitude":
                lat += change
            else:
                lon += change
        coordinates.append((lat / 1e5, lon / 1e5))
    return coordinates


def get_route_geometry(
    origin: dict, destination: dict, api_key: str | None = None
) -> list[tuple[float, float]] | None:
    """
    Return the actual driving route geometry between two {lat, lon} points
    as a list of (lat, lon) tuples. Uses Google Directions if a key is
    available, otherwise OSRM. A Google error falls back to OSRM.
    """
    key = _get_gmaps_key(api_key)
    if key:
        try:
            resp = httpx.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": f"{origin['lat']},{origin['lon']}",
                    "destination": f"{destination['lat']},{destination['lon']}",
                    "mode": "driving",
                    "key": key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "OK" and data.get("routes"):
                encoded = data["routes"][0]["overview_polyline"]["points"]
                return decode_polyline(encoded)
        except Exception as e:
            logger.warning("Google directions error, falling back to OSRM: %s", e)

    # OSRM fallback
    coords = f"{origin['lon']},{origin['lat']};{destination['lon']},{destination['lat']}"
    resp = httpx.get(
        f"{OSRM}/{coords}",
        params={"overview": "full", "geometries": "polyline"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    encoded = data["routes"][0]["geometry"]
    return decode_polyline(encoded)


def get_route(origin: dict, destination: dict, api_key: str | None = None) -> dict:
    """
    Calculate route between two {lat, lon} points.
    Uses Google Maps Distance Matrix if key available, otherwise OSRM.
    A Google error falls back to OSRM. Returns {duration_seconds, distance_meters}.
    """
    key = _get_gmaps_key(api_key)
    if key:
        try:
            result = _get_route_google(origin, destination, key)
            if result:
                return result
        except Exception as e:
            logger.warning("Google route error, falling back to OSRM: %s", e)

    # OSRM fallback
    coords = f"{origin['lon']},{origin['lat']};{destination['lon']},{destination['lat']}"
    resp = httpx.get(
        f"{OSRM}/{coords}",
        params={"overview": "false"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("No route found")
    route = data["routes"][0]
    return {
        "duration_seconds": int(route["duration"]),
        "distance_meters": int(route["distance"]),
    }


def calculate_total_miles(addresses: list[str], api_key: str | None = None) -> float | None:
    """
    Geocode a list of stop addresses and return total driving miles across
    consecutive legs (pickup -> ... -> delivery). Returns None if any stop
    fails to geocode or any leg fails to route.
    """
    if not addresses or len(addresses) < 2:
        return None

    points: list[dict] = []
    for addr in addresses:
        geo = geocode(addr, api_key)
        if not geo:
            return None
        points.append(geo)

    total_meters = 0
    for i in range(len(points) - 1):
        try:
            route = get_route(points[i], points[i + 1], api_key)
        except Exception:
            return None
        total_meters += route["distance_meters"]

    return round(total_meters / 1609.34, 1)


def calculate_eta(origin: dict, destination: dict, buffer_hours: float = 0, api_key: str | None = None) -> dict:
    """
    Full ETA calculation from origin {lat,lon} to destination {lat,lon}.
    Returns {eta_utc, duration_minutes, distance_miles, buffer_hours}.
    """
    route = get_route(origin, destination, api_key)
    duration_sec = route["duration_seconds"]
    buffer_sec = int(buffer_hours * 3600)
    total_sec = duration_sec + buffer_sec

    eta_utc = datetime.now(timezone.utc) + timedelta(seconds=total_sec)
    distance_miles = round(route["distance_meters"] / 1609.34, 1)

    return {
        "eta_utc": eta_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eta_display": eta_utc.strftime("%b %d, %I:%M %p UTC"),
        "duration_minutes": round(duration_sec / 60),
        "distance_miles": distance_miles,
        "buffer_hours": buffer_hours,
    }
