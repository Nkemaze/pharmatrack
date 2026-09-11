"""
Seed data for PharmaTrack — inserts a handful of real products, batches,
and stock movements so the UI has actual numbers to display instead of
Stitch's hardcoded mock rows.

Run this once, after init_db() has already created the tables:
    python database/seed.py

Safe to re-run: it clears existing data first so you don't get duplicates.
"""

import uuid
from datetime import datetime, timedelta
from db import get_db_connection


def new_id():
    return str(uuid.uuid4())


def seed():
    conn = get_db_connection()
    cur = conn.cursor()

    # Clear existing data so this script can be run more than once safely
    cur.execute("DELETE FROM loss_report")
    cur.execute("DELETE FROM stock_movement")
    cur.execute("DELETE FROM product_batch")
    cur.execute("DELETE FROM product")
    cur.execute('DELETE FROM "user"')

    # No demo users are created here anymore - people register their own
    # account (with their own name, password, and chosen role) from the
    # app's Register screen. Seeded product movements below are attributed
    # to no one in particular (performed_by_user_id left NULL).

    # A pharmacy tenant for the seed data (desktop: the single default tenant).
    import json
    pharmacy_id = new_id()
    cur.execute(
        """INSERT INTO pharmacy
           (id, name, address, city, phone, emergency_phone, latitude,
            longitude, opening_hours, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (pharmacy_id, "PharmaTrack Pharmacy", "Avenue de la République",
         "Yaoundé", "+237 222 22 22 22", "+237 677 00 00 00", "3.8480", "11.5021",
         json.dumps({"weekdayOpen": "08:00", "weekdayClose": "18:00",
                     "weekendOpen": "09:00", "weekendClose": "14:00"}))
    )

    # --- Products (matches names used across the UI screens) ---
    products = [
        {"name": "Amoxicillin 500mg", "category": "Antibiotics", "strength": "500mg", "dosage_form": "Capsule",
         "requires_prescription": 1, "is_controlled": 0,
         "price_per_unit": 250, "price_per_packet": 2400, "packet_size": 10, "unit_label": "capsule"},
        {"name": "Paracetamol 500mg", "category": "Analgesics", "strength": "500mg", "dosage_form": "Tablet",
         "requires_prescription": 0, "is_controlled": 0,
         "price_per_unit": 100, "price_per_packet": 850, "packet_size": 10, "unit_label": "tablet"},
        {"name": "Insulin Glargine 100u/ml", "category": "Diabetic Care", "strength": "100u/ml", "dosage_form": "Injection",
         "requires_prescription": 1, "is_controlled": 0,
         "price_per_unit": 15000, "price_per_packet": 15000, "packet_size": 1, "unit_label": "pen"},
        {"name": "Diazepam 5mg", "category": "Controlled - Sedative", "strength": "5mg", "dosage_form": "Tablet",
         "requires_prescription": 1, "is_controlled": 1,
         "price_per_unit": 120, "price_per_packet": 1050, "packet_size": 10, "unit_label": "tablet"},
        {"name": "Morphine Sulfate 10mg", "category": "Controlled - Analgesic", "strength": "10mg", "dosage_form": "Tablet",
         "requires_prescription": 1, "is_controlled": 1,
         "price_per_unit": 800, "price_per_packet": 7500, "packet_size": 10, "unit_label": "tablet"},
    ]

    today = datetime.now()

    for p in products:
        product_id = new_id()
        cur.execute(
            """INSERT INTO product (id, pharmacy_id, name, category, strength, dosage_form,
               requires_prescription, is_controlled, price_per_unit, price_per_packet,
               packet_size, unit_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, pharmacy_id, p["name"], p["category"], p["strength"], p["dosage_form"],
             p["requires_prescription"], p["is_controlled"], p["price_per_unit"],
             p["price_per_packet"], p["packet_size"], p["unit_label"])
        )

        # One batch per product, expiry a few months out (except one deliberately near-expiry)
        batch_id = new_id()
        expiry = today + timedelta(days=180)
        if p["name"] == "Insulin Glargine 100u/ml":
            expiry = today + timedelta(days=20)  # near-expiry, to test the "expiring soon" UI state

        cur.execute(
            """INSERT INTO product_batch (id, product_id, batch_number, expiry_date, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (batch_id, product_id, f"BN-{today.year}-{new_id()[:6].upper()}",
             expiry.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d %H:%M:%S'))
        )

        # A receipt movement (stock coming in) so current stock isn't zero
        starting_qty = 1500 if p["name"] != "Insulin Glargine 100u/ml" else 45  # low, to test low-stock UI state
        cur.execute(
            """INSERT INTO stock_movement
               (id, product_batch_id, movement_type, quantity, reference_number, performed_by_user_id, device_id)
               VALUES (?, ?, 'receipt', ?, ?, ?, ?)""",
            (new_id(), batch_id, starting_qty, f"GRN-{new_id()[:6].upper()}", None, "device-001")
        )

        # A small sale movement for realism (skip for the controlled/low-stock ones)
        if p["name"] not in ("Insulin Glargine 100u/ml", "Diazepam 5mg", "Morphine Sulfate 10mg"):
            cur.execute(
                """INSERT INTO stock_movement
                   (id, product_batch_id, movement_type, quantity, performed_by_user_id, device_id)
                   VALUES (?, ?, 'sale', ?, ?, ?)""",
                (new_id(), batch_id, -80, None, "device-001")
            )

    conn.commit()
    conn.close()
    print("Seed data inserted: 5 products, 1 pharmacy, 5 batches, movements included.")
    print("No user accounts were created - register your own account from the app's Register screen.")


if __name__ == '__main__':
    seed()