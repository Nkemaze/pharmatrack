"""
Database helper for PharmaTrack.

Run this file directly once to create pharmacy.db and set up the tables:
    python database/db.py

After that, import get_db_connection() from your Flask routes to query it.

Packaging note: when this runs as a normal Python script, paths are relative
to this file. When PyInstaller bundles it into a .exe, bundled files (like
schema.sql) get extracted to a temporary folder that disappears when the app
closes - so pharmacy.db must NOT live there, or your data would vanish every
time you close the app. Instead, the actual database file is kept next to
the .exe itself, which persists normally.
"""

import os
import sys


def _is_postgres():
    """True when DATABASE_URL points at the hosted PostgreSQL database.
    The desktop build and default test runs keep using the local SQLite file."""
    return bool(os.environ.get('DATABASE_URL'))


def _get_base_dir():
    """Folder the .exe lives in (or the project folder, when not packaged) -
    this is where pharmacy.db is kept, so it persists between runs."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_resource_dir():
    """Folder where PyInstaller extracts bundled read-only files (like
    schema.sql) at runtime. Falls back to the normal project folder when
    not packaged."""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


# Tests and deployment can provide a separate database path. Normal desktop
# use still defaults to pharmacy.db next to the application.
DB_PATH = os.environ.get('PHARMATRACK_DB_PATH') or os.path.join(_get_base_dir(), 'pharmacy.db')
SCHEMA_PATH = os.path.join(_get_resource_dir(), 'database', 'schema.sql') \
    if getattr(sys, 'frozen', False) \
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')


def get_db_connection():
    """Opens a connection to pharmacy.db (SQLite) or the PostgreSQL database
    named by DATABASE_URL. Caller is responsible for closing it."""
    if _is_postgres():
        import psycopg
        try:
            from database.pg_adapter import PGConnection
        except ModuleNotFoundError:  # running as `python database/db.py`
            from pg_adapter import PGConnection
        raw = psycopg.connect(os.environ['DATABASE_URL'])
        return PGConnection(raw)

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    # SQLite disables foreign-key enforcement by default; it must be enabled
    # separately for every connection.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # lets you access columns by name, e.g. row['name']
    return conn


def init_db():
    """Creates all tables if they don't already exist. Safe to run multiple times."""
    schema_path = SCHEMA_PATH
    if os.path.isdir(schema_path):
        # Defensive fallback: if a packaging mistake made this a folder
        # containing schema.sql instead of the file itself, look inside it.
        candidate = os.path.join(schema_path, 'schema.sql')
        if os.path.isfile(candidate):
            schema_path = candidate
        else:
            raise FileNotFoundError(
                f"Expected schema.sql but found a folder at {schema_path} "
                f"with no schema.sql inside it. Check the --add-data path used when packaging."
            )

    conn = get_db_connection()
    with open(schema_path, 'r') as f:
        conn.executescript(f.read())
    conn.commit()
    _run_migrations(conn)
    conn.close()
    print(f"Database ready at {DB_PATH}")


def _table_columns(conn, table):
    """Returns the set of column names present on a table (works on old DBs)."""
    if _is_postgres():
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        return {row["column_name"] for row in cur.fetchall()}
    import sqlite3
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(%s)" % table)
    return {row[1] for row in cur.fetchall()}


def _add_column(conn, table, column, declaration):
    """Adds a column to an existing table if it is missing. Safe to re-run."""
    if column not in _table_columns(conn, table):
        if _is_postgres():
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {declaration}')
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.commit()


def _ensure_default_pharmacy(conn):
    """Seeds a single default pharmacy so a table created before the pharmacy
    table existed (the classic desktop install) becomes a valid single-tenant
    database. Any legacy product/user rows with no pharmacy get re-homed here."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM pharmacy ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    if row is not None:
        return row["id"]

    import uuid as _uuid

    cur.execute("SELECT value FROM settings WHERE key = 'pharmacy_name'")
    row = cur.fetchone()
    name = row["value"] if row else None

    pharmacy_id = str(_uuid.uuid4())
    cur.execute(
        "INSERT INTO pharmacy (id, name, status) VALUES (?, ?, 'active')",
        (pharmacy_id, name or 'PharmaTrack Pharmacy'),
    )
    conn.commit()

    cur.execute("UPDATE product SET pharmacy_id = ? WHERE pharmacy_id IS NULL",
                (pharmacy_id,))
    cur.execute('UPDATE "user" SET pharmacy_id = ?, status = \'active\' WHERE pharmacy_id IS NULL',
                (pharmacy_id,))
    conn.commit()
    print(f"Seeded default pharmacy {pharmacy_id!r}")
    return pharmacy_id


def _run_migrations(conn):
    """
    Adds columns that later versions of the schema need, to databases
    created by an earlier version. CREATE TABLE IF NOT EXISTS (in schema.sql)
    only helps for brand-new databases - it does nothing for a table that
    already exists without a newer column. Safe to run every startup:
    each check only adds a column if it's actually missing.
    """
    _add_column(conn, "user", "pharmacy_id", "TEXT REFERENCES pharmacy(id)")
    _add_column(conn, "user", "status", "TEXT NOT NULL DEFAULT 'active'")
    _add_column(conn, "user", "must_update_profile", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "pharmacy", "email", "TEXT")
    _add_column(conn, "pharmacy", "license_number", "TEXT")
    _add_column(conn, "pharmacy", "state", "TEXT")
    _add_column(conn, "pharmacy", "zip_code", "TEXT")
    _add_column(conn, "pharmacy", "emergency_desc", "TEXT")
    _add_column(conn, "product", "pharmacy_id", "TEXT REFERENCES pharmacy(id)")
    _add_column(conn, "product", "price_per_unit", "REAL")
    _add_column(conn, "product", "price_per_packet", "REAL")
    _add_column(conn, "product", "packet_size", "INTEGER")
    _add_column(conn, "product", "unit_label", "TEXT")
    _add_column(conn, "product", "image_url", "TEXT")

    # Existing installs from before logins gained passwords.
    _add_column(conn, "user", "password_hash", "TEXT")

    # Indexes on the new tenant columns. Kept out of schema.sql because an
    # old database executes the script before these columns exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_pharmacy ON product(pharmacy_id)")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_user_pharmacy ON "user"(pharmacy_id)')

    # Barcode uniqueness is now per-pharmacy (two pharmacies may stock the
    # same GTIN barcode). Created here because the column may not exist when
    # schema.sql runs on an old database.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_product_barcode_pharmacy "
        "ON product(pharmacy_id, barcode)"
    )
    conn.commit()

    # Desktop installs upgrade to a single default tenant automatically.
    _ensure_default_pharmacy(conn)


if __name__ == '__main__':
    init_db()
