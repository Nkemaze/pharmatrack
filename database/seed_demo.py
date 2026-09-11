"""
Demo tenants + stock for PharmaTrack (Abuja, Nigeria).

Seeds several pharmacy tenants, each with a realistic catalogue of common
medicines, one batch per product, and receipt movements so the public API
shows them in stock. Also creates a pharmacist login per tenant so the web
UI can be used against the demo data.

Run this once, after init_db() has already created the tables (the server
does this at startup):
    python database/seed_demo.py

Safe to re-run: every row uses a deterministic id, so INSERT ... ON CONFLICT
DO NOTHING simply skips rows that already exist.
"""

import json
import uuid
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

try:
    from database.db import get_db_connection as _db_conn
except ImportError:  # running as `python database/seed_demo.py`
    from db import get_db_connection as _db_conn


def _connection():
    return _db_conn()

# Fixed namespace so ids are stable across runs (idempotent seeding).
_NS = uuid.UUID('3f1d5474-2df1-4f27-9d5b-9f9f1b0a7e6d')


def _id(slug):
    return str(uuid.uuid5(_NS, slug))


def _add_pharmacy(conn, slug, name, address, city, lat, lng, phone,
                  emergency_phone, hours):
    conn.execute(
        """INSERT INTO pharmacy
           (id, name, address, city, phone, emergency_phone, latitude,
            longitude, opening_hours, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
           ON CONFLICT(id) DO NOTHING""",
        (_id('pharmacy:' + slug), name, address, city, phone, emergency_phone,
         str(lat), str(lng), json.dumps(hours)),
    )


def _add_products(conn, pharmacy_slug, catalogue):
    """Inserts products, one batch, and a receipt movement per product.

    Prices vary slightly per pharmacy so the app's cross-pharmacy search and
    'Popular Medicines' list have real comparisons to show.
    """
    for item in catalogue:
        product_id = _id(f'{pharmacy_slug}:{item["name"]}')
        conn.execute(
            """INSERT INTO product
               (id, pharmacy_id, name, category, strength, dosage_form,
                requires_prescription, is_controlled, price_per_unit,
                price_per_packet, packet_size, unit_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (product_id, _id('pharmacy:' + pharmacy_slug), item['name'],
             item['category'], item['strength'], item['dosage_form'],
             1 if item['requires_prescription'] else 0, 0,
             item['price_per_unit'], item['price_per_packet'],
             item['packet_size'], item['unit_label']),
        )

        batch_id = _id(f'{pharmacy_slug}:{item["name"]}:batch')
        batch = "BN-DEMO-" + batch_id[:6].upper()
        expiry = (date.today() + timedelta(days=item.get('expires_in_days', 365))
                  ).isoformat()
        conn.execute(
            """INSERT INTO product_batch
               (id, product_id, batch_number, expiry_date)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (batch_id, product_id, batch, expiry),
        )

        # One receipt movement per batch so current stock is positive and the
        # public API reports the product as in_stock.
        conn.execute(
            """INSERT INTO stock_movement
               (id, product_batch_id, movement_type, quantity, reason)
               VALUES (?, ?, 'receipt', ?, 'Initial demo stock')
               ON CONFLICT(id) DO NOTHING""",
            (_id(f'{pharmacy_slug}:{item["name"]}:movement'), batch_id,
             item.get('quantity', 150)),
        )


def _add_pharmacist(conn, pharmacy_slug, username):
    conn.execute(
        """INSERT INTO "user"
           (id, name, role, password_hash, pharmacy_id, status)
           VALUES (?, ?, 'pharmacist', ?, ?, 'active')
           ON CONFLICT(id) DO NOTHING""",
        (_id('user:' + pharmacy_slug), username,
         generate_password_hash('demo-pass-123'), _id('pharmacy:' + pharmacy_slug)),
    )


def _item(category, strength, dosage_form, name, unit, packet_size,
          unit_price, packet_price, rx=False, **extra):
    return {
        'name': name, 'category': category, 'strength': strength,
        'dosage_form': dosage_form, 'unit_label': unit, 'packet_size': packet_size,
        'price_per_unit': unit_price, 'price_per_packet': packet_price,
        'requires_prescription': rx, **extra,
    }


