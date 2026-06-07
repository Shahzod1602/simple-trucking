# Ratecon (SimpleTrucking) — Audit va Yo'l Xaritasi

> Sana: 2026-06-07
> Qamrov: backend (FastAPI) + ilova frontend'i (`templates/index.html`, `admin.html`, `setup.html`). Landing page **qamrovda emas**.
> Metod: 4 ta parallel kod-audit agenti + 1 raqobatchi-tahlil agenti, so'ng topilmalar kodda tasdiqlangan va asosiylari tuzatilgan.

---

## 1. Qisqa xulosa

Ilova boy funksionallikka ega va ko'p joyda ehtiyotkor yozilgan (parametrlangan SQL, entity jadval nomi allowlist'da, multi-tenant scoping aksariyat o'qishlarda to'g'ri, login rate-limit, audit log, salt'langan PBKDF2). Lekin **to'rt sohada jiddiy muammolar** bor edi:

1. **Maxfiy kalitlar gigienasi** — `.env.example` git tarixida haqiqiy Gemini kalit, jonli `.env` da `1234567890` superadmin paroli.
2. **Auth/transport** — muddatsiz, bekor qilib bo'lmaydigan bearer tokenlar; nginx faqat HTTP (TLS yo'q).
3. **Xarajat/DoS** — `/extract` (Gemini) va `/api/geocode-stops` (Google Maps) auth'siz va cheklovsiz edi.
4. **Barqarorlik va to'g'rilik** — bloklovchi I/O `async` handler'larda event-loop'ni muzlatadi; pul `TEXT` + `float` sifatida saqlanib, dashboard daromadi noto'g'ri (`"$2,500"` → `0`).

Bu sessiyada **eng yuqori ta'sirli xavfsizlik, to'g'rilik va barqarorlik tuzatishlari amalga oshirildi va PostgreSQL'ga qarshi sinovdan o'tkazildi** (4/4 test + end-to-end tekshiruv). Qolgan muammolar 3-bo'limda ustuvorlik bo'yicha keltirilgan.

---

## 2. Shu sessiyada tuzatilgan muammolar

