import httpx

BASE = "https://api.samsara.com"


class SamsaraClient:
    def __init__(self, api_key: str):
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def get_drivers(self) -> list[dict]:
        """Returns list of active drivers: [{id, name}]"""
        resp = httpx.get(
            f"{BASE}/fleet/drivers",
            headers=self.headers,
            params={"driverActivationStatus": "active"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [{"id": d["id"], "name": d["name"]} for d in data]

    def get_driver_location(self, driver_id: str) -> dict | None:
        """Returns {lat, lon, speed_mph} for a driver's current vehicle location."""
        resp = httpx.get(
            f"{BASE}/fleet/vehicles/locations",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        vehicles = resp.json().get("data", [])
        for v in vehicles:
            assigned = v.get("driver") or {}
            if str(assigned.get("id", "")) == str(driver_id):
                loc = v.get("location", {})
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                if lat is not None and lon is not None:
                    return {
                        "lat": lat,
                        "lon": lon,
                        "speed_mph": loc.get("speedMilesPerHour") or 0,
                    }
        return None
