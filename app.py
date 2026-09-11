from flask import Flask, render_template, request, redirect, url_for, jsonify, abort, session, Response
from flask_jwt_extended import JWTManager
from datetime import timedelta
import os
import csv
import io
import time
import functools
from database.db import init_db
from database.queries import (
    get_product_list, get_product_detail,
    create_product, get_product_by_barcode,
    get_products_for_dropdown, get_batches_for_product,
    create_movement, get_product_id_for_batch,
    get_loss_reports, mark_loss_reported,
    get_dashboard_data,
    update_product, get_setting, set_setting,
    get_alert_count,
    get_users, get_user_by_id, create_user, delete_user, authenticate_user, user_name_exists,
    admin_exists,
    get_all_movements_for_export, is_token_revoked
)
from api import api_v1_bp

# Always ensure tables exist on startup. Safe to run every time because
# schema.sql uses CREATE TABLE IF NOT EXISTS - this also self-heals a
# leftover empty/broken pharmacy.db from a previous failed run, instead
# of silently trusting that the file's presence means it's set up correctly.
init_db()

app = Flask(__name__)
# Random key each launch is intentional: it means any previous session cookie
# stops working, so the app always asks "who's using it?" on a fresh start -
# reasonable for a shared desktop station used across shifts.
app.secret_key = os.urandom(32)
is_production = os.environ.get('PHARMATRACK_ENV', '').lower() == 'production'
jwt_secret = os.environ.get('JWT_SECRET_KEY')
if is_production and not jwt_secret:
    raise RuntimeError('JWT_SECRET_KEY must be set when PHARMATRACK_ENV=production.')

# A development secret is safe only for local testing because it changes on
# restart. Production requires a persistent secret supplied by the host.
app.config['JWT_SECRET_KEY'] = jwt_secret or os.urandom(64)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(
    minutes=int(os.environ.get('JWT_ACCESS_TOKEN_MINUTES', '30'))
)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(
    days=int(os.environ.get('JWT_REFRESH_TOKEN_DAYS', '30'))
)
app.config['JWT_TOKEN_LOCATION'] = ['headers']
jwt = JWTManager(app)


@jwt.token_in_blocklist_loader
def is_revoked_token(_jwt_header, jwt_payload):
    return is_token_revoked(jwt_payload['jti'])


@jwt.unauthorized_loader
def missing_token(reason):
    return jsonify(error='Authentication is required.', detail=reason), 401


@jwt.invalid_token_loader
def invalid_token(reason):
    return jsonify(error='Invalid authentication token.', detail=reason), 422


@jwt.expired_token_loader
def expired_token(_jwt_header, _jwt_payload):
    return jsonify(error='Authentication token has expired.'), 401


@jwt.revoked_token_loader
def revoked_token(_jwt_header, _jwt_payload):
    return jsonify(error='Authentication token has been revoked.'), 401

app.register_blueprint(api_v1_bp)

# Basic login-attempt limiting. In-memory is fine here: this is a single
# desktop process, not a multi-server deployment, and lockouts don't need
# to survive an app restart. Maps name -> {"count": int, "locked_until": float}
_login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def _check_lockout(name):
    """Returns seconds remaining if locked out, or 0 if login can proceed."""
    entry = _login_attempts.get(name)
    if not entry:
        return 0
    remaining = entry["locked_until"] - time.time()
    return max(0, remaining)


def _record_failed_attempt(name):
    entry = _login_attempts.setdefault(name, {"count": 0, "locked_until": 0})
    entry["count"] += 1
    if entry["count"] >= MAX_LOGIN_ATTEMPTS:
        entry["locked_until"] = time.time() + LOCKOUT_SECONDS
        entry["count"] = 0


def _clear_attempts(name):
    _login_attempts.pop(name, None)


