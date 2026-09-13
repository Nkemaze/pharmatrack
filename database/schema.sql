-- PharmaTrack database schema (SQLite and PostgreSQL compatible)
-- Matches the ERD: Product -> ProductBatch -> StockMovement -> (optional) LossReport
-- IDs are UUID strings (TEXT), not auto-increment integers — required so records
-- created offline on different devices never collide when they sync later.
-- Note: `user` is a reserved word in PostgreSQL, so it is always quoted
-- ("user"); SQLite treats the quoting the same as the bare name.
-- datetime('now') is translated to to_char(now(),...) by the PostgreSQL adapter.

-- A pharmacy is a tenant in the hosted, multi-pharmacy deployment. The
-- desktop app works with exactly one "default" pharmacy row (seeded at
-- migration), while the hosted server keeps one row per registered pharmacy.
CREATE TABLE IF NOT EXISTS pharmacy (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    phone TEXT,
    email TEXT,                     -- contact email (profile / disclosures)
    license_number TEXT,
    emergency_phone TEXT,
    emergency_desc TEXT,
    latitude TEXT,
    longitude TEXT,
    opening_hours TEXT,             -- JSON: weekdayOpen, weekdayClose, weekendOpen, weekendClose
    status TEXT NOT NULL DEFAULT 'active',  -- active | pending | rejected | suspended
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product (
    id TEXT PRIMARY KEY,
    pharmacy_id TEXT,          -- owning tenant; NULL only for legacy rows pre-migration
    name TEXT NOT NULL,
    category TEXT,
    strength TEXT,
    dosage_form TEXT,
    barcode TEXT,              -- NOT globally unique: two pharmacies can stock the same
                               -- GTIN barcode; uniqueness is enforced per pharmacy below
    requires_prescription INTEGER NOT NULL DEFAULT 0,  -- 0 = false, 1 = true
    is_controlled INTEGER NOT NULL DEFAULT 0,
    price_per_unit REAL,
    price_per_packet REAL,
    packet_size INTEGER,
    unit_label TEXT,
    image_url TEXT,
    low_stock_threshold INTEGER,     -- NULL = use the global setting
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (pharmacy_id) REFERENCES pharmacy(id)
);

CREATE TABLE IF NOT EXISTS product_batch (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    batch_number TEXT,
    expiry_date TEXT,          -- stored as 'YYYY-MM-DD'
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (product_id) REFERENCES product(id)
);

CREATE TABLE IF NOT EXISTS "user" (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT,
    device_id TEXT,
    password_hash TEXT,
    pharmacy_id TEXT,               -- the tenant this login belongs to
    status TEXT NOT NULL DEFAULT 'active',  -- active | pending | suspended
    must_update_profile INTEGER NOT NULL DEFAULT 0,  -- 1 = force profile setup on first login
    FOREIGN KEY (pharmacy_id) REFERENCES pharmacy(id)
);

CREATE TABLE IF NOT EXISTS stock_movement (
    id TEXT PRIMARY KEY,
    product_batch_id TEXT NOT NULL,
    movement_type TEXT NOT NULL,      -- receipt | sale | adjustment | transfer | return | destruction | loss
    quantity INTEGER NOT NULL,         -- signed: +in, -out
    counterparty_name TEXT,
    counterparty_address TEXT,
    reference_number TEXT,
    prescription_number TEXT,
    performed_by_user_id TEXT,
    device_id TEXT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT,
    approved_by_user_id TEXT,
    synced_at TEXT,             -- NULL until pushed to the remote server
    FOREIGN KEY (product_batch_id) REFERENCES product_batch(id),
    FOREIGN KEY (performed_by_user_id) REFERENCES "user"(id),
    FOREIGN KEY (approved_by_user_id) REFERENCES "user"(id)
);

CREATE TABLE IF NOT EXISTS loss_report (
    id TEXT PRIMARY KEY,
    stock_movement_id TEXT NOT NULL,
    circumstances TEXT NOT NULL,
    reported_to_authority_at TEXT,   -- NULL = not yet reported
    authority_reference TEXT,
    FOREIGN KEY (stock_movement_id) REFERENCES stock_movement(id)
);

-- Revoked access/refresh tokens. This lets a user sign out before a token
-- naturally expires, and lets an admin invalidate a compromised token.
CREATE TABLE IF NOT EXISTS token_blocklist (
    jti TEXT PRIMARY KEY,
    token_type TEXT NOT NULL,
    user_id TEXT NOT NULL,
    expires_at BIGINT NOT NULL,     -- unix seconds; BIGINT = 64-bit on both engines
    revoked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Failed login tracking (web UI and API) as a durable, DB-backed rate
-- limiter - survives restarts and works across multiple web workers where
-- an in-memory dict would give each process its own threshold.
CREATE TABLE IF NOT EXISTS login_attempt (
    scope TEXT NOT NULL,            -- 'web' (by username) or 'api' (by IP)
    key TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    first_attempt_at BIGINT NOT NULL DEFAULT 0,  -- unix seconds
    PRIMARY KEY (scope, key)
);

-- Helpful indexes for the queries the UI will actually run
CREATE INDEX IF NOT EXISTS idx_batch_product ON product_batch(product_id);
CREATE INDEX IF NOT EXISTS idx_product_barcode ON product(barcode);
CREATE INDEX IF NOT EXISTS idx_movement_batch ON stock_movement(product_batch_id);
CREATE INDEX IF NOT EXISTS idx_movement_synced ON stock_movement(synced_at);
CREATE INDEX IF NOT EXISTS idx_loss_movement ON loss_report(stock_movement_id);
CREATE INDEX IF NOT EXISTS idx_blocklist_expires ON token_blocklist(expires_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Portable upsert: ON CONFLICT DO NOTHING works on SQLite 3.24+ and PostgreSQL
INSERT INTO settings (key, value) VALUES ('low_stock_threshold', '10')
  ON CONFLICT(key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('pharmacy_name', 'PharmaTrack Pharmacy')
  ON CONFLICT(key) DO NOTHING;
