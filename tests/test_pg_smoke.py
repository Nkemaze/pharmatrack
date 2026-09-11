"""One-shot smoke test of the query layer against PostgreSQL.
Run: DATABASE_URL=postgresql://postgres:testpass@localhost:5433/pharmatrack .venv/bin/python tests/test_pg_smoke.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from database.db import get_db_connection
from database.queries import (
    create_product, create_movement, get_batches_for_product,
    get_product_list, get_public_inventory, get_popular_products,
    get_loss_reports, get_dashboard_data, create_user, authenticate_user,
    get_users, delete_user, revoke_token, is_token_revoked,
    get_public_pharmacies, get_public_pharmacy, get_user_by_id,
)


def main():
    assert os.environ.get('DATABASE_URL'), 'DATABASE_URL must be set (postgres)'
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM pharmacy ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        raise SystemExit('No pharmacy row seeded; run database/db.py first.')
    pharmacy_id = row['id']
    print('pharmacy:', pharmacy_id)

    uid = 'smoke-' + uuid.uuid4().hex[:8]
    try:
        conn.execute(
            'INSERT INTO "user" (id, name, role, password_hash, pharmacy_id, status) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            (uid, 'SmokeUser', 'pharmacist', 'hash', pharmacy_id, 'active'),
        )
        conn.commit()
    except Exception as e:
        print('user insert:', e)

    # create a product with stock, then a movement
    pid = create_product('SmokeDrug 10mg', 'Smoke', '10mg', 'Tablet', 'SMK-001',
                         False, False, 'BN-SMOKE', '2027-01-01', 50,
                         performed_by_user_id=uid, pharmacy_id=pharmacy_id,
                         price_per_unit=123.45, price_per_packet=1000,
                         packet_size=8, unit_label='tablet')
    print('created product:', pid)

    batches = get_batches_for_product(pid, pharmacy_id)
    print('batches:', [(b['batch_number'], b['quantity_remaining']) for b in batches])
    mid = create_movement(batches[0]['id'], 'sale', 3, performed_by_user_id=uid)
    print('movement:', mid)

    print('product_list:', [(p['name'], p['current_stock'], p['pharmacy_name'])
                            for p in get_product_list(pharmacy_id=pharmacy_id)])
    print('public_inventory:', [(p['name'], p['in_stock'], p['price_per_unit'])
                                for p in get_public_inventory(pharmacy_id=pharmacy_id)])
    print('popular:', get_popular_products(5))
    print('public_pharmacies:', [p['name'] for p in get_public_pharmacies()])
    print('public_pharmacy:', get_public_pharmacy(pharmacy_id)['name'])
    print('loss_reports:', get_loss_reports()['total_losses'])
    dash = get_dashboard_data()
    print('dashboard total_products:', dash['total_products'])

    # users round-trip
    created = create_user('SmokeReg', 'user', 'password-123', pharmacy_id=pharmacy_id)
    print('create_user:', created)
    print('get_user_by_id:', get_user_by_id(created))
    auth = authenticate_user('SmokeReg', 'password-123')
    print('authenticate:', auth['name'], auth['pharmacy_id'], auth['status'])
    print('get_users:', [u['name'] for u in get_users()])
    delete_user(created)

    revoke_token('smoke-jti', 'access', uid, 9999999999)
    print('revoked true:', is_token_revoked('smoke-jti'))
    print('revoked false:', is_token_revoked('nope'))

    conn.close()
    print('\nALL PG SMOKE TESTS PASSED')


if __name__ == '__main__':
    main()