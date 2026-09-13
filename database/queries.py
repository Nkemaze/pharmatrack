"""
Query helpers — turns raw database rows into the shapes the templates need.
Keeps app.py focused on routing, not SQL.
"""

from datetime import datetime, date
import time
from database.db import get_db_connection


def _parse_iso_date(value):
    """Parse a stored 'YYYY-MM-DD' date into a date object. Never raises:
    returns None for empty, null, or malformed values so one dirty batch row
    can't take down the dashboard."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def revoke_token(jti, token_type, user_id, expires_at):
    """Store a JWT identifier so it can no longer be used."""
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO token_blocklist (jti, token_type, user_id, expires_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(jti) DO NOTHING""",
            (jti, token_type, user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def is_token_revoked(jti):
    """Return whether a JWT identifier has been revoked."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM token_blocklist WHERE jti = ?", (jti,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_pharmacies():
    """All pharmacy tenants, for admin management and public discovery."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM pharmacy ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_public_pharmacies():
    """Active pharmacies only, shaped for the customer app (operating
    hours surfaced as a parsed object, no admin fields)."""
    from json import loads
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM pharmacy WHERE status = 'active' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r, opening_hours=_parse_hours(r["opening_hours"])) for r in rows]


def get_public_pharmacy(pharmacy_id):
    """One active pharmacy shaped for the customer app, or None."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pharmacy WHERE id = ? AND status = 'active'",
            (pharmacy_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = dict(row)
    result["opening_hours"] = _parse_hours(result["opening_hours"])
    return result


def _parse_hours(raw):
    """opening_hours is stored as JSON text; surface it as a dict (or {})."""
    from json import loads
    if not raw:
        return {}
    try:
        return loads(raw)
    except (TypeError, ValueError):
        return {}


def get_pharmacy(pharmacy_id):
    """One pharmacy tenant, or None."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM pharmacy WHERE id = ?", (pharmacy_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def create_pharmacy(name, address=None, city=None, phone=None,
                    emergency_phone=None, latitude=None, longitude=None,
                    opening_hours=None, status='active'):
    """Registers a new pharmacy tenant (used by the hosted self-registration
    flow, and by tests). Returns the new pharmacy id."""
    import uuid
    conn = get_db_connection()
    try:
        pharmacy_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO pharmacy
               (id, name, address, city, phone, emergency_phone, latitude,
                longitude, opening_hours, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pharmacy_id, name, address, city, phone, emergency_phone,
             latitude, longitude, opening_hours, status)
        )
        conn.commit()
        return pharmacy_id
    finally:
        conn.close()


def update_pharmacy(pharmacy_id, **fields):
    """Updates the mutable profile fields of a pharmacy tenant. Only keys
    present in fields are changed."""
    allowed = {"name", "address", "city", "phone", "emergency_phone",
               "latitude", "longitude", "opening_hours", "status"}
    sets = [f"{k} = ?" for k in fields if k in allowed]
    if not sets:
        return
    values = [fields[k] for k in fields if k in allowed] + [pharmacy_id]
    conn = get_db_connection()
    try:
        conn.execute(f"UPDATE pharmacy SET {', '.join(sets)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def create_pharmacy_registration(name, email, password, address=None, city=None,
                                 phone=None, emergency_phone=None, opening_hours=None):
    """Self-registered pharmacy application (hosted flow).

    Creates a pending pharmacy tenant plus a pending pharmacist login for the
    applicant. The account cannot sign in until an administrator approves the
    application (which flips both statuses to 'active'). Returns the new
    pharmacy id."""
    import uuid
    from werkzeug.security import generate_password_hash

    name = str(name).strip()
    email = str(email).strip()
    if not name or not email:
        raise ValueError("Pharmacy name and contact email are required.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    conn = get_db_connection()
    try:
        pharmacy_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO pharmacy
               (id, name, address, city, phone, emergency_phone, opening_hours, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (pharmacy_id, name, address, city, phone, emergency_phone, opening_hours)
        )
        user_id = str(uuid.uuid4())
        conn.execute(
            'INSERT INTO "user" (id, name, role, password_hash, pharmacy_id, status) '
            "VALUES (?, ?, 'pharmacist', ?, ?, 'pending')",
            (user_id, email, generate_password_hash(password), pharmacy_id)
        )
        conn.commit()
        return pharmacy_id
    finally:
        conn.close()


def get_pharmacies_with_applicant():
    """Every pharmacy tenant with its first pharmacist applicant, sorted
    pending-first - the data backing the admin's pharmacy management page."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT p.id, p.name, p.address, p.city, p.phone,
                      p.status, p.created_at,
                      (SELECT u.name FROM "user" u
                        WHERE u.pharmacy_id = p.id AND u.role = 'pharmacist'
                        ORDER BY u.id LIMIT 1) AS applicant_name,
                      (SELECT u.status FROM "user" u
                        WHERE u.pharmacy_id = p.id AND u.role = 'pharmacist'
                        ORDER BY u.id LIMIT 1) AS applicant_status
               FROM pharmacy p
               ORDER BY
                 CASE p.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1
                               WHEN 'suspended' THEN 2 ELSE 3 END,
                 p.name"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_pending_pharmacy_count():
    """Number of pharmacy applications awaiting approval."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pharmacy WHERE status = 'pending'"
        ).fetchone()
    finally:
        conn.close()
    return row["n"] if row else 0


