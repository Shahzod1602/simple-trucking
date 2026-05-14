import json
import logging

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.keeptruckin.com/v2"
BASE_V1 = "https://api.keeptruckin.com/v1"


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

    def get_trucks(self) -> list[dict]:
        """Returns list of trucks with driver assignment and real-time location."""
        resp = httpx.get(
            f"{BASE}/vehicles/current_locations",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        vehicles = resp.json().get("vehicles", [])
        result = []
        for v in vehicles:
            loc = v.get("current_location") or {}
            driver = v.get("current_driver") or {}
            truck = v.get("vehicle") or {}
            speed_kmh = loc.get("speed") or 0
            driver_name = f"{driver.get('first_name', '')} {driver.get('last_name', '')}".strip()
            result.append({
                "id": str(truck.get("id", "")),
                "truck_number": truck.get("number") or truck.get("name") or str(truck.get("id", "")),
                "vin": truck.get("vin") or "",
                "driver": {"id": str(driver["id"]), "name": driver_name} if driver.get("id") else None,
                "codriver": None,
                "lat": loc.get("lat"),
                "lon": loc.get("lon"),
                "speed_mph": round(speed_kmh * 0.621371, 1) if speed_kmh else None,
                "timestamp": loc.get("recorded_at"),
            })
        return result

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

    # ------------------------------------------------------------------ #
    #  Dispatch Location
    # ------------------------------------------------------------------ #

    def create_dispatch_location(
        self, name: str, address: str, city: str, state: str, zip_code: str,
    ) -> dict:
        """Motive'da yangi manzil yaratish."""
        resp = httpx.post(
            f"{BASE_V1}/dispatch_locations",
            headers=self.headers,
            json={
                "name": name,
                "address_line_1": address,
                "city": city,
                "state": state,
                "zip": zip_code,
                "country": "US",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("dispatch_location", {})

    # ------------------------------------------------------------------ #
    #  Push dispatch (RateCon → Motive)
    # ------------------------------------------------------------------ #

    def push_dispatch(
        self, load: dict, driver_id: str, vehicle_id: str,
    ) -> dict:
        """RateCon yukini Motive'ga yuborish. Haydovchi Motive ilovasida ko'radi."""
        load_number = load.get("load_number") or str(load["id"])

        stops = []
        # Pickup stop
        stops.append({
            "vendor_id": f"{load_number}-PU",
            "type": "pickup",
            "number": 1,
            "early_date": load.get("pickup_date"),
            "vendor_dispatch_location_id": f"LOC-{load['id']}-PU",
            "status": "available",
        })

        # Oraliq stoplar (stops_json dan)
        extra_stops = []
        if load.get("stops_json"):
            try:
                extra_stops = json.loads(load["stops_json"]) if isinstance(load["stops_json"], str) else load["stops_json"]
            except (json.JSONDecodeError, TypeError):
                pass

        for i, st in enumerate(extra_stops, start=2):
            stop_type = "pickup" if st.get("type", "").upper() in ("PU", "PICKUP") else "dropoff"
            stops.append({
                "vendor_id": f"{load_number}-STOP-{i}",
                "type": stop_type,
                "number": i,
                "vendor_dispatch_location_id": f"LOC-{load['id']}-STOP-{i}",
                "status": "available",
                "comments": st.get("notes") or st.get("comments") or "",
            })

        # Delivery stop (oxirgi)
        delivery_number = len(stops) + 1
        stops.append({
            "vendor_id": f"{load_number}-DEL",
            "type": "dropoff",
            "number": delivery_number,
            "early_date": load.get("delivery_date"),
            "vendor_dispatch_location_id": f"LOC-{load['id']}-DEL",
            "status": "available",
        })

        payload = {
            "vendor_id": load_number,
            "status": "planned",
            "loaded_miles": int(float(load["miles"])) if load.get("miles") else None,
            "dispatch_stops": stops,
            "dispatch_trips": [
                {
                    "vendor_id": f"{load_number}-TRIP",
                    "driver_id": int(driver_id),
                    "vehicle_id": int(vehicle_id),
                    "vendor_stop_ids": [s["vendor_id"] for s in stops],
                    "status": "not_started",
                }
            ],
        }

        logger.info("Pushing dispatch to Motive: vendor_id=%s", load_number)
        resp = httpx.post(
            f"{BASE}/dispatches",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("dispatch", {})

    # ------------------------------------------------------------------ #
    #  Import dispatches (Motive → RateCon)
    # ------------------------------------------------------------------ #

    def get_dispatches(
        self, status: str | None = None, per_page: int = 100, page_no: int = 1,
    ) -> dict:
        """Motive'dan dispatchlar ro'yxatini olish. pagination bilan."""
        params: dict = {"per_page": per_page, "page_no": page_no}
        if status:
            params["status"] = status
        resp = httpx.get(
            f"{BASE}/dispatches",
            headers=self.headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "dispatches": data.get("dispatches", []),
            "pagination": data.get("pagination", {}),
        }

    def get_dispatch(self, dispatch_id: int) -> dict | None:
        """Bitta dispatch ma'lumotini olish."""
        resp = httpx.get(
            f"{BASE}/dispatches/{dispatch_id}",
            headers=self.headers,
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("dispatch")

    @staticmethod
    def parse_dispatch_to_load(dispatch: dict) -> dict:
        """Motive dispatch ob'yektini RateCon load formatiga o'girish."""
        stops = sorted(dispatch.get("dispatch_stops", []), key=lambda s: s.get("number", 0))

        pickup_stop = next((s for s in stops if s.get("type") in ("pickup", "PU")), None)
        delivery_stop = next(
            (s for s in stops if s.get("type") in ("dropoff", "UL", "delivery")),
            stops[-1] if stops else None,
        )

        def _format_address(loc: dict) -> str:
            if not loc:
                return ""
            parts = [
                loc.get("address_line_1", ""),
                loc.get("city", ""),
                f"{loc.get('state', '')} {loc.get('zip', '')}".strip(),
            ]
            return ", ".join(p for p in parts if p)

        pickup_loc = (pickup_stop or {}).get("dispatch_location") or {}
        delivery_loc = (delivery_stop or {}).get("dispatch_location") or {}

        # Oraliq stoplar (pickup va delivery orasidagilar)
        middle_stops = [s for s in stops if s not in (pickup_stop, delivery_stop)]
        stops_json_list = []
        for s in middle_stops:
            loc = s.get("dispatch_location") or {}
            stops_json_list.append({
                "type": "PU" if s.get("type") in ("pickup", "PU") else "DEL",
                "address": _format_address(loc),
                "city": loc.get("city", ""),
                "state": loc.get("state", ""),
                "notes": s.get("comments", ""),
            })

        # driver_id olish
        trips = dispatch.get("dispatch_trips", [])
        driver_id = str(trips[0]["driver_id"]) if trips and trips[0].get("driver_id") else None

        return {
            "load_number": dispatch.get("vendor_id") or str(dispatch.get("id", "")),
            "pickup_address": _format_address(pickup_loc),
            "pickup_date": (pickup_stop or {}).get("early_date"),
            "delivery_address": _format_address(delivery_loc),
            "delivery_date": (delivery_stop or {}).get("early_date"),
            "origin_state": pickup_loc.get("state"),
            "destination_state": delivery_loc.get("state"),
            "miles": str(dispatch.get("loaded_miles", "")) if dispatch.get("loaded_miles") else None,
            "stops_json": json.dumps(stops_json_list) if stops_json_list else None,
            "motive_dispatch_id": dispatch.get("id"),
            "eld_driver_id": f"motive:{driver_id}" if driver_id else None,
        }

    # ------------------------------------------------------------------ #
    #  Update dispatch status
    # ------------------------------------------------------------------ #

    STATUS_MAP = {
        "upcoming": "planned",
        "dispatched": "active",
        "delivered": "completed",
        "cancelled": "cancelled",
    }

    REVERSE_STATUS_MAP = {
        "planned": "upcoming",
        "active": "dispatched",
        "completed": "delivered",
        "cancelled": "cancelled",
    }

    def update_dispatch_status(self, dispatch_id: int, new_status: str) -> dict:
        """Motive'da dispatch statusini yangilash.
        new_status: planned, active, completed, cancelled
        """
        # Avval to'liq ob'yektni olish (PUT partial emas)
        current = self.get_dispatch(dispatch_id)
        if not current:
            raise ValueError(f"Dispatch {dispatch_id} not found in Motive")

        current["status"] = new_status
        resp = httpx.put(
            f"{BASE}/dispatches",
            headers=self.headers,
            json=current,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("dispatch", {})