def _catalogue(scale=1.0):
    """The same medicines across every pharmacy, with pharmacy-specific pricing."""
    def px(unit):
        return round(max(10, unit * scale))

    return [
        _item('Analgesics', '500mg', 'Tablet', 'Paracetamol 500mg', 'tablet', 10,
              px(40), px(400)),
        _item('Analgesics', '400mg', 'Tablet', 'Ibuprofen 400mg', 'tablet', 10,
              px(120), px(900)),
        _item('Antibiotics', '500mg', 'Capsule', 'Amoxicillin 500mg', 'capsule', 10,
              px(150), px(1350), rx=True),
        _item('Antibiotics', '250mg/5ml', 'Suspension', 'Amoxicillin 250mg/5ml', 'bottle',
              1, px(1800), px(1800), rx=True),
        _item('Antimalarials', '80/480mg', 'Tablet',
              'Artemether-Lumefantrine 80/480mg', 'tablet', 6, px(250), px(1400), rx=True),
        _item('Antimalarials', '200mg', 'Tablet', 'Chloroquine 200mg', 'tablet', 6,
              px(80), px(450)),
        _item('GI & Digestion', '20mg', 'Capsule', 'Omeprazole 20mg', 'capsule', 14,
              px(130), px(1700), rx=True),
        _item('GI & Digestion', '400mg', 'Tablet', 'Metronidazole 400mg', 'tablet', 10,
              px(70), px(600), rx=True),
        _item('Antihistamines', '10mg', 'Tablet', 'Cetirizine 10mg', 'tablet', 10,
              px(60), px(550)),
        _item('Vitamins & Supplements', '500mg', 'Tablet', 'Vitamin C 500mg', 'tablet',
              10, px(50), px(450)),
        _item('Vitamins & Supplements', '20mg', 'Tablet', 'Zinc Sulphate 20mg', 'tablet',
              10, px(70), px(650)),
        _item('Cold & Flu', '100ml', 'Syrup', 'Cough Syrup (Adult)', 'bottle', 1,
              px(900), px(900)),
        _item('GI & Digestion', '', 'Sachet', 'Oral Rehydration Salts', 'sachet', 4,
              px(120), px(450)),
    ]


def seed():
    # Each pharmacy gets the catalogue shifted by a small price factor so the
    # same medicine is priced differently across tenants (realistic demo).
    pharma = [
        ('citycare', 'CityCare Pharmacy', '12 Aminu Kano Crescent, Wuse II',
         'Abuja', 9.0784, 7.4646, '0803 111 1111', '0803 111 1112', 1.0),
        ('medplus', 'MedPlus Health Store', 'Plot 54, Adetokunbo Ademola Crescent, Wuse II',
         'Abuja', 9.0725, 7.4879, '0803 222 2222', '0803 222 2223', 1.1),
        ('sunshine', 'Sunshine Pharmacy', '3 Gimbiya Street, Area 11, Garki',
         'Abuja', 9.0358, 7.4888, '0803 333 3333', '0803 333 3334', 0.95),
        ('greenleaf', 'Greenleaf Chemists', '21 Ahmadu Bello Way, Garki II',
         'Abuja', 9.0419, 7.4934, '0803 444 4444', '0803 444 4445', 1.05),
        ('evercare', 'Evercare Pharmacy', '9 Gana Street, Maitama',
         'Abuja', 9.0860, 7.4968, '0803 555 5555', '0803 555 5556', 1.15),
        ('trustwell', 'Trustwell Pharmacy', '15 Asokoro District',
         'Abuja', 9.0513, 7.5066, '0803 666 6666', '0803 666 6667', 0.9),
    ]
    hours = {
        'weekdayOpen': '08:00', 'weekdayClose': '21:00',
        'weekendOpen': '09:00', 'weekendClose': '19:00',
    }

    conn = _connection()
    try:
        for slug, name, address, city, lat, lng, phone, emergency, scale in pharma:
            _add_pharmacy(conn, slug, name, address, city, lat, lng, phone,
                          emergency, hours)
            _add_products(conn, slug, _catalogue(scale))
            _add_pharmacist(conn, slug, slug)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f'Demo data seeded for {len(pharma)} pharmacies:')
    for slug, name, *_ in pharma:
        print(f'  - {name}  (login: {slug} / demo-pass-123)')


if __name__ == '__main__':
    seed()