def update_pharmacy_status(pharmacy_id, status, user_status=None):
    """Sets a pharmacy's approval status. When user_status is given, every
    account of that pharmacy is set to it at the same time - the admin's
    Approve/Suspend/Reactivate controls stay in lock-step with the tenant."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE pharmacy SET status = ? WHERE id = ?", (status, pharmacy_id))
        if user_status is not None:
            conn.execute(
                'UPDATE "user" SET status = ? WHERE pharmacy_id = ?',
                (user_status, pharmacy_id),
            )
        conn.commit()
    finally:
        conn.close()


def delete_pharmacy_application(pharmacy_id):
    """Removes a pending/rejected application (its users and the pharmacy
    row). Refuses to delete an active or suspended tenant that may hold
    inventory. Returns True if the application was removed."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT status FROM pharmacy WHERE id = ?", (pharmacy_id,)
        ).fetchone()
        if row is None or row["status"] not in ("pending", "rejected"):
            return False
        conn.execute('DELETE FROM "user" WHERE pharmacy_id = ?', (pharmacy_id,))
        conn.execute("DELETE FROM pharmacy WHERE id = ?", (pharmacy_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def login_status_block(user):
    """Returns a displayable message if an account must stay signed out
    (pending approval, suspended or rejected), else None. Used by both the
    web /login route and the JSON API login so one rule gates both doorways.

    The pharmacy tenant's status is the stronger signal and is checked first
    so a rejected or suspended pharmacy gets that message even when its
    applicant user account still carries a legacy 'pending' flag."""
    status = (user or {}).get('status')
    pharmacy_status = (user or {}).get('pharmacy_status')
    if pharmacy_status == 'rejected':
        return ('This pharmacy registration was not approved. '
                'Contact your administrator.')
    if status == 'rejected':
        return ('This pharmacy registration was not approved. '
                'Contact your administrator.')
    if pharmacy_status == 'suspended':
        return 'This account has been suspended. Contact your administrator.'
    if status == 'suspended':
        return 'This account has been suspended. Contact your administrator.'
    if pharmacy_status == 'pending':
        return ('Your pharmacy registration is awaiting approval. You will be '
                'able to sign in once an administrator approves it.')
    if status == 'pending':
        return ('Your pharmacy registration is awaiting approval. You will be '
                'able to sign in once an administrator approves it.')
    return None


def get_product_list(search=None, pharmacy_id=None):
    """
    Returns one row per product with:
    - current_stock: computed by summing all movements across all its batches
    - expiry_status: 'Fine' / 'Expiring Soon' / 'Expired', based on the nearest batch expiry

    If search is given, only products whose name or a batch number contains
    that text (case-insensitive) are returned. If pharmacy_id is given, only
    products owned by that tenant are returned.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    where = ""
    params = ()
    if pharmacy_id:
        where = " WHERE p.pharmacy_id = ?"
        params = (pharmacy_id,)
    if search:
        like_term = f"%{search}%"
        if where:
            where += " AND (p.name LIKE ? OR pb.batch_number LIKE ?)"
            params += (like_term, like_term)
        else:
            where = " WHERE p.name LIKE ? OR pb.batch_number LIKE ?"
            params = (like_term, like_term)
    cur.execute(f"""
        SELECT p.id, p.pharmacy_id, p.name, p.category, p.dosage_form,
               ph.name AS pharmacy_name,
               COALESCE(SUM(sm.quantity), 0) AS current_stock,
               MIN(pb.expiry_date) AS nearest_expiry
        FROM product p
        JOIN pharmacy ph ON ph.id = p.pharmacy_id
        LEFT JOIN product_batch pb ON pb.product_id = p.id
        LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
        {where}
        GROUP BY p.id, ph.name
        ORDER BY p.name
    """, params)
    rows = cur.fetchall()
    conn.close()

    low_stock_threshold = int(get_setting("low_stock_threshold", "100"))
    today = date.today()
    products = []
    for row in rows:
        expiry_status = "Fine"
        if row["nearest_expiry"]:
            expiry_date = _parse_iso_date(row["nearest_expiry"])
            if expiry_date is not None:
                days_left = (expiry_date - today).days
                if days_left < 0:
                    expiry_status = "Expired"
                elif days_left <= 90:
                    expiry_status = "Expiring Soon"

        products.append({
            "id": row["id"],
            "pharmacy_id": row["pharmacy_id"],
            "pharmacy_name": row["pharmacy_name"] or "—",
            "name": row["name"],
            "category": row["category"] or "—",
            "dosage_form": row["dosage_form"] or "—",
            "current_stock": row["current_stock"],
            "is_low_stock": row["current_stock"] < low_stock_threshold,
            "expiry_status": expiry_status,
        })
    return products

def get_public_inventory(search=None, pharmacy_id=None):
    """Returns safe product information for the public/read-only API.

    Controlled substances and exact stock quantities are deliberately
    excluded - only whether a product is in stock at all. Keep this
    separate from get_product_list so callers cannot accidentally expose
    restricted fields by filtering a richer response.

    Optionally scoped to one pharmacy; otherwise spans every active tenant.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    base_query = """
        SELECT p.id, p.pharmacy_id, ph.name AS pharmacy_name,
               p.name, p.category, p.strength, p.dosage_form,
               p.requires_prescription, p.price_per_unit, p.price_per_packet,
               p.packet_size, p.unit_label, p.image_url,
               COALESCE(SUM(sm.quantity), 0) AS current_stock
        FROM product p
        JOIN pharmacy ph ON ph.id = p.pharmacy_id
        LEFT JOIN product_batch pb ON pb.product_id = p.id
        LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
        WHERE p.is_controlled = 0 AND ph.status = 'active'
    """
    filters = []
    params = ()
    if pharmacy_id:
        filters.append("p.pharmacy_id = ?")
        params += (pharmacy_id,)
    if search:
        like_term = f"%{search}%"
        filters.append("p.name LIKE ?")
        params += (like_term,)
    if filters:
        base_query += " AND " + " AND ".join(filters)
    cur.execute(base_query + " GROUP BY p.id, ph.name ORDER BY p.name", params)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": row["id"],
            "pharmacy_id": row["pharmacy_id"],
            "pharmacy_name": row["pharmacy_name"] or "—",
            "name": row["name"],
            "category": row["category"] or "—",
            "strength": row["strength"] or "—",
            "dosage_form": row["dosage_form"] or "—",
            "requires_prescription": bool(row["requires_prescription"]),
            "price_per_unit": row["price_per_unit"],
            "price_per_packet": row["price_per_packet"],
            "packet_size": row["packet_size"],
            "unit_label": row["unit_label"] or "unit",
            "image_url": row["image_url"],
            "in_stock": row["current_stock"] > 0,
        }
        for row in rows
    ]


