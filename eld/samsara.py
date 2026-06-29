import httpx

BASE = "https://api.samsara.com"


class SamsaraClient:
    def __init__(self, api_key: str):
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def get_drivers(self) -> list[dict]:
        """Returns list of active drivers: [{id, name}]"""
        result: list[dict] = []
        after: str | None = None
        # M11: follow Samsara cursor pagination (endCursor/hasNextPage) so large
        # fleets are not truncated to the first page; cap pages to avoid loops.
        for _ in range(50):
            params: dict = {"driverActivationStatus": "active"}
            if after:
                params["after"] = after
            resp = httpx.get(
                f"{BASE}/fleet/drivers",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            result.extend({"id": d["id"], "name": d["name"]} for d in body.get("data", []))
            pagination = body.get("pagination") or {}
            if not pagination.get("hasNextPage"):
                break
            after = pagination.get("endCursor")
            if not after:
                break
        return result

    def get_trucks(self) -> list[dict]:
        """Returns list of trucks with driver assignment and real-time location."""
        result = []
        after: str | None = None
        # M11: follow Samsara cursor pagination so large fleets are not truncated.
        for _ in range(50):
            params: dict = {}
            if after:
                params["after"] = after
            resp = httpx.get(
                f"{BASE}/fleet/vehicles/locations",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            vehicles = body.get("data", [])
            for v in vehicles:
                loc = v.get("location") or {}
                driver = v.get("driver") or {}
                result.append({
                    "id": str(v.get("id", "")),
                    "truck_number": v.get("name") or str(v.get("id", "")),
                    "vin": v.get("vin") or "",
                    "driver": {"id": str(driver["id"]), "name": driver.get("name", "")} if driver.get("id") else None,
                    "codriver": None,
                    "lat": loc.get("latitude"),
                    "lon": loc.get("longitude"),
                    "speed_mph": loc.get("speedMilesPerHour"),
                    "timestamp": loc.get("time"),
                })
            pagination = body.get("pagination") or {}
            if not pagination.get("hasNextPage"):
                break
            after = pagination.get("endCursor")
            if not after:
                break
        return result

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