def login_required(view):
    """Blocks a route unless someone has picked their name at /login."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Blocks a route unless the logged-in user's role is admin."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'admin':
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def pharmacist_required(view):
    """Blocks a route unless the logged-in user's role is pharmacist.
    Admin is deliberately an oversight role (Dashboard, Loss Reports,
    Settings, Accounts) - not day-to-day inventory/movement operations."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'pharmacist':
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    """Available in every template: the alert badge count, and who's
    currently logged in (for the sidebar profile section and role checks)."""
    return {
        "alert_count": get_alert_count() if 'user_id' in session else 0,
        "current_user_id": session.get('user_id'),
        "current_user_name": session.get('user_name'),
        "current_user_role": session.get('role'),
    }


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    admin_taken = admin_exists()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        role = request.form.get('role', 'pharmacist')

        if not name or not password:
            error = "Name and password are required."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif user_name_exists(name):
            error = "That name is already registered. Choose another, or sign in instead."
        elif role == 'admin' and admin_taken:
            # Server-side enforcement - not just a hidden dropdown option.
            # Even a tampered request can't create a second admin.
            error = "An admin account already exists for this pharmacy. Register as a pharmacist instead."
        else:
            user_id = create_user(name=name, role=role, password=password)
            session['user_id'] = user_id
            session['user_name'] = name
            session['role'] = role
            return redirect(url_for('dashboard'))

    return render_template('register.html', error=error, admin_taken=admin_taken)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '')
        password = request.form.get('password', '')

        locked_seconds = _check_lockout(name)
        if locked_seconds > 0:
            error = f"Too many failed attempts. Try again in {int(locked_seconds)} seconds."
        else:
            user = authenticate_user(name, password)
            if user:
                _clear_attempts(name)
                session['user_id'] = user['id']
                session['user_name'] = user['name']
                session['role'] = user['role']
                next_url = request.form.get('next') or url_for('dashboard')
                return redirect(next_url)
            # Deliberately generic - never reveals whether the name exists
            _record_failed_attempt(name)
            error = "Incorrect name or password."

    return render_template('login.html', error=error, next=request.args.get('next', ''))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    data = get_dashboard_data()
    return render_template('dashboard.html', active_page='dashboard', **data)

@app.route('/products')
@pharmacist_required
def product_list():
    search = request.args.get('q', '').strip()
    products = get_product_list(search=search if search else None)
    return render_template('product_list.html', active_page='inventory', products=products, search_query=search)

@app.route('/products/<product_id>')
@pharmacist_required
def product_details(product_id):
    product = get_product_detail(product_id)
    if product is None:
        abort(404)
    return render_template('product_details.html', active_page='inventory', product=product)

@app.route('/products/new', methods=['GET', 'POST'])
@pharmacist_required
def add_product():
    if request.method == 'POST':
        product_id = create_product(
            name=request.form['name'],
            category=request.form.get('category'),
            strength=request.form.get('strength'),
            dosage_form=request.form.get('dosage_form'),
            barcode=request.form.get('barcode') or None,
            requires_prescription=request.form.get('requires_prescription') == 'on',
            is_controlled=request.form.get('is_controlled') == 'on',
            batch_number=request.form.get('batch_number'),
            expiry_date=request.form.get('expiry_date'),
            initial_quantity=request.form.get('initial_quantity') or 0,
        )
        return redirect(url_for('product_details', product_id=product_id))

    return render_template('add_product.html', active_page='inventory')

@app.route('/api/barcode-lookup')
@pharmacist_required
def barcode_lookup():
    barcode = request.args.get('barcode', '')
    existing = get_product_by_barcode(barcode)
    return jsonify({"exists": existing is not None, "product": existing})

@app.route('/api/products/<product_id>/batches')
@pharmacist_required
def api_product_batches(product_id):
    return jsonify(get_batches_for_product(product_id))

@app.route('/movements/new', methods=['GET', 'POST'])
@pharmacist_required
def record_movement():
    if request.method == 'POST':
        batch_id = request.form['batch_id']
        create_movement(
            product_batch_id=batch_id,
            movement_type=request.form['movement_type'],
            quantity=request.form['quantity'],
            adjustment_direction=request.form.get('adjustment_direction'),
            counterparty_name=request.form.get('counterparty_name') or None,
            counterparty_address=request.form.get('counterparty_address') or None,
            reference_number=request.form.get('reference_number') or None,
            prescription_number=request.form.get('prescription_number') or None,
            reason=request.form.get('reason') or None,
        )
        product_id = get_product_id_for_batch(batch_id)
        return redirect(url_for('product_details', product_id=product_id))

    products = get_products_for_dropdown()
    return render_template('record_movement.html', active_page='movements', products=products)

@app.route('/products/<product_id>/edit', methods=['GET', 'POST'])
@pharmacist_required
def edit_product(product_id):
    # Deliberately NOT admin-only: editing product info is everyday
    # pharmacist work, same as adding products or recording movements.
    if request.method == 'POST':
        update_product(
            product_id=product_id,
            name=request.form['name'],
            category=request.form.get('category'),
            strength=request.form.get('strength'),
            dosage_form=request.form.get('dosage_form'),
            barcode=request.form.get('barcode') or None,
            requires_prescription=request.form.get('requires_prescription') == 'on',
            is_controlled=request.form.get('is_controlled') == 'on',
        )
        return redirect(url_for('product_details', product_id=product_id))

    product = get_product_detail(product_id)
    if product is None:
        abort(404)
    return render_template('edit_product.html', active_page='inventory', product=product)