def get_popular_products(limit=50):
    """An aggregate view across active pharmacies, powering the customer
    app's 'Popular Medicines' list: for each drug name, the cheapest unit
    price available, how many pharmacies carry it, and whether any has it
    in stock. Controlled substances are excluded."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT sq.name, MIN(sq.price_per_unit) AS cheapest_price,
               COUNT(DISTINCT sq.pharmacy_id) AS pharmacy_count,
               MAX(sq.in_stock_flag) AS any_in_stock
        FROM (
            SELECT p.name, p.price_per_unit, p.pharmacy_id,
                   CASE WHEN COALESCE(SUM(sm.quantity), 0) > 0 THEN 1 ELSE 0 END AS in_stock_flag
            FROM product p
            JOIN pharmacy ph ON ph.id = p.pharmacy_id
            LEFT JOIN product_batch pb ON pb.product_id = p.id
            LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
            WHERE p.is_controlled = 0 AND ph.status = 'active'
              AND p.price_per_unit IS NOT NULL
            GROUP BY p.id
        ) sq
        GROUP BY sq.name
        ORDER BY COUNT(DISTINCT sq.pharmacy_id) DESC, sq.name ASC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "name": r["name"],
            "form_label": None,
            "cheapest_price": r["cheapest_price"],
            "pharmacy_count": r["pharmacy_count"],
            "any_in_stock": bool(r["any_in_stock"]),
        }
        for r in rows
    ]

def get_product_detail(product_id, pharmacy_id=None):
    """
    Returns full detail for one product: its info, every batch with
    computed remaining quantity + expiry status, and its 10 most recent
    stock movements. Returns None if the product doesn't exist (or isn't
    owned by the given pharmacy).
    """
    conn = get_db_connection()
    cur = conn.cursor()

    if pharmacy_id:
        cur.execute("SELECT * FROM product WHERE id = ? AND pharmacy_id = ?",
                    (product_id, pharmacy_id))
    else:
        cur.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product_row = cur.fetchone()
    if product_row is None:
        conn.close()
        return None

    cur.execute("""
        SELECT pb.id, pb.batch_number, pb.expiry_date, pb.received_at,
               COALESCE(SUM(sm.quantity), 0) AS quantity_remaining
        FROM product_batch pb
        LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
        WHERE pb.product_id = ?
        GROUP BY pb.id
        ORDER BY pb.expiry_date ASC
    """, (product_id,))
    batch_rows = cur.fetchall()

    today = date.today()
    batches = []
    total_stock = 0
    for b in batch_rows:
        qty = b["quantity_remaining"]
        total_stock += qty
        status = "Healthy"
        if b["expiry_date"]:
            expiry_date = _parse_iso_date(b["expiry_date"])
            if expiry_date is not None:
                days_left = (expiry_date - today).days
                if days_left < 0:
                    status = "Expired"
                elif days_left <= 90:
                    status = "Near Expiry"
        batches.append({
            "id": b["id"],
            "batch_number": b["batch_number"],
            "expiry_date": b["expiry_date"],
            "quantity_remaining": qty,
            "status": status,
        })

    cur.execute("""
        SELECT sm.movement_type, sm.quantity, sm.reference_number,
               sm.occurred_at, sm.counterparty_name, sm.reason
        FROM stock_movement sm
        JOIN product_batch pb ON pb.id = sm.product_batch_id
        WHERE pb.product_id = ?
        ORDER BY sm.occurred_at DESC
        LIMIT 10
    """, (product_id,))
    movements = [dict(m) for m in cur.fetchall()]

    conn.close()

    product = dict(product_row)
    product["total_stock"] = total_stock
    product["batches"] = batches
    product["movements"] = movements
    return product


def get_product_by_barcode(barcode):
    """Used when a barcode is scanned — if a product with this barcode
    already exists, return its id/name so the UI can warn instead of
    creating a duplicate product."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM product WHERE barcode = ?", (barcode,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


def create_product(name, category, strength, dosage_form, barcode,
                    requires_prescription, is_controlled,
                    batch_number, expiry_date, initial_quantity,
                    performed_by_user_id=None, pharmacy_id=None,
                    price_per_unit=None, price_per_packet=None,
                    packet_size=None, unit_label=None, image_url=None):
    """
    Creates a product, its first batch, and the initial 'receipt' movement
    that gives it starting stock — all in one transaction, so you never end
    up with a product that has no batch, or a batch with no stock movement.
    Returns the new product's id.
    """
    import uuid

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        product_id = str(uuid.uuid4())
        # Legacy/single-tenant calls don't pass a pharmacy; fall back to the
        # first active one so rows always land inside a tenant.
        if pharmacy_id is None:
            row = cur.execute(
                "SELECT id FROM pharmacy WHERE status = 'active' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            pharmacy_id = row["id"] if row else None
        cur.execute(
            """INSERT INTO product
               (id, pharmacy_id, name, category, strength, dosage_form, barcode,
                requires_prescription, is_controlled, price_per_unit,
                price_per_packet, packet_size, unit_label, image_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, pharmacy_id, name, category, strength, dosage_form,
             barcode or None, 1 if requires_prescription else 0,
             1 if is_controlled else 0, price_per_unit, price_per_packet,
             packet_size, unit_label, image_url)
        )

        batch_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO product_batch (id, product_id, batch_number, expiry_date)
               VALUES (?, ?, ?, ?)""",
            (batch_id, product_id, batch_number, expiry_date)
        )

        if initial_quantity and int(initial_quantity) > 0:
            cur.execute(
                """INSERT INTO stock_movement
                   (id, product_batch_id, movement_type, quantity, performed_by_user_id, reason)
                   VALUES (?, ?, 'receipt', ?, ?, ?)""",
                (str(uuid.uuid4()), batch_id, int(initial_quantity), performed_by_user_id,
                 "Initial stock on product creation")
            )

        conn.commit()
        return product_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_products_for_dropdown(pharmacy_id=None):
    """Minimal product list for the Record Movement product selector."""
    conn = get_db_connection()
    cur = conn.cursor()
    if pharmacy_id:
        cur.execute("SELECT id, name, is_controlled FROM product WHERE pharmacy_id = ? ORDER BY name",
                    (pharmacy_id,))
    else:
        cur.execute("SELECT id, name, is_controlled FROM product ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "is_controlled": bool(r["is_controlled"])} for r in rows]


def get_batches_for_product(product_id, pharmacy_id=None):
    """Batches for one product, with current remaining quantity, for the
    batch dropdown that populates after a product is selected."""
    conn = get_db_connection()
    cur = conn.cursor()
    if pharmacy_id:
        cur.execute("""
            SELECT pb.id, pb.batch_number, pb.expiry_date,
                   COALESCE(SUM(sm.quantity), 0) AS quantity_remaining
            FROM product_batch pb
            JOIN product p ON p.id = pb.product_id
            LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
            WHERE pb.product_id = ? AND p.pharmacy_id = ?
            GROUP BY pb.id
            ORDER BY pb.expiry_date ASC
        """, (product_id, pharmacy_id))
    else:
        cur.execute("""
            SELECT pb.id, pb.batch_number, pb.expiry_date,
                   COALESCE(SUM(sm.quantity), 0) AS quantity_remaining
            FROM product_batch pb
            LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
            WHERE pb.product_id = ?
            GROUP BY pb.id
            ORDER BY pb.expiry_date ASC
        """, (product_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r["id"], "batch_number": r["batch_number"],
         "expiry_date": r["expiry_date"], "quantity_remaining": r["quantity_remaining"]}
        for r in rows
    ]


# Movement types where the entered quantity is stored as negative (stock going out)
_OUTBOUND_TYPES = {"sale", "transfer", "destruction", "loss"}
# Movement types where the entered quantity is stored as positive (stock coming in)
_INBOUND_TYPES = {"receipt", "return"}


def create_movement(product_batch_id, movement_type, quantity, adjustment_direction=None,
                     counterparty_name=None, counterparty_address=None,
                     reference_number=None, prescription_number=None,
                     reason=None, performed_by_user_id=None, device_id=None):
    """
    Records one stock movement. Quantity is always entered as a positive
    number by the pharmacist; this function applies the correct sign based
    on movement_type, so current stock (SUM of quantities) stays correct.

    For 'adjustment', adjustment_direction ('add' or 'remove') decides the sign,
    since an adjustment can go either way.

    For 'loss', also creates a linked loss_report row (required for the
    Loi n°97/019 unreported-loss tracking).

    Returns the new stock_movement id.
    """
    import uuid

    quantity = int(quantity)
    if movement_type in _OUTBOUND_TYPES:
        signed_quantity = -abs(quantity)
    elif movement_type in _INBOUND_TYPES:
        signed_quantity = abs(quantity)
    elif movement_type == "adjustment":
        signed_quantity = abs(quantity) if adjustment_direction == "add" else -abs(quantity)
    else:
        raise ValueError(f"Unknown movement_type: {movement_type}")

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Serialize stock writes for this SQLite database. Without this, two
        # simultaneous sales could both read the same balance and oversell.
        cur.execute("BEGIN IMMEDIATE")
        if signed_quantity < 0:
            cur.execute(
                """SELECT COALESCE(SUM(quantity), 0) AS quantity_remaining
                   FROM stock_movement WHERE product_batch_id = ?""",
                (product_batch_id,),
            )
            quantity_remaining = cur.fetchone()["quantity_remaining"]
            if quantity_remaining + signed_quantity < 0:
                raise ValueError(
                    f"Insufficient stock. Only {quantity_remaining} unit(s) remain in this batch."
                )

        movement_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO stock_movement
               (id, product_batch_id, movement_type, quantity, counterparty_name,
                counterparty_address, reference_number, prescription_number,
                performed_by_user_id, device_id, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (movement_id, product_batch_id, movement_type, signed_quantity,
             counterparty_name, counterparty_address, reference_number,
             prescription_number, performed_by_user_id, device_id, reason)
        )

        if movement_type == "loss":
            cur.execute(
                """INSERT INTO loss_report (id, stock_movement_id, circumstances)
                   VALUES (?, ?, ?)""",
                (str(uuid.uuid4()), movement_id, reason or "No reason provided")
            )

        conn.commit()
        return movement_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_product_id_for_batch(batch_id):
    """Used after saving a movement, to know which product page to redirect to."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT product_id FROM product_batch WHERE id = ?", (batch_id,))
    row = cur.fetchone()
    conn.close()
    return row["product_id"] if row else None


def get_product_id_for_batch_owned_by(batch_id, pharmacy_id):
    """Like get_product_id_for_batch, but only if the batch's product belongs
    to the given pharmacy (used to scope API movements to a tenant)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT pb.product_id FROM product_batch pb
        JOIN product p ON p.id = pb.product_id
        WHERE pb.id = ? AND p.pharmacy_id = ?
    """, (batch_id, pharmacy_id))
    row = cur.fetchone()
    conn.close()
    return row["product_id"] if row else None


def get_loss_reports():
    """
    Real loss history for the Loss Reports screen: joins loss_report back to
    the movement, batch, and product that generated it. Also returns summary
    stats actually computable from real data (no fabricated numbers).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT lr.id AS loss_report_id, lr.circumstances, lr.reported_to_authority_at,
               lr.authority_reference,
               sm.occurred_at, sm.quantity, sm.reference_number,
               pb.batch_number,
               p.name AS product_name, p.is_controlled
        FROM loss_report lr
        JOIN stock_movement sm ON sm.id = lr.stock_movement_id
        JOIN product_batch pb ON pb.id = sm.product_batch_id
        JOIN product p ON p.id = pb.product_id
        ORDER BY sm.occurred_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    losses = []
    for r in rows:
        losses.append({
            "loss_report_id": r["loss_report_id"],
            "date": r["occurred_at"],
            "product_name": r["product_name"],
            "batch_number": r["batch_number"],
            "quantity": abs(r["quantity"]),
            "reason": r["circumstances"],
            "is_controlled": bool(r["is_controlled"]),
            "reported": r["reported_to_authority_at"] is not None,
            "reported_at": r["reported_to_authority_at"],
        })

    total_losses = len(losses)
    unreported_count = sum(1 for l in losses if not l["reported"])
    total_units_lost = sum(l["quantity"] for l in losses)

    # Most common reason - simple exact-text match count, honest given free-text reasons
    reason_counts = {}
    for l in losses:
        reason_counts[l["reason"]] = reason_counts.get(l["reason"], 0) + 1
    most_common_reason = max(reason_counts, key=reason_counts.get) if reason_counts else "—"

    return {
        "losses": losses,
        "total_losses": total_losses,
        "unreported_count": unreported_count,
        "total_units_lost": total_units_lost,
        "most_common_reason": most_common_reason,
    }


def mark_loss_reported(loss_report_id, authority_reference=None):
    """Marks a loss as reported to the authorities (Loi n°97/019 requirement)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """UPDATE loss_report
           SET reported_to_authority_at = datetime('now'), authority_reference = ?
           WHERE id = ?""",
        (authority_reference, loss_report_id)
    )
    conn.commit()
    conn.close()


LOW_STOCK_THRESHOLD = 100  # same threshold used for the red-highlight in product_list


def get_dashboard_data():
    """
    Real numbers for the dashboard: counts for the stat cards, the actual
    low-stock products, actual expiring/expired batches, and the most
    recent stock movements across all products.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    low_stock_threshold = int(get_setting("low_stock_threshold", "100"))

    # Total products
    cur.execute("SELECT COUNT(*) AS c FROM product")
    total_products = cur.fetchone()["c"]

    # Per-product current stock, to find low-stock ones
    cur.execute("""
        SELECT p.id, p.name, COALESCE(SUM(sm.quantity), 0) AS current_stock
        FROM product p
        LEFT JOIN product_batch pb ON pb.product_id = p.id
        LEFT JOIN stock_movement sm ON sm.product_batch_id = pb.id
        GROUP BY p.id
    """)
    stock_rows = cur.fetchall()
    low_stock_items = [
        {"id": r["id"], "name": r["name"], "current_stock": r["current_stock"],
         "threshold": low_stock_threshold}
        for r in stock_rows if r["current_stock"] < low_stock_threshold
    ]
    low_stock_items.sort(key=lambda x: x["current_stock"])

    # Batches that are expired or expiring within 90 days
    cur.execute("""
        SELECT pb.batch_number, pb.expiry_date, p.name AS product_name
        FROM product_batch pb
        JOIN product p ON p.id = pb.product_id
        WHERE pb.expiry_date IS NOT NULL
        ORDER BY pb.expiry_date ASC
    """)
    batch_rows = cur.fetchall()

    today = date.today()
    expiry_watch = []
    for b in batch_rows:
        expiry_date = _parse_iso_date(b["expiry_date"])
        if expiry_date is None:
            continue
        days_left = (expiry_date - today).days
        if days_left <= 90:
            expiry_watch.append({
                "product_name": b["product_name"],
                "batch_number": b["batch_number"],
                "expiry_date": b["expiry_date"],
                "days_left": days_left,
                "status": "Expired" if days_left < 0 else "Warning",
            })

    # Most recent 5 movements across all products
    cur.execute("""
        SELECT sm.movement_type, sm.quantity, sm.reference_number, sm.occurred_at,
               sm.counterparty_name, p.name AS product_name
        FROM stock_movement sm
        JOIN product_batch pb ON pb.id = sm.product_batch_id
        JOIN product p ON p.id = pb.product_id
        ORDER BY sm.occurred_at DESC
        LIMIT 5
    """)
    recent_movements = [dict(r) for r in cur.fetchall()]

    conn.close()

    unreported_losses_count = get_loss_reports()["unreported_count"]

    return {
        "total_products": total_products,
        "low_stock_items": low_stock_items,
        "low_stock_count": len(low_stock_items),
        "expiry_watch": expiry_watch,
        "expiring_soon_count": len(expiry_watch),
        "unreported_losses_count": unreported_losses_count,
        "recent_movements": recent_movements,
    }


def update_product(product_id, name, category, strength, dosage_form, barcode,
                    requires_prescription, is_controlled,
                    price_per_unit=None, price_per_packet=None,
                    packet_size=None, unit_label=None, image_url=None):
    """Updates a product's own fields. Does NOT touch batches or stock -
    those only ever change through Record Movement, by design."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """UPDATE product
           SET name = ?, category = ?, strength = ?, dosage_form = ?, barcode = ?,
               requires_prescription = ?, is_controlled = ?,
               price_per_unit = ?, price_per_packet = ?,
               packet_size = ?, unit_label = ?, image_url = ?
           WHERE id = ?""",
        (name, category, strength, dosage_form, barcode or None,
         1 if requires_prescription else 0, 1 if is_controlled else 0,
         price_per_unit, price_per_packet, packet_size, unit_label, image_url,
         product_id)
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_alert_count():
    """Lightweight combined count for the header notification badge -
    reuses the same real numbers as the dashboard (low stock + expiring +
    unreported losses), without building the full detail lists."""
    data = get_dashboard_data()
    return data["low_stock_count"] + data["expiring_soon_count"] + data["unreported_losses_count"]


def get_users():
    """All users, for the admin's account management page (no password data)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT id, name, role FROM "user" ORDER BY name')
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]


def get_user_by_id(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT id, name, role, pharmacy_id, status FROM "user" WHERE id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "role": row["role"],
            "pharmacy_id": row["pharmacy_id"], "status": row["status"]}


def admin_exists():
    """Used to enforce a single admin account system-wide: the 'admin'
    role option is only offered at registration (and in Manage Accounts)
    when this returns False."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM \"user\" WHERE role = 'admin' LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row is not None


def user_name_exists(name):
    """Used during self-registration to prevent two accounts sharing a name
    - login looks users up by name, so names must be unique."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM "user" WHERE name = ?', (name,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def login_attempt_state(scope, key):
    """Returns (count, first_attempt_at) for a login-ratelimit key, or
    (0, 0) when the key has no recorded attempts."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT count, first_attempt_at FROM login_attempt WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return (0, 0)
    return (row["count"], row["first_attempt_at"])


def record_login_attempt(scope, key, max_attempts, window_seconds):
    """
    Records one failed login for a ratelimit key ('web' by username,
    'api' by IP). When the window has fully elapsed since the recorded
    first attempt, the counter restarts at 1; otherwise it increments.
    The caller checks login_attempt_lockout() before this would matter.
    """
    now = int(time.time())
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT count, first_attempt_at FROM login_attempt WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO login_attempt (scope, key, count, first_attempt_at) VALUES (?, ?, 1, ?)",
                (scope, key, now),
            )
        elif now - row["first_attempt_at"] >= window_seconds:
            conn.execute(
                "UPDATE login_attempt SET count = 1, first_attempt_at = ? WHERE scope = ? AND key = ?",
                (now, scope, key),
            )
        else:
            conn.execute(
                "UPDATE login_attempt SET count = count + 1 WHERE scope = ? AND key = ?",
                (scope, key),
            )
        conn.commit()
    finally:
        conn.close()


def login_attempt_lockout(scope, key, max_attempts, window_seconds):
    """Seconds remaining in a lockout for a key, or 0 if it may try again."""
    count, first_attempt_at = login_attempt_state(scope, key)
    if count < max_attempts:
        return 0
    remaining = int(window_seconds - (time.time() - first_attempt_at))
    return remaining if remaining > 0 else 0


def clear_login_attempts(scope, key):
    """Resets a key after a successful login."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM login_attempt WHERE scope = ? AND key = ?", (scope, key))
        conn.commit()
    finally:
        conn.close()


