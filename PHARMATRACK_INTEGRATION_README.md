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
| `pharmatrack/` web UI (templates + static) | Flask + Tailwind, served by the app | Same database |
| `customer_mobile_app/` | Flutter (Android/iOS/web) | Firebase Auth + Cloud Firestore |

> Note: the old `pharmacy_web_app/` directory no longer exists. The pharmacy UI
> is now the Flask templates inside `pharmatrack/` (login, dashboard, products,
> movements, loss reports, settings), served by the same process as the API.

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

Implemented via **option 3**: `database/db.py` remains the abstraction; when the
`DATABASE_URL` env var is set, `get_db_connection()` returns a `PGConnection`
(from `database/pg_adapter.py`) instead of a plain `sqlite3.Connection`.  The
adapter transparently translates SQLite-flavoured statements (`?` placeholders,
`datetime('now')`, `LIKE` → `ILIKE`, `BEGIN IMMEDIATE` → no-op) so the rest
of the query layer (`queries.py`, `api/`, `app.py`) remains backend-agnostic.

Other portability changes applied directly in the source SQL:
- `"user"` is double-quoted everywhere (`user` is a Postgres reserved word;
  SQLite treats `"user"` as the same bare identifier).
- `INSERT OR IGNORE INTO …` replaced with portable `INSERT INTO … ON
  CONFLICT(col) DO NOTHING` (SQLite ≥3.24 and Postgres both accept this).
- `expires_at` and `first_attempt_at` columns changed to `BIGINT` in
  `schema.sql` (SQLite's `INTEGER` is already 64-bit; Postgres `INTEGER` is
  32-bit int4, which overflows near 2038 for unix-second timestamps).
- `GROUP BY` clauses now include all non-aggregated selected columns
  (`GROUP BY p.id, ph.name`) — required by Postgres.

Postgres schema is created by `init_db()` reading the same `schema.sql`;
the adapter strips `--` comments before splitting on `;` to avoid
spurious statement boundaries.

Tests: 9 SQLite unit tests all pass. Full query-layer smoke test verified
against a local PostgreSQL 17 Docker container (`pharma_pg`, port 5433).

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

### 6.0 What PostgreSQL is going to be used for

PostgreSQL is the **proposed replacement for SQLite** in production. The current
app reads/writes one local `pharmacy.db` file. That works on a desktop, but not
on a hosted multi-user server:

- SQLite is a single write-lock file: concurrent pharmacies/clients serialize on
  writes and it will bottleneck.
- On serverless platforms the filesystem is **ephemeral** — a SQLite file written
  during one request can vanish (or never be shared) on the next. Vercel
  explicitly does **not** support SQLite (only read-only bundling is possible).
- PostgreSQL is a real server database: concurrent connections, longer-lived,
  and it is the storage backend both Vercel (via Neon) and Render offer natively.

So the plan: keep SQLite for the local desktop app (fast, zero-setup), and point
the hosted **API** at a managed PostgreSQL database via a `DATABASE_URL` env var.
The schema/queries are ported once (see §3.4); each install chooses its engine.

### 6.1 Hosting target: Render (API + PostgreSQL)

**Render is the correct home for the Flask API and its database.**

| Concern | Render |
|---|---|
| Flask long-running process | Yes — a Web Service runs `gunicorn wsgi:app` 24/7 |
| SQLite survives? | Not needed — Render manages PostgreSQL |
| Managed PostgreSQL | Yes, first-class (`New > PostgreSQL`), internal + external URLs, SSL |
| Free tier | Web service (spins down when idle) + Postgres (expires after ~30 days) |
| Deploy method | Git push, or `render.yaml` Blueprint (web + database + env vars in one file) |
| Environment variables | Dashboard or Blueprint |

Deployment shape (final state):

```text
Render
├── PostgreSQL  (managed, DATABASE_URL auto-wired)
└── Web Service → gunicorn wsgi:app   (serves BOTH the UI and /api/v1)
    env: PHARMATRACK_ENV=production, JWT_SECRET_KEY, SECRET_KEY, CORS_ORIGINS
```

### 6.2 Hosting target: Render (only platform)

**Everything runs on Render — no Vercel.**

