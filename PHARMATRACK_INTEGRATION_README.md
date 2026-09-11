# PharmaTrack + Customer App — Required Modifications

This document describes **every change that must be made** so that:

1. The **customer mobile app** reads its data from the **PharmaTrack API** instead of Firebase Firestore.
2. The **whole system** can be **hosted** on a public server.

It is a planning/research document. Nothing here has been implemented yet.

---

## 1. Current State

| Component | Stack | Data source today |
|---|---|---|
| `pharmatrack/` | Flask + SQLite + pywebview + Tailwind | Local `pharmacy.db` |
| `customer_mobile_app/` | Flutter (Android/iOS/web) | Firebase Auth + Cloud Firestore |
| `pharmacy_web_app/` | Flutter web | Firebase Auth + Cloud Firestore |

**PharmaTrack's API** (`/api/v1`) already exists and is JWT-protected (roles `admin`, `pharmacy`, `user`), but it was designed for a **single pharmacy's internal inventory**:

- Products, batches, stock movements, loss reports, settings.
- No pharmacy profile data, no prices, no images, no multi-pharmacy support.
- Public `GET /products` returns only name/category/strength/form/prescription + an `in_stock` boolean.

**The customer app** is a Firebase-backed, drug-first discovery app:
- Lists active pharmacies (name, address, city, phone, lat/lng, opening hours).
- Searches every pharmacy's drug inventory by name.
- Shows drug prices, shows "popular medicines" aggregated across pharmacies.
- No login; read-only.

**The two have almost no shared fields.** Connecting them is a data-model redesign, not a rewiring.

---

## 2. Goal

- The customer app talks to a **hosted PharmaTrack server** via its REST API.
- PharmaTrack becomes a **multi-pharmacy platform** (each `pharmacy`, `admin`, and `pharmacist/user` scoped to a pharmacy).
- Firebase is **fully removed** from the customer app.

---

## 3. Required Modifications — PharmaTrack Database

Current schema is in `pharmatrack/database/schema.sql`.

### 3.1 New table: `pharmacy`

All existing records (products, batches, movements, users) belong to a pharmacy once this lands.