def create_user(name, role, password, pharmacy_id=None):
    """Create an account with a supported role and hashed password. When no
    pharmacy is specified (desktop/single-tenant use), the account is homed
    in the first active pharmacy."""
    import uuid
    from werkzeug.security import generate_password_hash

    name = str(name).strip()
    if not name:
        raise ValueError("Name is required.")
    if role not in {"admin", "pharmacist", "user"}:
        raise ValueError("Unsupported user role.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    conn = get_db_connection()
    cur = conn.cursor()
    if pharmacy_id is None:
        row = cur.execute(
            "SELECT id FROM pharmacy WHERE status = 'active' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        pharmacy_id = row["id"] if row else None
    user_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO \"user\" (id, name, role, password_hash, pharmacy_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, role, generate_password_hash(password), pharmacy_id)
    )
    conn.commit()
    conn.close()
    return user_id


def authenticate_user(name, password):
    """
    Checks a login attempt against the stored password hash.
    Returns the user dict (including pharmacy_id and status) on success,
    or None on failure - the caller should show the same generic error
    either way (wrong name, or right name/wrong password), so a login
    attempt can't be used to discover which usernames exist.
    """
    from werkzeug.security import check_password_hash

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT u.id, u.name, u.role, u.password_hash, u.pharmacy_id,
                  u.status, ph.status AS pharmacy_status
           FROM "user" u
           LEFT JOIN pharmacy ph ON ph.id = u.pharmacy_id
           WHERE u.name = ?""",
        (name,)
    )
    row = cur.fetchone()
    conn.close()

    if row is None or not row["password_hash"]:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return {"id": row["id"], "name": row["name"], "role": row["role"],
            "pharmacy_id": row["pharmacy_id"], "status": row["status"],
            "pharmacy_status": row["pharmacy_status"]}


def delete_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM "user" WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()


def get_all_movements_for_export():
    """All stock movements with product/batch context, for CSV export."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT sm.occurred_at, p.name AS product_name, pb.batch_number,
               sm.movement_type, sm.quantity, sm.reference_number,
               sm.counterparty_name, sm.reason
        FROM stock_movement sm
        JOIN product_batch pb ON pb.id = sm.product_batch_id
        JOIN product p ON p.id = pb.product_id
        ORDER BY sm.occurred_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
