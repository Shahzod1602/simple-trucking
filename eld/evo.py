import httpx

BASE = "https://read.evoeld.com/api/v2"


class EvoClient:
    def __init__(self, api_key: str, provider_token: str, usdot: str):
        self.headers = {
            "x-api-key": api_key,
            "provider-token": provider_token,
        }
        self.usdot = usdot

    def get_drivers(self) -> list[dict]:
        """Returns list of units as drivers: [{id, name}]"""
        resp = httpx.get(
            f"{BASE}/units-by-usdot/{self.usdot}",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        units = resp.json().get("units", [])
        return [
            {"id": u["vin"], "name": u.get("truck_number") or u["vin"]}
            for u in units
            if u.get("vin")
        ]

    def get_trucks(self) -> list[dict]:
        """Returns list of trucks with real-time location."""
        resp = httpx.get(
            f"{BASE}/units-by-usdot/{self.usdot}",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        units = resp.json().get("units", [])
        result = []
        for u in units:
            vin = u.get("vin") or ""
            coords = u.get("coordinates") or {}
            result.append({
                "id": vin,
                "truck_number": u.get("truck_number") or vin,
                "vin": vin,
                "driver": None,
                "codriver": None,
                "lat": float(coords["lat"]) if coords.get("lat") is not None else None,
                "lon": float(coords["lng"]) if coords.get("lng") is not None else None,
                "speed_mph": None,
                "timestamp": u.get("timestamp"),
            })
        return result

    def get_driver_location(self, driver_id: str) -> dict | None:
        """Returns {lat, lon} for a unit by VIN."""
        resp = httpx.get(
            f"{BASE}/unit-by-vin/{self.usdot}/{driver_id}",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        unit = resp.json().get("unit", {})
        coords = unit.get("coordinates", {})
        lat = coords.get("lat")
        lon = coords.get("lng")
        if lat is not None and lon is not None:
            return {"lat": float(lat), "lon": float(lon)}
        return None