| Piece | Platform |
|---|---|
| `pharmatrack` web UI (templates) | Render Web Service |
| Flask API (`/api/v1`) | Render Web Service (same process) |
| PostgreSQL | Render Postgres |
| `customer_mobile_app` APK download | Render Web Service (static route) |
| `customer_mobile_app` web build | optional; same Render web service or right-click deploy later |

The long-running Flask process and its DB stay on Render; nothing is split to
another provider.

### 6.3 Hosting the APK download link

The customer app is distributed as a downloadable **APK served from Render**, so
people install it directly from a link (no app store required).

Build once, serve forever:

```text
flutter build apk --release
→ build/app/outputs/flutter-apk/app-release.apk  (one-file, self-contained)
```

Delivered via the existing Flask app — add a small static route (e.g.
`GET /download` and `GET /apk/latest`, or drop the file in `static/`):

- Flask serves the APK with `send_file(..., as_attachment=True,
  download_name="pharmafinder-vX.Y.Z.apk")`.
- `Content-Disposition: attachment` forces a download; the link is shared as
  `https://<render-url>/download`.
- A tiny landing page (`/download`) on the pharmatrack UI shows the app name,
  version, and a "Download Android App" button pointing at the APK.

APK build notes:

- **Signing** — `android/app/build.gradle.kts:35` currently signs releases with
  the debug key. That's fine for a personal link, but for updates you must keep
  the same key, or every install becomes a "different app".
  - Preferred: generate a release keystore (`keytool -genkey ...`), configure
    `signingConfigs.release`, use one `android/app/upload-keystore.jks`. Android
    will then treat future builds as updates to the same app.
- **Version** — set `versionName`/`versionCode` in `pubspec.yaml` and bump on
  every rebuild so users get an update instead of a "reinstall".
- **Targeting** — `minSdk = flutter.minSdkVersion` (default 21). Cover ~100% of
  devices; only needed if offline-map/cache plugins require higher.
- **APK size** — a release APK with offline maps + routing runs roughly
  30–60 MB; on Render's free/Hobby web service disk that's fine. The render
  service must not suspend while an APK download is in flight — a paid instance
  (or `suspend` disabled) avoids mid-transfer drops.
- **FlutterFire cleanup** — the Firebase Google Services plugin is still applied
  in `android/app/build.gradle.kts:4`; remove it when Firebase is stripped from
  the app (§7.1) and regenerate if needed.

### 6.4 render.yaml (new infra files, not created yet)

- `render.yaml` — Blueprint: web service (`gunicorn wsgi:app`) + managed Postgres
  + env vars (`PHARMATRACK_ENV`, `JWT_SECRET_KEY`, `SECRET_KEY`, `CORS_ORIGINS`).
- `.env.example` — same variables for local/docker use.
- `requirements.txt` additions — `gunicorn`, `psycopg[binary]` (or SQLAlchemy),
  `flask-cors`.
- `scripts/build_apk.sh` — one command pipeline: `flutter build apk --release`,
  copy the APK into `pharmatrack/static/` (or a versioned `downloads/` folder),
  tag the version.

Production environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `PHARMATRACK_ENV` | yes | `production` (app refuses to start otherwise) |
| `JWT_SECRET_KEY` | yes | Persistent JWT signing secret |
| `SECRET_KEY` | yes (new) | Persistent session cookie secret |
| `DATABASE_URL` | yes (new, on Render) | Postgres connection string |
| `JWT_ACCESS_TOKEN_MINUTES` | no | Default 30 |
| `JWT_REFRESH_TOKEN_DAYS` | no | Default 30 |
| `CORS_ORIGINS` | no | Comma-separated allowed origins (customer Flutter app builds) |

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
Create `render.yaml` Blueprint (Web Service + managed Postgres + env vars); port queries to PostgreSQL (`DATABASE_URL`); deploy the whole `pharmatrack` app (UI + API) to **Render**; configure `CORS_ORIGINS`; add `/download` route; build the APK and host it on Render for direct download.

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

---

## 11. TODO Checklist

