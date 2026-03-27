"""
Geocoding (Google Maps or Nominatim fallback) and routing (OSRM).
"""

import os
from datetime import datetime, timedelta

import httpx

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
OSRM = "https://router.project-osrm.org/route/v1/driving"

HEADERS = {"User-Agent": "SimpleTruckingETA/1.0"}


def _geocode_google(address: str) -> dict | None:
    """Geocode using Google Maps API."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None
    resp = httpx.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "key": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "OK" and data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lon": loc["lng"]}
    return None


def geocode(address: str, _depth: int = 0) -> dict | None:
    """Convert address string to {lat, lon}.
    Uses Google Maps if GOOGLE_MAPS_API_KEY is set, otherwise Nominatim with fallback.
    """
    if _depth > 3 or not address.strip():
        return None

    # Try Google Maps first (much more accurate)
    if _depth == 0:
        result = _geocode_google(address)
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
        return geocode(parts[1], _depth + 1)
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


def get_route(origin: dict, destination: dict) -> dict:
    """
    Calculate route between two {lat, lon} points.
    Returns {duration_seconds, distance_meters}.
    """
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


def calculate_eta(origin: dict, destination: dict, buffer_hours: float = 0) -> dict:
    """
    Full ETA calculation from origin {lat,lon} to destination {lat,lon}.
    Returns {eta_utc, duration_minutes, distance_miles, buffer_hours}.
    """
    route = get_route(origin, destination)
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