@app.route('/preferences')
@login_required
def preferences():
    """Open to every logged-in user: personal display preferences
    (theme, language) - NOT system configuration, which lives in /settings."""
    return render_template('preferences.html', active_page='preferences')

@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """Admin-only: system-wide configuration, not personal preferences."""
    if request.method == 'POST':
        set_setting('pharmacy_name', request.form.get('pharmacy_name', 'PharmaTrack Pharmacy'))
        set_setting('pharmacy_address', request.form.get('pharmacy_address', ''))
        set_setting('pharmacy_latitude', request.form.get('pharmacy_latitude', ''))
        set_setting('pharmacy_longitude', request.form.get('pharmacy_longitude', ''))
        set_setting('low_stock_threshold', request.form.get('low_stock_threshold', '100'))
        return redirect(url_for('settings'))

    return render_template(
        'settings.html',
        active_page='settings',
        pharmacy_name=get_setting('pharmacy_name', 'PharmaTrack Pharmacy'),
        pharmacy_address=get_setting('pharmacy_address', ''),
        pharmacy_latitude=get_setting('pharmacy_latitude', ''),
        pharmacy_longitude=get_setting('pharmacy_longitude', ''),
        low_stock_threshold=get_setting('low_stock_threshold', '100'),
        total_products=len(get_product_list()),
    )

@app.route('/settings/users', methods=['GET', 'POST'])
@admin_required
def manage_users():
    """Admin-only: create/remove pharmacist accounts. Cannot create a
    second admin - there is exactly one admin, already signed in here."""
    error = None
    if request.method == 'POST':
        role = request.form.get('role', 'pharmacist')
        if role == 'admin':
            error = "Only one admin account is allowed. New accounts must be pharmacists."
        elif len(request.form.get('password', '')) < 8:
            error = "Password must be at least 8 characters."
        else:
            create_user(name=request.form['name'], role=role, password=request.form['password'])
            return redirect(url_for('manage_users'))

    return render_template('manage_users.html', active_page='settings', users=get_users(), error=error)

@app.route('/settings/users/<user_id>/delete', methods=['POST'])
@admin_required
def delete_user_route(user_id):
    if user_id == session.get('user_id'):
        # Refuse to let an admin delete their own currently-active account
        abort(400)
    delete_user(user_id)
    return redirect(url_for('manage_users'))

@app.route('/movements/export')
@login_required
def export_movements():
    rows = get_all_movements_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Product', 'Batch', 'Type', 'Quantity', 'Reference', 'Counterparty', 'Reason'])
    for r in rows:
        writer.writerow([r['occurred_at'], r['product_name'], r['batch_number'], r['movement_type'],
                          r['quantity'], r['reference_number'] or '', r['counterparty_name'] or '', r['reason'] or ''])
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=pharmatrack_movements.csv'}
    )

@app.route('/loss-reports/export')
@login_required
def export_loss_reports():
    unreported_only = request.args.get('unreported') == '1'
    data = get_loss_reports()
    rows = [l for l in data['losses'] if not l['reported']] if unreported_only else data['losses']

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Product', 'Batch', 'Quantity', 'Reason', 'Reported', 'Reported At'])
    for l in rows:
        writer.writerow([l['date'], l['product_name'], l['batch_number'], l['quantity'],
                          l['reason'], 'Yes' if l['reported'] else 'No', l['reported_at'] or ''])

    filename = 'pharmatrack_unreported_losses.csv' if unreported_only else 'pharmatrack_loss_reports.csv'
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/loss-reports')
@login_required
def loss_reports():
    data = get_loss_reports()
    return render_template('loss_reports.html', active_page='loss_reports', **data)

@app.route('/loss-reports/<loss_report_id>/mark-reported', methods=['POST'])
@login_required
def mark_reported(loss_report_id):
    mark_loss_reported(loss_report_id, authority_reference=request.form.get('authority_reference'))
    return redirect(url_for('loss_reports'))

def start_flask():
    app.run(port=5000, debug=False, use_reloader=False)

if __name__ == '__main__':
    import threading
    import webbrowser

    threading.Thread(target=start_flask, daemon=True).start()
    app_url = 'http://127.0.0.1:5000'

    try:
        import webview
        webview.create_window('PharmaTrack', app_url)
        webview.start()
    except webview.errors.WebViewException:
        print('pywebview has no GTK or Qt backend; opening PharmaTrack in your browser.')
        webbrowser.open(app_url)
        threading.Event().wait()