- [x] **Phase 1 — Schema (PharmaTrack)**
  - [x] Add `pharmacy` table to `database/schema.sql`
  - [x] Add `pharmacy_id`, `price_per_unit`, `price_per_packet`, `packet_size`, `unit_label`, `image_url` columns to `product` table
  - [x] Write migrations in `database/db.py:_run_migrations()` for the new columns/table
  - [x] Seed a default pharmacy and assign existing products to it

- [x] **Phase 2 — API (PharmaTrack)**
  - [x] Add `pharmacy_id` to JWT claims (`api/auth.py`)
  - [x] Scope all existing product/movement/report endpoints by `pharmacy_id`
  - [x] Add `GET /api/v1/pharmacies` (list active pharmacies, public)
  - [x] Add `GET /api/v1/pharmacies/<id>` (single pharmacy, public)
  - [x] Add `GET /api/v1/pharmacies/<id>/products` (pharmacy's product list, public)
  - [x] Add `GET /api/v1/products/search?q=` (cross-pharmacy search, public)
  - [x] Add `GET /api/v1/products/popular` (popular medicines aggregation, public)
  - [x] Extend `GET /api/v1/products` to include prices, images, and pharmacy name

- [x] **Phase 3 — Web app (PharmaTrack)**
  - [x] Update Settings page to write the new `pharmacy` table fields (city, phone, hours, status)
  - [x] Set `SECRET_KEY` from env var instead of `os.urandom(32)`
  - [x] Add Flask-CORS with configurable allowed origins
  - [x] Move login rate limiter from in-memory to database-backed

- [x] **Phase 4 — Hosting / Deployment (Render)**
  - [x] Create `render.yaml` Blueprint (web service + managed Postgres + env vars)
  - [x] Create `.env.example` with all required production variables
  - [x] Add `gunicorn`, `psycopg[binary]`, `flask-cors` to `requirements.txt`
  - [x] Port all SQLite queries to PostgreSQL-compatible SQL (`ILIKE` etc.)
  - [x] Add `GET /download` route + landing page for APK download
  - [x] Add `GET /apk/latest` route serving the latest APK from `static/`
  - [x] Add `GET /api/v1/health` (Render health check)
  - [ ] Set `PHARMATRACK_ENV=production`, `JWT_SECRET_KEY`, `SECRET_KEY`, `CORS_ORIGINS` in Render env
  - [ ] Deploy and verify UI + API + Postgres connection on Render

- [x] **Phase 5 — Customer mobile app**
  - [x] Remove `firebase_core` and `cloud_firestore` from `pubspec.yaml`
  - [x] Remove Firebase init from `lib/main.dart`
  - [x] Create `lib/config.dart` with `API_BASE_URL` constant
  - [x] Rewrite `lib/services/pharmacy_service.dart` (Firestore → REST calls)
  - [x] Adapt `lib/models/drug.dart` to parse API JSON (snake_case fields)
  - [x] Adapt `lib/models/pharmacy.dart` to parse API JSON
  - [x] Adapt `lib/models/popular_drug.dart` to parse `/products/popular` response
  - [x] Add retry + offline fallback to `PharmacyService` (same style as `routing_service.dart`)

- [ ] **Phase 5a — Android build setup**
  - [x] Remove `com.google.gms.google-services` plugin from `android/app/build.gradle.kts` and `settings.gradle.kts`
  - [x] Remove `google-services.json` from `android/app/`
  - [ ] Generate a release signing keystore (`keytool -genkey`)
  - [ ] Configure `signingConfigs.release` in `build.gradle.kts` using the keystore
  - [ ] Bump `versionName` / `versionCode` in `pubspec.yaml`
  - [ ] Run `flutter build apk --release`
  - [ ] Copy APK into `pharmatrack/static/downloads/` or commit to repo

- [ ] **Phase 6 — Verification**
  - [ ] PharmaTrack existing unit tests still pass (`python -m unittest discover -s tests -v`)
  - [ ] New API endpoints covered by tests (auth scoping, public stock safety)
  - [ ] `flutter analyze` clean — no errors
  - [ ] Manual end-to-end: download APK → install → search drugs → list pharmacies → open detail → get directions, all against the hosted API
  - [ ] Verify CORS headers on API responses from a browser
  - [ ] Verify login, product management, and movement recording on hosted web UI