| # | Jiddiylik | Muammo | Tuzatish | Fayl(lar) |
|---|---|---|---|---|
| 1 | **CRITICAL** | `.env.example` da haqiqiy Gemini kalit (git tarixida) | Placeholder'larga almashtirildi; barcha kalitlar namuna sifatida | `.env.example` |
| 2 | **CRITICAL** | Entity jadvallarida stored XSS (`${v}` escape'siz innerHTML; JSON-in-onclick) | Qiymatlar `esc()` bilan; Edit tugmasi `id`-lookup orqali (markupda data yo'q) | `templates/index.html` |
| 3 | **HIGH** | `/extract` va `/api/geocode-stops` auth'siz xarajat/DoS vektori | `require_dispatcher` + per-dispatcher rate-limit qo'shildi | `app.py`, `index.html` |
| 4 | **HIGH** | Multi-tenant IDOR: `delete_kpi_entry` company_id'ni inkor qiladi; `add_kpi_entry`/`upsert_kpi_comment` body FK'ga ishonadi | Barcha KPI yozuv/komment/o'chirish company-scoped; FK'lar kompaniyaga tekshiriladi | `database.py`, `app.py` |
| 5 | **HIGH** | Bloklovchi I/O `async` handler'larda event-loop'ni muzlatadi | Eng og'ir 13 handler `def`/`to_thread` ga o'tkazildi (extract, geocode, ELD, Motive, map, invoice, alerts, trucks) | `app.py` |
| 6 | **HIGH** | Pul `TEXT`+`float`; dashboard regex `"$2,500"` ni `0` deb hisoblaydi | Yagona `parse_money()` + xavfsiz SQL `_rate_to_numeric()`; komissiya `Decimal` bilan | `database.py`, `app.py` |
| 7 | **HIGH** | Gemini chaqiruvi: timeout/retry/null-check yo'q; quota → xom 500 | Timeout (60s), backoff retry, bo'sh-javob guard, quota→503, fayl-hash cache | `extractor/llm_extractor.py`, `app.py` |
| 8 | **HIGH** | Telegram `/link` bearer tokenni guruh chatiga oshkor qiladi | Single-use qisqa kod (30 daqiqa amal qiladi); token endi qabul qilinmaydi | `database.py`, `bot_poller.py`, `app.py`, `index.html` |
| 9 | **HIGH** | Bot offset faqat xotirada → restart'da `/delivered` qayta ishlaydi | Offset DB'da saqlanadi; har-update xato izolyatsiyasi; long-polling | `bot_poller.py`, `telegram.py` |
| 10 | **MEDIUM** | `geocode()` Google xatosida Nominatim fallback'ga o'tmaydi | Google chaqiruvi try/except'ga olindi; barcha fallback'lar ishlaydi | `routing.py` |
| 11 | **MEDIUM** | Geocode cache yo'q → takroriy xarajat + Nominatim rate-limit | In-process geocode/reverse-geocode cache | `routing.py` |
| 12 | **MEDIUM** | Hot jadvallarda indeks yo'q (loads, dispatchers, groups, kpi…) | 11 ta indeks `init_db`'ga qo'shildi | `database.py` |
| 13 | **MEDIUM** | 3 ta dublikat tier-matching; mos kelmasa jimgina $0 | Yagona `match_tier()`/`tier_earning()`; mos-yo'q holati `tier_percentage=None` orqali ko'rsatiladi | `database.py`, `app.py` |
| 14 | **MEDIUM** | Schema-modal o'z metama'lumotini inkor qiladi (validatsiya yo'q, `alert`, xom FK-id input) | Required/number inline validatsiya; FK & enum dropdown'lar; toast | `templates/index.html` |
| 15 | **MEDIUM** | Modal a11y: role/aria/Escape/label-link yo'q | `role=dialog`, `aria-modal`, Escape, fokus, `<label for>`, overlay-bosib-yopish | `templates/index.html` |
| 16 | **MEDIUM** | `/extract` xato `str(e)` ni mijozga oshkor qiladi (internals leak) | Umumiy xabar + server-side log | `app.py` |
| 17 | **LOW** | Aniqlanmagan CSS o'zgaruvchilari (`--surface-2`, `--bg-card`…) | `:root`'da tokenlarga bog'landi | `templates/index.html` |
| 18 | **LOW** | `datetime.utcnow()` (deprecated, naive) ETA'da | `datetime.now(timezone.utc)` | `routing.py` |
| 19 | **LOW** | `/deliveredxyz` `/delivered` ni ishga tushiradi; `@botname` ishlamaydi | Buyruq normalizatsiyasi (`@bot` olib tashlanadi, aniq moslik) | `bot_poller.py` |

**Tekshiruv:** Barcha Python fayllar kompilyatsiya bo'ldi; mavjud test to'plami PostgreSQL'ga qarshi **4/4 o'tdi**; money/link-code/IDOR/earnings tuzatishlari real DB'da end-to-end tasdiqlandi (`"$2,500.00"` → dashboard `2500.0`).

---

## 3. Hali tuzatilmagan muammolar (ustuvorlik bo'yicha)

### 3.1 Xavfsizlik (keyingi navbatda)
- **Token modeli** — muddatsiz, bekor qilib bo'lmaydigan UUID bearer tokenlar; parol o'zgarganda rotatsiya yo'q, logout yo'q. → JWT (`exp` bilan) yoki server-side sessiya; parol o'zgarganda tokenni yangilash; `/api/logout`.
- **TLS / security headers** — nginx faqat `:80`. → 443'da TLS terminatsiya, 80→443 redirect, HSTS, `X-Content-Type-Options`, `X-Frame-Options`/CSP, `Referrer-Policy`.
- **Parol hash** — PBKDF2-SHA256 (200k). → Argon2id (`argon2-cffi`) yoki bcrypt; minimal uzunlik 6 → 10-12.
- **ELD kalitlari ochiq matnda** (`eld_configs.api_key`/`provider_token`). → `cryptography.Fernet` bilan shifrlash; list endpoint'da maskalash.
- **`get_eld_configs` company-fallback** — past-huquqli user admin ELD kalitidan foydalanadi. → admin-gate yoki har chaqiruvni audit-log.
- **Ochiq `/api/register` + birinchi user avto-admin** — yangi deploy'da birinchi ro'yxatdan o'tgan global admin bo'ladi. → `count_dispatchers()==0 → admin` shartini olib tashlash yoki register'ni invite orqasiga olish.
- **`/api/health` internals oshkor qiladi** (DB xato matni, cache hajmi). → auth'siz faqat `{"ok": bool}`.

### 3.2 To'g'rilik / ma'lumot yaxlitligi
- **Migratsiya tizimi yo'q** — `schema_migrations` jadvali bor lekin ishlatilmaydi; barcha DDL har startup'da `init_db`'da. → Alembic yoki kichik runner.
- **`UNIQUE(email)` yo'q** — register check-then-act poygasi (dublikat akkaunt). → `ALTER TABLE dispatchers ADD CONSTRAINT … UNIQUE(email)` (avval dedup), `kpi_entries(load_id)` va `loads(motive_dispatch_id)` uchun partial unique.
- **Pul ustunlari hali ham `TEXT`** — parsing endi mustahkam, lekin to'liq yechim: `total_rate_cents BIGINT`/`NUMERIC` ustun, yozuvda normalizatsiya, backfill.
- **Load status hayot-sikli validatsiyasi yo'q** — `delivered → upcoming` mumkin; `current_stop_index` semantikasi Motive vs qo'lda yo'lda mos kelmaydi. → ruxsat etilgan o'tishlar xaritasi.
- **Tranzaksiyasiz ko'p-qadamli yozishlar** — Motive import = 2 alohida `get_conn()`; qisman holat/dublikat xavfi. → bitta `get_conn()` ichida atomik.
- **`invoice.py`** — `load["broker"]` o'qiydi (ustun `broker_name`), shuning uchun har invoice'da placeholder; total accessorial/charge'larni hisobga olmaydi.

### 3.3 Barqarorlik / arxitektura
- **Qolgan ~70 DB-handler hali `async`** — endi indekslangani uchun tez (bir necha ms), lekin event-loop'ni qisqa muddat bloklaydi. → barchasini `def` ga o'tkazish + DB pool `maxconn`'ni anyio threadpool'iga moslab oshirish (yoki Redis-backed rate-limit/cache, chunki hozir `--workers 4` da per-process).
- **In-memory rate-limit/cache `--workers 4` da per-process** — login limiti ~5× zaifroq, prefetch faqat 1 worker'da. → Redis.
- **`app.py` 2800 qator monolit** — auth/company/load-ownership uchun FastAPI `Depends` ajratish (40+ takror boilerplate).

### 3.4 Frontend
- **Bitta 6000-qatorli HTML** — CSS/JS'ni cache'lanadigan `static/app.css`/`app.js` ga ajratish (har navigatsiyada 363KB qayta yuklanadi).
- **Modal focus-trap** — Escape/fokus qo'shildi, lekin to'liq Tab-trap qoldi.
- **Loads jadvali mobil'da 1200px** — <768px uchun stacked-card layout; 8-amalli katakni "⋯" menyuga.
- **Navigatsiya bir-birini takrorlaydi** — "Live Loads" ni All-Loads "Dispatched" tab'iga birlashtirish; "Dispatch Map" vs "Live Map".
- **localStorage-only Send History** — server-side qilish (audit-log allaqachon yozadi).
- **FK dropdown'larni kengaytirish** — hozir asosiy FK'lar (vendor, group, trailer, load, bill) dropdown; qolganlari uchun ham.

---

## 4. KRITIK qo'lda bajariladigan amallar (men qila olmadim)

> Bular sizning hisoblaringizga kirishni talab qiladi — iltimos zudlik bilan bajaring:

1. **BARCHA maxfiy kalitlarni rotatsiya qiling** (buzilgan deb hisoblang):
   - Ikki Gemini kalit (`.env.example` dagi `…GoPs` va jonli `…rhjk`) — Google Cloud Console.
   - Telegram bot token — BotFather `/revoke`.
   - Google Maps kalit — Google Cloud (+ HTTP referrer/IP cheklov qo'ying).
   - PostgreSQL paroli.
2. **Superadmin parolini kuchli qilib o'zgartiring** — hozirgi `1234567890` ni almashtiring (`.env` da `SUPER_ADMIN_PASSWORD`).
3. **Git tarixini tozalang** — `.env.example` dagi kalit hali tarixda. `git filter-repo` yoki BFG bilan o'chiring (rotatsiya'dan keyin).
4. **nginx'da TLS yoqing** — 443, 80→443 redirect, HSTS.

---

## 5. Raqobatchi tahlili — eng yaxshi g'oyalar

**Strategik xulosa:** Bozor ikkiga bo'lingan — Datatruck/McLeod 10+ (200+) truck'ga ketdi, AI-startaplar (Numeo $349+, DispatchMVP) broker-negotiation'ni premium narxda quvyapti, **hammasi ingliz tilida**. 1-25 truck'li, rus/o'zbek tilli owner-operator segmenti — **bo'sh**. Yutuq: Truckbase'ning sevimli oqimini (ratecon → dispatch → BOL/POD → invoice → settlement) **Telegram ichida, rus/o'zbekcha, self-serve, owner-operator narxida** yetkazish.

### Raqobatchi narxlari (2026)
| Platforma | Narx | Segment | Eng kuchli tomoni |
|---|---|---|---|
| Datatruck | $99 (1-6), $299 (7-25); AI Updater +$99, AI Dispatcher +$399 | 10+ truck | Profit-per-truck/lane ko'rinishi; TruckGPT |
| Truckbase | $290+/oy | 5-50 | **UX** (30 daq o'rganiladi); PDF→dispatch→BOL oqimi |
| Toro TMS | $19 / $29 / $49 | bulk, kichik | Eng arzon, shaffof narx |
| LoadOps | $55-75/driver | o'rta | Load-board integratsiyasi |
| Numeo AI | $69 (1-10), $349 (11-100); Spot bepul | AI-first | Broker'ga AI qo'ng'iroq, email rate negotiation |
| DispatchMVP | trial; narx yopiq | AI-first | "Otto" ovozli dispatch |

### 15 eng yaxshi g'oya (ta'sir/kuch bo'yicha)
| # | G'oya | Ilhom | Niche'ga mosligi | Kuch | Ta'sir |
|---|---|---|---|---|---|
| 1 | **Telegram-native auto check-call / "AI Updater" lite** — ELD GPS'dan ETA+status'ni broker thread'iga avto-post | Datatruck Updater | Datatruck'ning $99 feature'i, lekin Telegram'da, RU/UZ | M | Yuqori |
| 2 | **Self-serve onboarding (<30 daq, sotuv qo'ng'irog'isiz)** | Truckbase UX | Sizning segment ingliz sotuv qo'ng'irog'iga chidamaydi | M | Yuqori |
| 3 | **Bir bosishda "yukni haydovchiga yuborish" (Telegram)** | Truckbase | Bot allaqachon bor — handoff'ni 1 amalga | S | Yuqori |
| 4 | **To'liq zanjir: ratecon import → Telegram dispatch → BOL/POD foto** | Truckbase (#1) | Hamma qism bor; ulash = "shunchaki ishlaydi" | M | Yuqori |
| 5 | **Hujjat fotosini avto-yaxshilash ("scan" rejimi)** | DispatchMVP | Yomon yorug'likda foto → toza PDF → kam factoring rad | S | O'rta |
| 6 | **Dublikat-kiritishsiz: load → invoice → settlement avtomatik** | Truckbase | Kichik carrier #1 istagi; invoice+tier bor | M | Yuqori |
| 7 | **Profit-per-load / per-truck (yengil)** | Datatruck | Owner-operator marja'ga qaraydi; ma'lumot bor | M | Yuqori |
| 8 | **Screenshot/forward-to-bot yuk kiritish** | DispatchMVP, Numeo | Telegram'da screenshot bilan yuk keladi | M | Yuqori |
| 9 | **Telegram'da suhbat-dispatch (matn "Otto", RU/UZ)** | DispatchMVP Otto | Ovoz qiyin; matn-NL erishish mumkin, noyob | M | Yuqori |
| 10 | **1-2 truck uchun bepul/ultra-arzon tier** | Toro, Numeo Lite | Eng kichikni egallash → o'sish dvigateli | S | Yuqori |
| 11 | **IFTA hisoboti** (ELD mileage'dan) | Table-stakes | US carrier majburiy talabi; stickiness | M | O'rta |
| 12 | **Factoring/TriumphPay yuborish (1 bosish)** | Truckbase, OTR | Deyarli har kichik carrier factoring qiladi | S-M | Yuqori |
| 13 | **Mijoz/shipper status linki** (login'siz) | Truckbase portal | 5-truck carrier professional ko'rinadi | M | O'rta |
| 14 | **AI email rate negotiation** (keyinroq) | Numeo | Daromad yuqori; ingliz + javobgarlik → fast-follow | L | O'rta-Yuqori |
| 15 | **AI ovozli broker-qo'ng'iroq** (uzoq muddat) | DispatchMVP, Numeo | Frontier; og'ir; kuzatib, RU/UZ bilan o'zib ketish | L | O'rta |

### Tavsiya etilgan ketma-ketlik
- **Hozir (1-2 chorak):** #3, #5, #10, #12 (S) + #1, #2, #4, #6, #8, #9 (M). Bu = "Truckbase oqimi, lekin Telegram-native, RU/UZ, self-serve, owner-operator narxida".
- **Keyin:** #7 (profit), #11 (IFTA), #13 (mijoz linki).
- **Fast-follow/kuzatuv:** #14, #15.

---

## 6. Yo'l xaritasi (fazalar)

**Faza 0 — Xavfsizlik yopilishi (1 hafta):** 4-bo'limdagi qo'lda amallar (kalit rotatsiya, git tarix, TLS, superadmin parol) + token muddati/rotatsiyasi + Argon2.

**Faza 1 — Poydevor mustahkamlash (1-3 oy):** migratsiya tizimi (Alembic), `UNIQUE(email)`, pul ustunlarini `NUMERIC`'ga, load status validatsiyasi, qolgan handler'larni `def`+Redis (rate-limit/cache), `app.py` `Depends` refaktor.

**Faza 2 — Differensiatorlar (3-6 oy):** #1-#10 g'oyalar (Telegram auto check-call, self-serve, 1-bosish dispatch, to'liq ratecon→POD zanjiri, screenshot-to-bot, matn-NL dispatch, profit ko'rinishi, arzon tier).

**Faza 3 — Kengayish (6-12 oy):** #11-#13 (IFTA, factoring, mijoz linki) + frontend bundling/mobil, keyin #14-#15 (AI negotiation/ovoz) kuzatuvi.

---

*Manbalar: raqobatchi narxlari va feature'lari uchun Datatruck, Truckbase, Toro, LoadOps, Numeo, DispatchMVP rasmiy sahifalari va G2/Capterra sharhlari (2025-2026).*
