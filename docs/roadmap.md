# Ratecon (SimpleTrucking) — Mahsulot rejasi

> So'nggi yangilanish: 2026-05-15
> Maqsad: AQSh'dagi 1-25 truck'lik (asosan post-Soviet/o'zbek) carrier va dispatcher'lar uchun Telegram-native TMS.

---

## 1. Pozitsiyalash va asosiy strategiya

### Mavjud manzara
- **Datatruck** (Toshkent + AQSh) — eng yaqin raqobatchi. 4 ta o'zbek asoschisi, $12M Series A (2026-yanvar), 1,000+ kompaniya. Enterprise'ga ketyapti (40+ truck).
- **Truckbase** — UX yetakchisi, $290–490/oy, 5-50 truck.
- **Toro TMS** — $19-49/oy, bulk hauler.
- **LoadOps** — kichik carrier, dispatcher commission qurilgan.

### Sening pozitsiyang
> **"Datatruck for the rest of us"** — Datatruck olishni xohlamaydigan 1-9 truck'li owner-operator va kichik carrier'lar uchun, Telegram'da yashaydigan, ruscha/o'zbekcha gapiradigan, $29-99/oydan boshlanadi.

### Daxlsiz afzalliklar (moats)
1. **Telegram-native** — drayverlar yangi app yuklamaydi
2. **Ruscha + O'zbekcha UI** — Datatruck'da yo'q (asoschilar o'zbek bo'lsa-da, mahsulot English-only)
3. **Owner-operator ham qabul** — Datatruck ≥10 truck talab qiladi
4. **Self-serve narx sahifasi** — Datatruck demo'ni majburlaydi
5. **4 ta ELD provider** (Motive + Samsara + ZippyELD + EVO) — ZippyELD/EVO post-Soviet bozor uchun muhim

### Narx rejasi (yo'l xaritasi)
| Tier | Narx | Truck soni | Datatruck'da analog |
|---|---|---|---|
| **Starter** | $29/oy | 1-3 | yo'q |
| **Growth** | $79/oy | 4-9 | yo'q |
| **Scale** | $199/oy | 10-25 | Pro ($299) |
| **Custom** | – | 25+ | foydalanmaymiz |

---

## 2. Hozirgi holat (2026-05-15)

### Bajarilgan (production'da)
- ✅ AI ratecon ekstraktsiyasi (Gemini)
- ✅ Load CRUD, status, ETA hisoblash
- ✅ Telegram bot — status push, ETA alert, broker'ga avto-xabar (qisman)
- ✅ ELD: Motive + Samsara + Zippy + EVO
- ✅ KPI/komissiya tier (pay_tiers + kpi_entries)
- ✅ Multi-tenant (companies + dispatchers RBAC)
- ✅ Invoice PDF generation
- ✅ Audit log
- ✅ Google Maps geocoding + Nominatim fallback
- ✅ Landing page + privacy/terms (Google for Startups uchun)
- ✅ **Yangi:** 10 ta TMS jadval (customers, vendors, locations, trailers, driver_documents, safety_tasks, bills, transactions, work_orders, mailbox_messages)
- ✅ **Yangi:** Universal CRUD API `/api/entities/{slug}` (5 metod)
- ✅ **Yangi:** Dashboard sahifasi (12 KPI kartochka)
- ✅ **Yangi:** Datatruck-style sidebar (7 ta bo'lim, 24+ menu element)
- ✅ **Yangi:** Schema-driven modal — har entity uchun yagona modal

### WIP (kommit qilingan, lekin to'liq emas)
- 🟡 **Motive dispatch integratsiyasi** — push, sync, import endpoint'lari yozilgan, lekin UI flow polish kerak

---

## 3. Faza 1: Poydevorni mustahkamlash (1-3 oy)

### 3.1 Yangi entity'larni load'lar bilan bog'lash
- [ ] Load yaratishda **Customer dropdown** (broker tanlash)
  - Avto-fill: payment_terms, credit info, contact
  - Auto-create option: "Yangi broker qo'shish" tugma load modal ichida
- [ ] Load yaratishda **Trailer dropdown**
  - Status auto-update: load dispatched → trailer "in use"
- [ ] Load yaratishda **Location autocomplete**
  - Pickup/delivery uchun saqlangan locations'dan tanlash
  - Geocode bir marta — saqlangan koordinatalar ishlatiladi (Google Maps API tejaladi)
- [ ] Transaction avto-yaratish
  - Load delivered → income transaction
  - Bill paid → expense transaction
- [ ] Driver Documents: **expiry alert**
  - 30 kun qolganda Telegram'ga: "Eshmat akangiz CDL 15 kunda tugaydi"
  - Dashboard'da qizil chiroq (allaqachon hisoblanadi)

### 3.2 Email forwarding (Mailbox to'ldirish)
- [ ] Har company uchun unique email: `loads-{company-slug}@ratecon.app`
- [ ] SendGrid Inbound Parse yoki Postmark integratsiya
- [ ] Broker'dan kelgan email → `mailbox_messages` jadvaliga avto-yoziladi
- [ ] PDF attachment bo'lsa → Gemini ekstraksiya → load avto-yaratiladi
- [ ] Dashboard'da "Unread mail" badge avto-yangilanadi

### 3.3 Self-serve onboarding
- [ ] Public pricing sahifasi (`/pricing`)
- [ ] Stripe Checkout — kredit karta bilan to'lov
- [ ] Trial: 14 kun bepul, kredit karta talab qilinmaydi
- [ ] Avto-narx hisoblash: truck soni × tier

### 3.4 Localization
- [ ] Backend: tarjima fayllari (`locales/en.json`, `locales/ru.json`, `locales/uz.json`)
- [ ] Frontend: i18n kalitlar bilan UI matnlarini ajratish
- [ ] User profilida til tanlash
- [ ] Telegram bot xabarlari ham 3 tilda

---

## 4. Faza 2: Differentsiatorlar (4-6 oy)

### 4.1 Chrome Extension 🔥 eng katta yutuq
- [ ] Manifest V3
- [ ] Gmail'da PDF attachment yonida `📥 Send to Ratecon` tugma
- [ ] DAT load board sahifasida `⭐ Save to Ratecon`
- [ ] Truckstop, 123LoadBoard ham qo'llab-quvvatlanadi
- [ ] Chrome Web Store'ga publish
- [ ] Marketing: demo'da birinchi navbatda ko'rsatish (wow factor)

### 4.2 Telegram Mini App (drayverlar uchun)
- [ ] Telegram WebApp API integratsiya
- [ ] BOL/POD skanlash (`navigator.mediaDevices.getUserMedia`)
- [ ] Status yangilash (loaded/in transit/delivered)
- [ ] Hujjatlarni ko'rish (CDL, medical, insurance)
- [ ] Bugungi load ma'lumotlari + xarita
- [ ] **Avantaj:** native app emas, Telegram ichida ochiladi — install yo'q

### 4.3 Broker verification
- [ ] SaferWatch yoki Carrier411 API integratsiya
- [ ] Yangi customer yaratganda real-time MC# tekshiruv
- [ ] Credit score, payment history, complaints
- [ ] "✅ Good standing" yoki "⚠️ Late pay history" indicator

### 4.4 Factoring integratsiyasi
- [ ] **TriumphPay** API (1-25 truck carrier'larning 80% TriumphPay'da)
- [ ] Load delivered → invoice auto-submit
- [ ] Status sync: pending → approved → paid → denied
- [ ] OTR Capital, RTS Financial (keyingi navbatda)

### 4.5 Loadboard integratsiya (kirish darajasi)
- [ ] DAT API ($500-1000/oy)
- [ ] Sening lane'laringga mos yuklarni avto-skanerlash
- [ ] Telegram bot: "Bugun Chicago→Atlanta lane'da $2.85/mile yangi yuk bor"
- [ ] Auto-bidding qilmaymiz (xavfli) — faqat tavsiya

### 4.6 Planning Calendar va Dispatch Board
- [ ] Calendar view (FullCalendar.js): pickup/delivery sanalari
- [ ] Kanban board: Available → Assigned → In Transit → Delivered
- [ ] Drag-drop bilan load'ni driver'ga assign qilish
- [ ] Mavjud `loads.group_id` orqali datasi tayyor

---

## 5. Faza 3: AI va avtomatlash (7-12 oy)

### 5.1 AI Updater (Datatruck'ning kuchli featuri)
- [ ] Driver Telegram'da status berdi → bot avto-Email broker'ga
- [ ] Template'lar broker'ga moslashtirilgan (har broker turli format kutadi)
- [ ] Avto-attach: tracking link, POD photo
- [ ] **Target:** dispatcher 70% kommunikatsiya vaqtini tejaydi

### 5.2 AI Insight Analysis
- [ ] Lane profitability dashboard
  - "Chicago→Atlanta avtomatik $2.65/mile, sen $2.85 kelishding (+7.5%)"
- [ ] Broker performance scoring
  - On-time pay, average rate offered, dispute count
- [ ] Suggest action: "Bu broker bilan kelajakda ehtiyot bo'l"

### 5.3 Voice dispatch (Telegram voice)
- [ ] Drayver Telegram'ga voice yuboradi → OpenAI Whisper transcribe → Gemini parse → status update
- [ ] **Avantaj:** drayver mashina haydab voice yuborishi mumkin
- [ ] Datatruck'da bu yo'q — bizning unique moat

### 5.4 IFTA va Payroll
- [ ] Quarterly IFTA report (ELD'dan mileage per state olib)
- [ ] PDF chiqarish — driver/auditor uchun
- [ ] Payroll run: pay_tiers + kpi_entries → ACH file (NACHA format) export
- [ ] Driver settlement statement (haftalik PDF)

### 5.5 Fuel cards va tolllar
- [ ] EFS yoki Comdata API — transactions avto-import
- [ ] PrePass yoki BestPass — toll cost per load tracking
- [ ] Load profitability avto-recalculate (fuel + toll - rate)

---

## 6. Faza 4: Kengayish (12+ oy)

### 6.1 Apps & Marketplace
- [ ] 3rd-party developer'lar uchun webhook system
- [ ] OAuth provider — boshqa app'lar Ratecon ma'lumotini ulasinlar
- [ ] Marketplace UI — installable integrations (Quickbooks, Stripe, va h.k.)

### 6.2 EDI (faqat agar enterprise'ga o'tsak)
- [ ] EDI 204 (load tender), 214 (status), 210 (invoice)
- [ ] Walmart/Amazon kabi shipper'lar uchun
- [ ] **Diqqat:** Faqat segment kengaytirsak — kichik carrier'lar EDI ishlatmaydi

### 6.3 Mobile native app (Telegram Mini App yetmagan bo'lsa)
- [ ] React Native — iOS + Android
- [ ] **Diqqat:** Faqat Telegram Mini App user'lar talab qilsa qo'shamiz

### 6.4 Enterprise features (agar 25+ truck'ga o'tsak)
- [ ] SOC 2 audit (~$30-50K/yil)
- [ ] SSO (SAML, Okta)
- [ ] Audit log retention, RBAC granular permission'lar
- [ ] Custom contract terms, SLA

---

## 7. Risklar va dependency'lar

### 7.1 Texnik risklar
- **PostgreSQL scaling** — 1000+ company'da read replica kerak bo'ladi
- **Telegram rate limits** — 30 msg/sec per bot. 1000+ company'da multi-bot kerak
- **Gemini quotalari** — kuniga 1500 free request. Paid tier'ga o'tish kerak
- **ELD API rate limits** — har provider'da boshqacha. Cache strategiya bor, lekin masshtab kerak

### 7.2 Biznes risklar
- **Datatruck SMB segmentga qaytsa** — ular yana $99/oy Basic tier'iga qaytishi mumkin. Sening narx ostida tushish kerak
- **Truckbase narxni pasaytirsa** — hozir $290-490, agar $99'ga tushsa, segmentga zarba
- **AQSh regulyatsiyasi** — DOT yangi qoidalar, ELD mandatlari o'zgarishi mumkin

### 7.3 Tashqi dependency'lar
- **GitHub PAT** sinilgan — git bundle orqali deploy qilyapmiz. Tuzatish: SSH key yoki yangi PAT
- **Google Maps API** — costly, alternativ Mapbox/OSRM ishga tushirildi
- **Stripe** — AQSh kompaniyasi kerak (LLC bo'lsa bo'ladi)
- **TriumphPay API access** — partnership program orqali olinadi (3-6 oy)

---

## 8. Asosiy ko'rsatkichlar (KPI'lar)

### Faza 1 (3 oy)
- 50 ta self-serve signup
- 10 ta to'lovchi mijoz
- $1,000 MRR
- 100+ load/kun tizimga qo'shiladi

### Faza 2 (6 oy)
- 200 to'lovchi mijoz
- $20,000 MRR
- Chrome extension 500+ install
- Telegram Mini App 1000+ DAU

### Faza 3 (12 oy)
- 500 to'lovchi mijoz
- $75,000 MRR
- $1M ARR yaqinida
- Series A raise (Datatruck'dan keyingi o'zbek logistic startup)

---

## 9. Aniq keyingi qadamlar (priority)

Bugun deploy qilingan poydevor ustida, **birinchi 4 hafta** uchun aniq vazifalar:

### Hafta 1
1. Load yaratish modal'iga **Customer dropdown** qo'shish
2. Load yaratish modal'iga **Location autocomplete** qo'shish
3. Driver document expiry → Telegram'ga alert chiqishi

### Hafta 2
4. Public `/pricing` sahifasi (no Stripe yet)
5. Mailbox'da broker email simulyatsiyasi (manual qo'shish UI bilan)
6. Mailbox'dan PDF attachment'ni `/extract` ga uzatish — bir bosishda load yaratish

### Hafta 3
7. Stripe Checkout integratsiyasi
8. Self-serve signup flow (`/register` to'liq)
9. Tier'lar bo'yicha truck soni cheklash

### Hafta 4
10. SendGrid Inbound Parse — haqiqiy email forwarding
11. Postmark fallback (agar SendGrid spam'da tushsa)
12. Russian/Uzbek UI tarjimalari (i18n yo'lboshchisi)

---

## 10. Manbalar

### Asosiy raqobatchilar
- Datatruck: https://www.datatruck.io
- Truckbase: https://www.truckbase.com
- Toro TMS: https://www.torotms.com
- LoadOps: https://loadops.com
- DispatchMVP: https://dispatchmvp.ai
- Numeo AI: https://numeo.ai

### Texnik API'lar
- TriumphPay: https://triumphpay.com/api
- SaferWatch (broker verify): https://saferwatch.com
- DAT load board API: https://developer.dat.com
- SendGrid Inbound Parse: https://docs.sendgrid.com/for-developers/parsing-email/inbound-email
- Postmark Inbound: https://postmarkapp.com/inbound

### Tashkilotlar
- IT Park Ventures (Toshkent) — seed funding
- Aloqa Ventures — seed funding
- Silkroad Angels Club — angel investors
- Google for Startups — Datatruck programma'ga kirgan, sen ham urin
