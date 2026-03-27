import httpx

BASE = "https://api.keeptrucking.com/v2"


class MotiveClient:
    def __init__(self, api_key: str):
        self.headers = {"X-Api-Key": api_key}

    def get_drivers(self) -> list[dict]:
        """Returns list of active drivers: [{id, name}]"""
        resp = httpx.get(
            f"{BASE}/users",
            headers=self.headers,
            params={"role": "driver", "status": "active", "per_page": 100},
            timeout=10,
        )
        resp.raise_for_status()
        users = resp.json().get("users", [])
        return [
            {"id": str(u["id"]), "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()}
            for u in users
        ]

    def get_driver_location(self, driver_id: str) -> dict | None:
        """Returns {lat, lon, speed_mph} for a driver via their assigned vehicle."""
        resp = httpx.get(
            f"{BASE}/vehicles/current_locations",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        vehicles = resp.json().get("vehicles", [])
        for v in vehicles:
            driver = v.get("current_driver") or {}
            if str(driver.get("id", "")) == str(driver_id):
                loc = v.get("current_location") or {}
                lat = loc.get("lat")
                lon = loc.get("lon")
                if lat is not None and lon is not None:
                    speed_kmh = loc.get("speed") or 0
                    return {
                        "lat": lat,
                        "lon": lon,
                        "speed_mph": round(speed_kmh * 0.621371, 1),
                    }
        return None
