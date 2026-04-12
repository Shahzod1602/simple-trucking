"""
Geocoding (Google Maps or Nominatim fallback) and routing (Google Maps or OSRM fallback).
"""

import os
from datetime import datetime, timedelta

import httpx

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
OSRM = "https://router.project-osrm.org/route/v1/driving"

HEADERS = {"User-Agent": "SimpleTruckingETA/1.0"}


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


def geocode(address: str, api_key: str | None = None, _depth: int = 0) -> dict | None:
    """Convert address string to {lat, lon}.
    Uses Google Maps if api_key or GOOGLE_MAPS_API_KEY is set, otherwise Nominatim with fallback.
    """
    if _depth > 3 or not address.strip():
        return None

    # Try Google Maps first (much more accurate)
    if _depth == 0:
        result = _geocode_google(address, api_key)
        if result:
            return result

    # Nominatim fallback
    resp = httpx.get(
        NOMINATIM,
        params={"q": address, "format": "json", "limit": 1},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if results:
        return {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}

    # Strip first token and retry
    parts = address.split(", ", 1)
    if len(parts) > 1:
        return geocode(parts[1], api_key, _depth + 1)
    return None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Convert lat/lon to a short human-readable address."""
    resp = httpx.get(
        NOMINATIM_REVERSE,
        params={"lat": lat, "lon": lon, "format": "json"},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    addr = data.get("address", {})
    parts = [
        (addr.get("house_number", "") + " " + addr.get("road", "")).strip(),
        addr.get("city") or addr.get("town") or addr.get("village") or "",
        addr.get("state", ""),
        addr.get("postcode", ""),
        addr.get("country", ""),
    ]
    return ", ".join(p for p in parts if p) or data.get("display_name")


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


def get_route(origin: dict, destination: dict, api_key: str | None = None) -> dict:
    """
    Calculate route between two {lat, lon} points.
    Uses Google Maps Distance Matrix if key available, otherwise OSRM.
    Returns {duration_seconds, distance_meters}.
    """
    key = _get_gmaps_key(api_key)
    if key:
        result = _get_route_google(origin, destination, key)
        if result:
            return result

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

    eta_utc = datetime.utcnow() + timedelta(seconds=total_sec)
    distance_miles = round(route["distance_meters"] / 1609.34, 1)

    return {
        "eta_utc": eta_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eta_display": eta_utc.strftime("%b %d, %I:%M %p UTC"),
        "duration_minutes": round(duration_sec / 60),
        "distance_miles": distance_miles,
        "buffer_hours": buffer_hours,
    }
