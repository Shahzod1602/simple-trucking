# Motive Dispatch Integration

> RateCon tizimida Motive (KeepTrucking) orqali yuklarni import va push qilish bo'yicha qo'llanma.

---

## Mundarija

- [Kirish](#kirish)
- [API konfiguratsiya](#api-konfiguratsiya)
- [Yukni push qilish (RateCon → Motive)](#yukni-push-qilish-ratecon--motive)
- [Yukni import qilish (Motive → RateCon)](#yukni-import-qilish-motive--ratecon)
- [Status sinxronizatsiyasi](#status-sinxronizatsiyasi)
- [Manzil formatlash](#manzil-formatlash)
- [Amalga oshirish rejasi](#amalga-oshirish-rejasi)
- [Cheklovlar va yechimlar](#cheklovlar-va-yechimlar)

---

## Kirish

**Motive** (avvalgi nomi KeepTrucking) — yuk mashinalarini boshqarish va ELD platformasi. Hozirgi `eld/motive.py` faqat haydovchi lokatsiyasi va mashina ma'lumotlarini oladi. Bu hujjat yuklar (dispatch) bilan ishlash integratsiyasini tavsiflaydi.

**Motive'da yuk = `dispatch`**. Barcha load operatsiyalari dispatch endpoint orqali amalga oshiriladi.

### Hozirgi va rejalashtirilgan funksiyalar

| Funksiya | Holati |
|----------|--------|
| Haydovchi lokatsiyasi | Tayyor |
| Mashina ro'yxati | Tayyor |
| Haydovchilar ro'yxati | Tayyor |
| Yukni Motive'ga push qilish | Rejalashtirilgan |
| Yukni Motive'dan import qilish | Rejalashtirilgan |
| Status sinxronizatsiyasi | Rejalashtirilgan |

---

## API konfiguratsiya

| Parametr | Qiymat |
|----------|--------|
| Base URL | `https://api.gomotive.com` |
| Legacy URL | `https://api.keeptruckin.com` (hali ishlaydi) |
| Autentifikatsiya | `X-Api-Key` header |
| Tavsiya etilgan versiya | **v2** |
| Rate limit | 20 so'rov/soniya |

API kalitni Motive Dashboard → Developer Settings dan olish mumkin.

---

## Yukni push qilish (RateCon → Motive)

Dispetcher RateCon'da yuk yaratib, haydovchiga tayinlaganda — uni Motive'ga yuborish mumkin. Haydovchi Motive ilovasida yukni ko'radi.

### Endpoint

```
POST https://api.gomotive.com/v2/dispatches
Header: X-Api-Key: <api_key>
```

### Request namunasi

```json
{
  "vendor_id": "RC-12345",
  "status": "planned",
  "loaded_miles": 450,
  "dispatch_stops": [
    {
      "vendor_id": "STOP-1",
      "type": "pickup",
      "number": 1,
      "early_date": "2026-04-17T08:00:00-05:00",
      "late_date": "2026-04-17T14:00:00-05:00",
      "vendor_dispatch_location_id": "LOC-PU-1",
      "status": "available",
      "comments": "Call before arrival"
    },
    {
      "vendor_id": "STOP-2",
      "type": "dropoff",
      "number": 2,
      "early_date": "2026-04-18T08:00:00-05:00",
      "late_date": "2026-04-18T18:00:00-05:00",
      "vendor_dispatch_location_id": "LOC-DEL-1",
      "status": "available"
    }
  ],
  "dispatch_trips": [
    {
      "vendor_id": "TRIP-1",
      "vehicle_id": 64734,
      "driver_id": 1088505,
      "vendor_stop_ids": ["STOP-1", "STOP-2"],
      "status": "not_started"
    }
  ]
}
```

### Response

```json
{
  "dispatch": {
    "id": 98765,
    "vendor_id": "RC-12345",
    "status": "planned",
    "dispatch_stops": [...],
    "dispatch_trips": [...]
  }
}
```

> Qaytgan `dispatch.id` ni bazada saqlash kerak — keyingi update va sync uchun.

### Maydonlar xaritasi: RateCon → Motive

| RateCon (loads jadvali) | Motive dispatch maydoni |
|-------------------------|------------------------|
| `load_number` | `vendor_id` |
| `pickup_address` | `dispatch_stops[0]` → location |
| `pickup_date` | `dispatch_stops[0].early_date` |
| `delivery_address` | `dispatch_stops[-1]` → location |
| `delivery_date` | `dispatch_stops[-1].early_date` |
| `stops_json` | `dispatch_stops[]` (oraliq stoplar) |
| `miles` | `loaded_miles` |
| `status = dispatched` | `status = "planned"` yoki `"active"` |
| `status = delivered` | `status = "completed"` |
| `group.eld_driver_id` | `dispatch_trips[0].driver_id` |

### Stop location yaratish

Har bir stop uchun **dispatch_location** kerak. Avval lokatsiyani yaratib, keyin stopga bog'lash:

```
POST https://api.gomotive.com/v1/dispatch_locations
```

```json
{
  "name": "ABC Warehouse",
  "address_line_1": "123 Main St",
  "city": "Dallas",
  "state": "TX",
  "zip": "75201",
  "country": "US"
}
```

### Push jarayoni qadam-baqadam

```
1. RateCon'da yuk yaratiladi + haydovchi tayinlanadi
2. Dispetcher "Motive'ga yuborish" tugmasini bosadi
3. Backend:
   a. Haydovchining Motive driver_id va vehicle_id ni olish
   b. Pickup/delivery manzillarini parse qilish
   c. dispatch_location yaratish (agar mavjud bo'lmasa)
   d. POST /v2/dispatches yuborish
   e. Qaytgan dispatch.id ni loads.motive_dispatch_id ga saqlash
4. Haydovchi Motive ilovasida yukni ko'radi
```

---

## Yukni import qilish (Motive → RateCon)

Motive'da mavjud yuklarni RateCon'ga tortib olish.

### Endpoint

```
GET https://api.gomotive.com/v2/dispatches
Header: X-Api-Key: <api_key>
```

### Query parametrlari

| Parametr | Tavsif |
|----------|--------|
| `page_no` | Sahifa raqami |
| `per_page` | Har sahifada nechta (max 100) |
| `driver_id` | Haydovchi bo'yicha filter |
| `vehicle_id` | Mashina bo'yicha filter |
| `status` | `planned`, `active`, `completed`, `cancelled` |

### Response namunasi

```json
{
  "dispatches": [
    {
      "id": 98765,
      "vendor_id": "RC-12345",
      "status": "active",
      "dispatch_stops": [
        {
          "id": 111,
          "type": "pickup",
          "number": 1,
          "dispatch_location": {
            "name": "ABC Warehouse",
            "address_line_1": "123 Main St",
            "city": "Dallas",
            "state": "TX",
            "zip": "75201"
          },
          "early_date": "2026-04-17T08:00:00-05:00",
          "late_date": "2026-04-17T14:00:00-05:00",
          "arrived_at": null,
          "departed_at": null
        },
        {
          "id": 112,
          "type": "dropoff",
          "number": 2,
          "dispatch_location": {
            "name": "XYZ Distribution",
            "address_line_1": "456 Oak Ave",
            "city": "Houston",
            "state": "TX",
            "zip": "77001"
          },
          "early_date": "2026-04-18T08:00:00-05:00"
        }
      ],
      "dispatch_trips": [
        {
          "vehicle_id": 64734,
          "driver_id": 1088505,
          "status": "not_started"
        }
      ]
    }
  ],
  "pagination": {
    "per_page": 25,
    "page_no": 1,
    "total": 42
  }
}
```

### Maydonlar xaritasi: Motive → RateCon

| Motive dispatch maydoni | RateCon (loads jadvali) |
|------------------------|-------------------------|
| `vendor_id` | `load_number` |
| `dispatch_stops[0].dispatch_location` | `pickup_address` (qismlarni birlashtirish) |
| `dispatch_stops[0].early_date` | `pickup_date` |
| `dispatch_stops[-1].dispatch_location` | `delivery_address` |
| `dispatch_stops[-1].early_date` | `delivery_date` |
| `dispatch_stops[0].dispatch_location.state` | `origin_state` |
| `dispatch_stops[-1].dispatch_location.state` | `destination_state` |
| `dispatch_stops` (hammasi) | `stops_json` (JSON array) |
| `dispatch_trips[0].driver_id` | `group.eld_driver_id` → `"motive:{driver_id}"` |
| `status = planned/active` | `status = "dispatched"` |
| `status = completed` | `status = "delivered"` |
| `loaded_miles` | `miles` |
| `id` | `motive_dispatch_id` (yangi maydon) |

### Import jarayoni qadam-baqadam

```
1. GET /v2/dispatches?status=active yuborish
2. Har bir dispatch uchun:
   a. vendor_id → load_number (dublikat tekshirish)
   b. dispatch_stops → pickup/delivery/stops_json ga parse qilish
   c. dispatch_trips → driver tayinlash
   d. database.create_load() chaqirish
   e. motive_dispatch_id saqlash
```

---

## Status sinxronizatsiyasi

### RateCon → Motive (status push)

RateCon'da yuk statusi o'zgarganda Motive'ga ham yuborish:

```
PUT https://api.gomotive.com/v2/dispatches
```

| RateCon status | Motive status |
|----------------|---------------|
| `upcoming` | `planned` |
| `dispatched` | `active` |
| `delivered` | `completed` |
| o'chirilgan | `cancelled` |

> **Muhim:** PUT to'liq dispatch ob'yektini talab qiladi (partial update yo'q). Avval GET qilib, o'zgartirib, keyin PUT yuborish kerak.

### Motive → RateCon (status pull)

Haydovchi Motive ilovasida stopga yetib kelganini belgilasa:

| Motive maydoni | Ma'nosi |
|----------------|---------|
| `dispatch_stops[].arrived_at` | Yetib keldi |
| `dispatch_stops[].departed_at` | Jo'nab ketdi |

Bu ma'lumotlarni polling orqali olib, `current_stop_index` ni yangilash mumkin.

### Sinxronizatsiya strategiyasi

| Variant | Tavsif | Murakkablik |
|---------|--------|-------------|
| **A: Polling** | `bot_poller.py` da har 5 daqiqada GET qilish | Sodda |
| **B: Webhook + Polling** | `form_entry_upserted` webhook + polling fallback | O'rtacha |

Tavsiya: avval **Variant A** bilan boshlash.

---

## Manzil formatlash

RateCon'da manzil bitta string:
```
"123 Main St, Dallas, TX 75201"
```

Motive alohida maydonlarni talab qiladi:
```json
{
  "address_line_1": "123 Main St",
  "city": "Dallas",
  "state": "TX",
  "zip": "75201"
}
```

### Parse qilish usullari

**Usul 1: Vergul bo'yicha split**
```python
parts = address.split(",")
# parts[0] = "123 Main St"
# parts[1] = " Dallas"
# parts[2] = " TX 75201" → state va zip ajratish
```

**Usul 2: Google Maps Geocoding** (aniqroq)
```python
# routing.py da mavjud geocode() funksiyasi orqali
# structured address olish mumkin
```

---

## Amalga oshirish rejasi

### 1-qadam: Bazaga yangi maydon

```sql
ALTER TABLE loads ADD COLUMN motive_dispatch_id INTEGER;
```

### 2-qadam: `eld/motive.py` ga yangi metodlar

```python
class MotiveClient:
    # ... mavjud metodlar (get_drivers, get_trucks, get_driver_location) ...

    def create_dispatch_location(self, name, address, city, state, zip_code):
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

    def push_dispatch(self, load, driver_id, vehicle_id):
        """RateCon yukini Motive'ga yuborish."""
        stops = [
            {
                "vendor_id": f"{load['load_number']}-PU",
                "type": "pickup",
                "number": 1,
                "early_date": load.get("pickup_date"),
                "vendor_dispatch_location_id": f"LOC-{load['id']}-PU",
                "status": "available",
            },
            {
                "vendor_id": f"{load['load_number']}-DEL",
                "type": "dropoff",
                "number": 2,
                "early_date": load.get("delivery_date"),
                "vendor_dispatch_location_id": f"LOC-{load['id']}-DEL",
                "status": "available",
            },
        ]
        payload = {
            "vendor_id": load["load_number"],
            "status": "planned",
            "loaded_miles": int(load.get("miles") or 0) or None,
            "dispatch_stops": stops,
            "dispatch_trips": [
                {
                    "vendor_id": f"{load['load_number']}-TRIP",
                    "driver_id": int(driver_id),
                    "vehicle_id": int(vehicle_id),
                    "vendor_stop_ids": [s["vendor_id"] for s in stops],
                    "status": "not_started",
                }
            ],
        }
        resp = httpx.post(
            f"{BASE}/dispatches",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("dispatch", {})

    def get_dispatches(self, status=None, per_page=100):
        """Motive'dan yuklar ro'yxatini olish."""
        params = {"per_page": per_page}
        if status:
            params["status"] = status
        resp = httpx.get(
            f"{BASE}/dispatches",
            headers=self.headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("dispatches", [])

    def update_dispatch_status(self, dispatch_id, new_status):
        """Motive'da yuk statusini yangilash."""
        resp = httpx.get(
            f"{BASE}/dispatches/{dispatch_id}",
            headers=self.headers,
            timeout=10,
        )
        resp.raise_for_status()
        dispatch = resp.json().get("dispatch", {})
        dispatch["status"] = new_status
        resp = httpx.put(
            f"{BASE}/dispatches",
            headers=self.headers,
            json=dispatch,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("dispatch", {})
```

### 3-qadam: Yangi API endpointlar (`app.py`)

| Method | Endpoint | Vazifasi |
|--------|----------|----------|
| `POST` | `/api/loads/{load_id}/motive/push` | Yukni Motive'ga yuborish |
| `GET` | `/api/motive/dispatches` | Motive'dan yuklar ro'yxati |
| `POST` | `/api/motive/dispatches/{dispatch_id}/import` | Bitta yukni import qilish |
| `POST` | `/api/loads/{load_id}/motive/sync-status` | Status sinxronizatsiya |

---

## Cheklovlar va yechimlar

| Muammo | Yechim |
|--------|--------|
| Motive'da DELETE endpoint yo'q | `status: "cancelled"` ga o'zgartirish |
| PUT partial update qilmaydi | GET → o'zgartirish → PUT (to'liq ob'yekt) |
| Manzillar alohida yaratiladi | Avval `dispatch_location` yaratib, keyin stopga bog'lash |
| Dispatch uchun webhook yo'q | `GET /v2/dispatches` bilan polling (har 5 daqiqa) |
| `driver_id` va `vehicle_id` majburiy | Push qilishdan oldin haydovchi va mashina tayinlangan bo'lishi shart |
| Rate limit: 20 req/sec | `app.py` dagi mavjud rate limiter yetarli |

---

## Foydali havolalar

- [Motive API Docs — Dispatches v2](https://developer-docs.gomotive.com/reference/overview-dispatches-v2)
- [Dispatch Locations](https://developer-docs.gomotive.com/reference/create-a-new-dispatch)
- [Authentication](https://developer-docs.gomotive.com/docs/authentication)
- [TMS Integration Workflow](https://developer-docs.gomotive.com/reference/tms-integration-workflow)
- [Webhooks](https://developer-docs.gomotive.com/reference/overview-company-webhooks)
