import httpx

BASE = "https://read.zippyeld.com/api/v2"


class ZippyClient:
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
            result = {"lat": float(lat), "lon": float(lon)}
            speed = coords.get("speed")
            if speed is not None:
                result["speed_mph"] = float(speed)
            return result
        return None