```sql
CREATE TABLE pharmacy (
    id              TEXT PRIMARY KEY,            -- UUID
    name            TEXT NOT NULL,
    address         TEXT,
    city            TEXT,
    latitude        REAL,
    longitude       REAL,
    phone           TEXT,
    emergency_phone TEXT,
    weekday_open    TEXT,                        -- 'HH:MM' or NULL (24h)
    weekday_close   TEXT,
    weekend_open    TEXT,
    weekend_close   TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | suspended | deleted
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### 3.2 New columns on `product`

The customer app shows prices and images; PharmaTrack has neither.

```sql
ALTER TABLE product ADD COLUMN pharmacy_id TEXT REFERENCES pharmacy(id);
ALTER TABLE product ADD COLUMN price_per_unit   REAL;
ALTER TABLE product ADD COLUMN price_per_packet REAL;
ALTER TABLE product ADD COLUMN packet_size      INTEGER;
ALTER TABLE product ADD COLUMN unit_label       TEXT DEFAULT 'unit';
ALTER TABLE product ADD COLUMN image_url        TEXT;
```

> Migration note: existing products must be assigned to a pharmacy (run an UPDATE after creating the pharmacy row). Follow the existing `db.py:_run_migrations()` pattern — add columns only if missing, on every startup.

### 3.3 `settings` table

Aligned with the new `pharmacy` table:
- Keep `low_stock_threshold` (already per-installation).
- `pharmacy_name` / `pharmacy_address` / `pharmacy_latitude` / `pharmacy_longitude` are **replaced** by the `pharmacy` row. Read from `pharmacy` instead.

### 3.4 PostgreSQL (for production hosting)

SQLite's single write-lock file will not survive multiple pharmacies/users on a public server.

- Port all queries in `database/queries.py` to use a pool (e.g. `psycopg` + a small connection helper).
- Or use SQLAlchemy to wrap both engines.
- Or: keep `database/db.py` as an abstraction and add a Postgres implementation.
- Enable foreign keys per connection, same as today (`PRAGMA foreign_keys = ON` → Postgres enforces by default).
- Schedule the SQLite → Postgres data migration for existing installs.

---

## 4. Required Modifications — PharmaTrack API

Blueprint prefix: `/api/v1` (`api/__init__.py`). Existing endpoints listed at the end of the README.

### 4.1 Existing endpoints — scope to the logged-in pharmacy

All pharmacy/admin inventory endpoints must be restricted to the caller's own pharmacy:

- `POST /products`, `PUT /products/<id>`, `POST /movements` → only if the product/batch belongs to the JWT's `pharmacy_id`.
- `GET /products/<id>/batches` → same.
- `GET /products` (token view) → only the caller's pharmacy products.

Add a `pharmacy_id` claim to the JWT, alongside `role` and `name`.

### 4.2 New endpoints (all public unless noted)

| Method | Path | Purpose | JSON fields returned |
|---|---|---|---|
| `GET` | `/pharmacies` | List active pharmacies | `id, name, address, city, phone, emergency_phone, latitude, longitude, hours{weekday_open, weekday_close, weekend_open, weekend_close}, status` |
| `GET` | `/pharmacies/<id>` | One pharmacy (404 if not active) | Same fields |
| `GET` | `/pharmacies/<id>/products` | One pharmacy's safe inventory | `id, name, category, strength, dosage_form, requires_prescription, price_per_unit, price_per_packet, packet_size, unit_label, image_url, in_stock` |
| `GET` | `/products/search?q=` | Cross-pharmacy drug search | Grouped result: `{pharmacy: {...}, drugs: [...]}` — pharmacy info + matching drugs with prices + `in_stock` |
| `GET` | `/products/popular` | Popular medicines aggregation | `name, form_label, cheapest_price, pharmacy_count, any_in_stock` |

### 4.3 Extend the existing public `GET /products`

Currently returns only: `id, name, category, strength, dosage_form, requires_prescription, in_stock`.

Must **add**: `price_per_unit, price_per_packet, packet_size, unit_label, image_url, city, distance_km` (optional), and the owning pharmacy's `id`/`name`. Controlled products remain excluded (existing rule). Exact stock numbers remain hidden publicly.

### 4.4 Add helper endpoints for the app's needs

- `GET /pharmacies/<id>/drugs?q=` — per-pharmacy inventory filter (the app's pharmacy detail screen has an in-section search).
- Keep search case-insensitive, like the existing `LIKE` behavior in `queries.py:get_product_list()`. Beware the Postgres `ILIKE` change.

### 4.5 Public endpoint behavior

The customer app is login-free for browsing, so all discovery endpoints stay public (as today). Only inventory *management* keeps its `pharmacy`/`admin` JWT requirement. A `POST /auth/login` for the `user` role may be added later for personalized features — do **not** make discovery require it.

---

## 5. Required Modifications — PharmaTrack Web / Desktop App

1. **Persistent session secret** — `app.py:35` uses `app.secret_key = os.urandom(32)`, which logs everyone out on restart. On a hosted server this must come from an env var (`SECRET_KEY`).
2. **Admin Settings page** (`app.py:/settings`) — already writes `pharmacy_name/address/latitude/longitude`; update it to write the new `pharmacy` table once (including `city`, `phone`, `emergency_phone`, hours, `status`).
3. **Register/login flow** — unchanged for desktop, but the API role mapping (`pharmacist` ↔ `pharmacy`) must be preserved.
4. **CORS** — add a CORS extension and allow only trusted origins (the hosted dashboard and later the web build of the customer app), as warned in the existing README.
5. **Persistent rate limiting** — login limiter is in-memory (`app.py:83`). Back it with the database (e.g. `login_attempts` table) for a multi-process server.
6. **WSGI entry point** — `wsgi.py` already exists and skips pywebview; keep it, and do not ship the desktop window on the server.

---

## 6. Required Modifications — Hosting / Deployment

New infrastructure files to add (not created yet):

- `Dockerfile` — Python 3.10+ image, `pip install -r requirements.txt`, run `waitress-serve wsgi:app` (or gunicorn on Linux).
- `docker-compose.yml` — app + PostgreSQL (+ Nginx optionally).
- `.env.example` — `PHARMATRACK_ENV=production`, `SECRET_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`, `CORS_ORIGINS`, token lifetimes.
- Reverse proxy with TLS (Caddy/Nginx) for HTTPS.

Production environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `PHARMATRACK_ENV` | yes | `production` (app refuses to start otherwise) |
| `JWT_SECRET_KEY` | yes | Persistent JWT signing secret |
| `SECRET_KEY` | yes (new) | Persistent session cookie secret |
| `DATABASE_URL` | yes (new) | Postgres connection string |
| `JWT_ACCESS_TOKEN_MINUTES` | no | Default 30 |
| `JWT_REFRESH_TOKEN_DAYS` | no | Default 30 |
| `CORS_ORIGINS` | no | Comma-separated allowed origins |

---

## 7. Required Modifications — Customer Mobile App

Firebase replaces → REST to the hosted PharmaTrack server.

### 7.1 Dependencies

Remove:
- `firebase_core`, `cloud_firestore`, `firebase_options.dart`

Add:
- `http` (already present), plus a JSON parsing approach (or `dio` if preferred).

### 7.2 New configuration

- `lib/config.dart` (or similar) — holds the API base URL (e.g. `https://api.yourdomain.com/api/v1`), settable per build (dev/prod).

