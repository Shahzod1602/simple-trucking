import httpx
from datetime import datetime, timedelta, timezone

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

    def _get_latest_speed(self, vehicle_id: str) -> float | None:
        """Fetch latest speed from Trackings API (last 15 min)."""
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to_ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            resp = httpx.get(
                f"https://read.zippyeld.com/api/externalservice/trackings/{self.usdot}/{vehicle_id}/",
                headers=self.headers,
                params={"from": from_ts, "to": to_ts},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            if data and isinstance(data, list):
                return data[-1].get("speed")
        except Exception:
            pass
        return None

    def get_trucks(self) -> list[dict]:
        """Returns list of trucks with driver/codriver assignment and real-time location."""
        assignments_resp = httpx.get(
            f"https://read.zippyeld.com/api/externalservice/current-units/{self.usdot}",
            headers=self.headers,
            params={"is_active": "true", "perPage": 100},
            timeout=10,
        )
        assignments_resp.raise_for_status()
        units = assignments_resp.json().get("data", [])

        loc_resp = httpx.get(
            f"{BASE}/units-by-usdot/{self.usdot}",
            headers=self.headers,
            timeout=10,
        )
        loc_resp.raise_for_status()
        loc_by_vin = {u["vin"]: u for u in loc_resp.json().get("units", []) if u.get("vin")}

        result = []
        for u in units:
            vin = u.get("vin") or ""
            vehicle_id = u.get("id") or vin
            d = u.get("driver") or {}
            cd = u.get("codriver") or {}
            driver_name = f"{d.get('first_name', '')} {d.get('second_name', '')}".strip()
            codriver_name = f"{cd.get('first_name', '')} {cd.get('second_name', '')}".strip()
            loc = loc_by_vin.get(vin, {})
            coords = loc.get("coordinates") or {}
            # M10: avoid an N+1 blocking HTTP call per truck. Read speed from the
            # bulk units-by-usdot coordinates payload already fetched above instead
            # of a per-truck Trackings request (None when the payload omits speed).
            speed = coords.get("speed")
            result.append({
                "id": vehicle_id,
                "truck_number": u.get("truck_number") or vin,
                "vin": vin,
                "driver": {"id": str(d["id"]), "name": driver_name} if d.get("id") else None,
                "codriver": {"id": str(cd["id"]), "name": codriver_name} if cd.get("id") else None,
                "lat": float(coords["lat"]) if coords.get("lat") is not None else None,
                "lon": float(coords["lng"]) if coords.get("lng") is not None else None,
                "speed_mph": float(speed) if speed is not None else None,
                "timestamp": loc.get("timestamp"),
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
            result = {"lat": float(lat), "lon": float(lon)}
            speed = coords.get("speed")
            if speed is not None:
                result["speed_mph"] = float(speed)
            return result
        return None