### 7.3 Service layer — rewrite `PharmacyService`

Current `pharmacy_service.dart` makes Firestore calls. Replace each with an HTTP call:

| Current method (Firestore) | New implementation (REST) |
|---|---|
| `fetchPharmacies()` — `collection('pharmacies').get()` | `GET /pharmacies` (filter `active` client-side, keep Haversine sort) |
| `fetchPharmacy(id)` — `collection('pharmacies').doc(id).get()` | `GET /pharmacies/<id>` |
| `fetchDrugs(pharmacyId)` — subcollection read | `GET /pharmacies/<id>/products` |
| `searchDrugs(query)` — `collectionGroup('drugs')` | `GET /products/search?q=` |
| `fetchPopularDrugs()` — client aggregation | `GET /products/popular` |
| `distanceKmBetween(a, b)` | Unchanged (client-side Haversine) |

- Keep the existing 10-minute cache / SharedPreferences fallback pattern; it now stores REST JSON instead of Firestore snapshots.
- Keep the `PharmacySearchResult` helper shape; populate it from the search endpoint's grouped response.
- Add timeout + retry + offline fallback handling in line with the existing `routing_service.dart` style.

### 7.4 Models — map JSON instead of Firestore documents

| Model | Firestore field → | API JSON field |
|---|---|---|
| `Pharmacy.name` | `name` | `name` |
| `Pharmacy.address` | `address` | `address` |
| `Pharmacy.city` | `city` | `city` |
| `Pharmacy.phone` | `emergencyPhone` or `phone` | `phone` / `emergency_phone` |
| `Pharmacy.latitude/longitude` | `latitude`/`longitude` | `latitude` / `longitude` |
| `Pharmacy.hours` | `hours` map | `hours.weekday_open` etc. |
| `Pharmacy.status` | `status` | `status` |
| `Drug.name` | `name` | `name` |
| `Drug.pricePerUnit` | `pricePerUnit` / legacy `price` | `price_per_unit` |
| `Drug.pricePerPacket` | `pricePerPacket` | `price_per_packet` |
| `Drug.packetSize` | `packetSize` | `packet_size` |
| `Drug.unitLabel` | `unitLabel` | `unit_label` |
| `Drug.quantity` | `quantity` (int) | **`in_stock` (bool)** — no public exact stock |
| `Drug.category` | `category` | `category` |
| `Drug.expiryDate` | `expiryDate` | not in public view (fine) |
| `Drug.imageUrl` | `imageUrl` | `image_url` |

**Important behavioral change:** the public API never returns exact quantity. The app's `Drug.inStock` must switch from `quantity > 0` to the API's `in_stock` boolean. Keep `_manualOpen` style overrides if present.

`PopularDrug` (name, formLabel, cheapestPrice, pharmacyCount, anyInStock) maps 1:1 to the new `/products/popular` endpoint.

### 7.5 Add a JWT client layer (optional for now)

Discovery stays public and anonymous, so a token is **not required** to browse. Add a small `AuthService` later if the app needs personalized features:
- `POST /auth/login` (name/password), store `access_token` + `refresh_token` in `shared_preferences`.
- `POST /auth/refresh` when the access token (30 min) expires.
- `Authorization: Bearer <token>` header, refresh-on-401 logic.

### 7.6 Files that stay **unchanged**

- `location_service.dart` (Nominatim geocoding)
- `routing_service.dart` (OSRM)
- `offline_map_service.dart` (tile caching)
- `prefs_service.dart`, `connectivity_service.dart`, `launcher_service.dart`
- `app_state.dart`
- All map / directions / splash / onboarding / settings screens (they render the models, which keep the same Dart shapes)
- `mini_map`, widgets, theme

Only `main.dart`, `pharmacy_service.dart`, and the three model files change behavior.

---

## 8. Data Model Comparison (Firestore → API)

```text
pharmacies/{id}                      →  GET /pharmacies/<id>
    name, address, city,             =    same fields in the pharmacy row
    phone, emergencyPhone,           =    phone, emergency_phone
    latitude, longitude, status,     =    latitude, longitude, status
    hours {weekdayOpen, ...}         =    hours {weekday_open, ...}

pharmacies/{id}/drugs/{drugId}       →  GET /pharmacies/<id>/products
    name, pricePerUnit/price,        =    name, price_per_unit
    pricePerPacket, packetSize,      =    price_per_packet, packet_size
    unitLabel, quantity, category,   =    unit_label, in_stock(bool), category
    expiryDate, imageUrl             =    (not public), image_url

collectionGroup('drugs') search      →  GET /products/search?q=
popular drugs (client aggregate)     →  GET /products/popular
```

---

## 9. Suggested Implementation Order

**Phase 1 — Schema (PharmaTrack)**
Add `pharmacy` table; add `pharmacy_id`, price, and image columns to `product`; write migrations; seed a default pharmacy for existing data.

**Phase 2 — API (PharmaTrack)**
Add JWT `pharmacy_id` claim; scope existing endpoints; add `/pharmacies`, `/pharmacies/<id>/products`, `/products/search`, `/products/popular`; extend public `GET /products`.

**Phase 3 — Web app (PharmaTrack)**
Point Settings at the `pharmacy` row; persistent `SECRET_KEY`; CORS; DB-backed rate limiting.

**Phase 4 — Hosting**
Move to PostgreSQL; Dockerfile + compose; reverse proxy + HTTPS; env config.

**Phase 5 — Customer app**
Remove Firebase; add config/base URL; rewrite `PharmacyService`; adapt `Drug`, `Pharmacy`, `PopularDrug` models; keep cache/offline behavior.

**Phase 6 — Verification**
- PharmaTrack unit tests (`python -m unittest discover -s tests -v`) still pass.
- New endpoints covered by tests (auth scope, public safety of stock numbers).
- `flutter analyze` clean; manual test: search → list → detail → directions against the hosted API.

---

## 10. Open Questions to Decide

1. **One app, many pharmacies** — is the customer app meant to search *many independent pharmacies* each running PharmaTrack, or a *single hosted instance* serving all pharmacies' data in one database? The plan above assumes the latter.
2. **Pharmacy accounts** — do pharmacies each get an admin in the central DB and manage their own inventory via the web/desktop app pointed at the hosted server, or is inventory managed elsewhere?
3. **Exact stock** — the customer app previously showed "in stock / out of stock" only (it used `quantity > 0`), so hiding exact counts via the public API is acceptable. Confirm.
4. **Images** — PharmaTrack has no image hosting; adding `image_url` assumes images are uploaded elsewhere (Cloudinary/S3) and URLs stored.
5. **Currency** — the app displays FCFA; prices should be stored in FCFA.