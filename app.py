from dotenv import load_dotenv

load_dotenv()  # local .env (gitignored): DATABASE_URL, SECRET_KEY, JWT_SECRET_KEY...
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort, session, Response, send_from_directory
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from datetime import date, timedelta
import os
import csv
import io
import json
import functools
from werkzeug.exceptions import HTTPException
from database.db import init_db
from database.queries import (
    get_product_list, get_product_detail,
    create_product, get_product_by_barcode,
    get_products_for_dropdown, get_batches_for_product,
    create_movement, get_product_id_for_batch, get_product_id_for_batch_owned_by,
    update_batch,
    get_loss_reports, mark_loss_reported,
    get_dashboard_data,
    update_product, get_setting, set_setting,
    get_alert_count,
    get_users, get_user_by_id, create_user, delete_user, authenticate_user, user_name_exists,
    admin_exists, change_password, mark_profile_completed, verify_password,
    get_all_movements_for_export, is_token_revoked,
    get_pharmacy, update_pharmacy, get_pharmacies,
    login_attempt_lockout, record_login_attempt, clear_login_attempts,
    create_pharmacy_registration, create_pharmacy_account, get_pharmacies_with_applicant,
    get_pending_pharmacy_count, get_pending_setup_count, update_pharmacy_status,
    delete_pharmacy_application, login_status_block,
)
from api import api_v1_bp

# Always ensure tables exist on startup. Safe to run every time because
# schema.sql uses CREATE TABLE IF NOT EXISTS - this also self-heals a
# leftover empty/broken pharmacy.db from a previous failed run, instead
# of silently trusting that the file's presence means it's set up correctly.
init_db()

# Optional demo seeding for hosted deployments that have no shell access
# (e.g. Render free tier). Set SEED_DEMO=true in the service environment and
# restart to populate demo pharmacies, medicines and pharmacist logins. Safe
# to leave on: seeding is idempotent (deterministic ids + ON CONFLICT DO NOTHING).
if os.environ.get('SEED_DEMO', '').strip().lower() in ('1', 'true', 'yes'):
    from database.seed_demo import seed as seed_demo
    seed_demo()

app = Flask(__name__)
# Random key each launch is intentional on a desktop install: any previous
# session cookie stops working, so the app always asks "who's using it?" on
# a fresh start - reasonable for a shared desktop station used across shifts.
# Hosted production needs a persistent SECRET_KEY so sessions survive
# restarts; PHARMATRACK_ENV=production enforces that.
secret_key = os.environ.get('SECRET_KEY')
app.secret_key = secret_key if secret_key else os.urandom(32)
is_production = os.environ.get('PHARMATRACK_ENV', '').lower() == 'production'
jwt_secret = os.environ.get('JWT_SECRET_KEY')
if is_production and not jwt_secret:
    raise RuntimeError('JWT_SECRET_KEY must be set when PHARMATRACK_ENV=production.')
if is_production and not secret_key:
    raise RuntimeError('SECRET_KEY must be set when PHARMATRACK_ENV=production.')

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

# CORS for the responsive web UI when accessed from another origin (e.g. the
# customer app or a dev frontend served elsewhere). CORS_ORIGINS is a
# comma-separated whitelist; '*' means any origin (fine for a public API).
_cors_origins = os.environ.get('CORS_ORIGINS', '*')
CORS(app, origins=_cors_origins.split(',') if _cors_origins != '*' else '*',
     supports_credentials=True)

# Setup code for the secret /admin-console first-run bootstrap (mirrors
# AdminConfig.adminSetupCode in the Flutter app). ENV override allows each
# deployment to change it without touching code.
ADMIN_SETUP_CODE = os.environ.get('PHARMATRACK_ADMIN_SETUP_CODE', 'PharmaAdmin#2026')


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

# Login-attempt limiting, backed by the login_attempt table so it survives
# restarts and works across multiple web workers (hosted deployment). The
# web UI (and the API in api/auth.py) each use their own scope.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def _web_login_key():
    return request.remote_addr or 'unknown'


def _check_lockout(name):
    """Returns seconds remaining if locked out, or 0 if login can proceed."""
    return login_attempt_lockout('web', name, MAX_LOGIN_ATTEMPTS, LOCKOUT_SECONDS)


def _record_failed_attempt(name):
    record_login_attempt('web', name, MAX_LOGIN_ATTEMPTS, LOCKOUT_SECONDS)


def _clear_attempts(name):
    clear_login_attempts('web', name)


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
    currently logged in (for the sidebar profile section and role checks).

    Runs for every page — including the error page itself — so the DB
    lookups are guarded: a database hiccup or a single bad row must never
    turn an otherwise-fine page (or the 500 page) into a second failure."""
    is_admin = session.get('role') == 'admin'
    alert_count = 0
    pending_pharmacy_count = 0
    if 'user_id' in session:
        try:
            user = get_user_by_id(session.get('user_id'))
            pharmacy_id = user.get('pharmacy_id') if user else None
            alert_count = get_alert_count(pharmacy_id=pharmacy_id)
        except Exception:
            app.logger.exception('Failed to load alert count for template')
    if is_admin:
        try:
            pending_pharmacy_count = get_pending_pharmacy_count()
        except Exception:
            app.logger.exception('Failed to load pending pharmacy count for template')
    return {
        "alert_count": alert_count,
        "pending_pharmacy_count": pending_pharmacy_count,
        "current_user_id": session.get('user_id'),
        "current_user_name": session.get('user_name'),
        "current_user_role": session.get('role'),
    }


def _is_hosted():
    """True on the hosted multi-tenant server (PostgreSQL), where /register
    accepts pharmacy applications. Desktop single-pharmacy installs keep the
    legacy account form. PHARMATRACK_HOSTED lets the test suite exercise the
    hosted flow against a local database."""
    return bool(os.environ.get('DATABASE_URL')) or \
        os.environ.get('PHARMATRACK_HOSTED', '').lower() in ('1', 'true', 'yes')


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    success = None
    admin_taken = admin_exists()
    is_hosted = _is_hosted()

    if request.method == 'POST':
        role = request.form.get('role', 'pharmacy')

        if not is_hosted:
            # Desktop / single-pharmacy station: create a local account.
            name = request.form.get('name', '').strip()
            password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')

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
                try:
                    user_id = create_user(name=name, role=role, password=password)
                except Exception:
                    app.logger.exception('Account registration failed (desktop)')
                    error = ('We could not create your account right now. '
                             'Please try again in a moment.')
                else:
                    session['user_id'] = user_id
                    session['user_name'] = name
                    session['role'] = role
                    return redirect(url_for('dashboard'))

        elif role == 'admin':
            # Hosted bootstrap: the very first account is the platform
            # administrator. After that, registration is pharmacy-only.
            name = request.form.get('name', '').strip()
            password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')
            if admin_taken:
                error = "An admin account already exists for this site."
            elif not name or not password:
                error = "Name and password are required."
            elif password != confirm_password:
                error = "Passwords do not match."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif user_name_exists(name):
                error = "That name is already registered. Choose another."
            else:
                try:
                    create_user(name=name, role='admin', password=password)
                except Exception:
                    app.logger.exception('Admin bootstrap failed')
                    error = ('We could not create the administrator account right '
                             'now. Please try again in a moment.')
                else:
                    success = ("Administrator account created. "
                               "Sign in to review pharmacy applications.")

        else:
            # Hosted self-registration: a pharmacy application. It is saved
            # as 'pending' and the applicant cannot sign in until an
            # administrator approves it. No auto-login here.
            pharmacy_name = request.form.get('pharmacy_name', '').strip()
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not pharmacy_name or not email:
                error = "Pharmacy name and contact email are required."
            elif '@' not in email:
                error = "Enter a valid contact email address."
            elif password != confirm_password:
                error = "Passwords do not match."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif user_name_exists(email):
                error = "That email is already registered. Sign in instead."
            else:
                try:
                    create_pharmacy_registration(
                        name=pharmacy_name,
                        email=email,
                        password=password,
                        address=request.form.get('address', '').strip() or None,
                        city=request.form.get('city', '').strip() or None,
                        phone=request.form.get('phone', '').strip() or None,
                    )
                except Exception:
                    app.logger.exception('Pharmacy application failed')
                    error = ('We could not submit your application right now. '
                             'Please try again in a moment.')
                else:
                    success = ("Your pharmacy registration has been submitted and "
                               "is now awaiting administrator approval. You will be "
                               "able to sign in once it has been approved.")

    return render_template('register.html', error=error, success=success,
                           admin_taken=admin_taken, is_hosted=is_hosted)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '')
        password = request.form.get('password', '')

        try:
            locked_seconds = _check_lockout(name)
            if locked_seconds > 0:
                error = f"Too many failed attempts. Try again in {int(locked_seconds)} seconds."
            else:
                user = authenticate_user(name, password)
                if user:
                    block_message = login_status_block(user)
                    if block_message:
                        # Valid credentials, but the account (or its pharmacy
                        # tenant) is pending approval, suspended or rejected.
                        error = block_message
                    else:
                        _clear_attempts(name)
                        session.permanent = (request.form.get('remember') == 'on')
                        session['user_id'] = user['id']
                        session['user_name'] = user['name']
                        session['role'] = user['role']
                        if user.get('role') == 'pharmacist' and user.get('must_update_profile'):
                            # First login on an admin-created (or newly
                            # approved) pharmacy account: force the profile
                            # setup before the dashboard can be reached.
                            return redirect(url_for('profile_setup'))
                        return redirect(_safe_next(request.form.get('next')))
                else:
                    # Deliberately generic - never reveals whether the name exists
                    _record_failed_attempt(name)
                    error = "Incorrect name or password."
        except Exception:
            # A transient DB hiccup must not bounce the user onto a bare 500
            # page in the middle of a sign-in - show a friendly retry message.
            app.logger.exception('Login failed while checking credentials')
            error = ('We could not complete your sign-in right now. '
                     'Please try again in a moment.')

    return render_template('login.html', error=error, next=request.args.get('next', ''))


@app.route('/admin-console', methods=['GET', 'POST'])
def admin_console():
    """The secret administrator portal (mirrors the Flutter admin login page).

    First run on a fresh platform: a bootstrap form (email, password + setup
    code) creates the very first 'admin' account. Once an admin exists, the
    same page becomes a plain admin sign-in. There is deliberately no link to
    this route anywhere in the app."""
    show_bootstrap = not admin_exists()
    error = None

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')

        if show_bootstrap:
            confirm = request.form.get('confirm_password', '')
            setup_code = request.form.get('setup_code', '').strip()
            if not name or not password:
                error = "Name and password are required."
            elif password != confirm:
                error = "Passwords do not match."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif setup_code != ADMIN_SETUP_CODE:
                error = "The setup code is incorrect."
            elif user_name_exists(name):
                error = "That name is already registered. Choose another."
            else:
                try:
                    create_user(name=name, role='admin', password=password)
                except Exception:
                    app.logger.exception('Admin console bootstrap failed')
                    error = ('We could not create the administrator account right '
                             'now. Please try again in a moment.')
                else:
                    user = authenticate_user(name, password)
                    session['user_id'] = user['id']
                    session['user_name'] = user['name']
                    session['role'] = user['role']
                    return redirect(url_for('dashboard'))
        else:
            try:
                locked_seconds = _check_lockout(name)
                if locked_seconds > 0:
                    error = (f"Too many failed attempts. Try again in "
                             f"{int(locked_seconds)} seconds.")
                else:
                    user = authenticate_user(name, password)
                    if user is None:
                        _record_failed_attempt(name)
                        error = "Incorrect name or password."
                    elif user['role'] != 'admin':
                        error = "Only administrators can use this console."
                    else:
                        _clear_attempts(name)
                        session.permanent = True
                        session['user_id'] = user['id']
                        session['user_name'] = user['name']
                        session['role'] = user['role']
                        return redirect(url_for('dashboard'))
            except Exception:
                app.logger.exception('Admin console login failed')
                error = ('We could not complete your sign-in right now. '
                         'Please try again in a moment.')

    return render_template('admin_console.html', show_bootstrap=show_bootstrap,
                           error=error)


@app.route('/settings/profile', methods=['GET', 'POST'])
@pharmacist_required
def pharmacy_profile():
    """Pharmacy Settings: the logged-in pharmacist edits their own pharmacy
    profile (name, license, contact, hours, location) and can change their
    account password. Mirrors the Flutter app's PharmacyProfilePage."""
    user = get_user_by_id(session.get('user_id'))
    if user is None or not user.get('pharmacy_id'):
        return redirect(url_for('dashboard'))
    pharmacy = get_pharmacy(user['pharmacy_id']) or {}

    error = None
    if request.method == 'POST':
        pharmacy_name = request.form.get('pharmacy_name', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        new_password = request.form.get('new_password', '')

        if not pharmacy_name or not address:
            error = "Pharmacy name and street address are required."
        elif '@' not in email:
            error = "Enter a valid contact email address."
        else:
            # Password change is optional; only when the new-password box is filled in.
            if new_password:
                current_password = request.form.get('current_password', '')
                if not verify_password(user['id'], current_password):
                    error = "Current password is incorrect."
                elif len(new_password) < 8:
                    error = "New password must be at least 8 characters."
                elif new_password != request.form.get('confirm_password', ''):
                    error = "New passwords do not match."

        if error is None:
            try:
                opening_hours = {
                    'weekdayOpen': request.form.get('hours_weekday_open') or None,
                    'weekdayClose': request.form.get('hours_weekday_close') or None,
                    'weekendOpen': request.form.get('hours_weekend_open') or None,
                    'weekendClose': request.form.get('hours_weekend_close') or None,
                }
                update_pharmacy(
                    user['pharmacy_id'],
                    name=pharmacy_name,
                    address=address,
                    license_number=request.form.get('license_number', '').strip() or None,
                    email=email,
                    city=request.form.get('city', '').strip() or None,
                    state=request.form.get('state', '').strip() or None,
                    zip_code=request.form.get('zip_code', '').strip() or None,
                    phone=request.form.get('phone', '').strip() or None,
                    emergency_phone=request.form.get('emergency_phone', '').strip() or None,
                    emergency_desc=request.form.get('emergency_desc', '').strip() or None,
                    latitude=request.form.get('latitude', '').strip() or None,
                    longitude=request.form.get('longitude', '').strip() or None,
                    opening_hours=json.dumps(opening_hours),
                )
                if new_password:
                    change_password(user['id'], new_password)
                return redirect(url_for('pharmacy_profile', updated=1))
            except Exception:
                app.logger.exception('Pharmacy profile update failed')
                error = 'We could not save your profile right now. Please try again in a moment.'

    pharmacy = get_pharmacy(user['pharmacy_id']) or pharmacy
    parsed_hours = _parse_hours(pharmacy.get('opening_hours'))
    return render_template(
        'pharmacy_profile.html',
        active_page='settings',
        error=error,
        updated=request.args.get('updated') == '1',
        pharmacy=pharmacy,
        hours_weekday_open=(parsed_hours.get('weekdayOpen') or '08:00'),
        hours_weekday_close=(parsed_hours.get('weekdayClose') or '20:00'),
        hours_weekend_open=(parsed_hours.get('weekendOpen') or '09:00'),
        hours_weekend_close=(parsed_hours.get('weekendClose') or '15:00'),
    )


@app.route('/profile-setup', methods=['GET', 'POST'])
@login_required
def profile_setup():
    """Forced onboarding for a pharmacy account that has not completed its
    profile setup yet (must_update_profile = 1). The pharmacist saves their
    pharmacy details, is re-authenticated with the temporary password, and
    chooses a new password - only then is the flag lifted."""
    user = get_user_by_id(session.get('user_id'))
    if user is None or user['role'] != 'pharmacist':
        return redirect(url_for('dashboard'))
    if not user.get('must_update_profile'):
        return redirect(url_for('dashboard'))
    pharmacy = get_pharmacy(user['pharmacy_id']) if user['pharmacy_id'] else {}

    error = None
    if request.method == 'POST':
        pharmacy_name = request.form.get('pharmacy_name', '').strip()
        email = request.form.get('email', '').strip()
        address = request.form.get('address', '').strip()
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not pharmacy_name or not address:
            error = "Pharmacy name and street address are required."
        elif '@' not in email:
            error = "Enter a valid contact email address."
        elif not current_password:
            error = "Enter your current password to confirm this change."
        elif not verify_password(user['id'], current_password):
            error = "Current password is incorrect."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password != confirm_password:
            error = "New passwords do not match."
        else:
            try:
                opening_hours = {
                    'weekdayOpen': request.form.get('hours_weekday_open') or None,
                    'weekdayClose': request.form.get('hours_weekday_close') or None,
                    'weekendOpen': request.form.get('hours_weekend_open') or None,
                    'weekendClose': request.form.get('hours_weekend_close') or None,
                }
                update_pharmacy(
                    user['pharmacy_id'],
                    name=pharmacy_name,
                    license_number=request.form.get('license_number', '').strip() or None,
                    email=email,
                    address=address,
                    city=request.form.get('city', '').strip() or None,
                    state=request.form.get('state', '').strip() or None,
                    zip_code=request.form.get('zip_code', '').strip() or None,
                    phone=request.form.get('phone', '').strip() or None,
                    emergency_phone=request.form.get('emergency_phone', '').strip() or None,
                    emergency_desc=request.form.get('emergency_desc', '').strip() or None,
                    latitude=request.form.get('latitude', '').strip() or None,
                    longitude=request.form.get('longitude', '').strip() or None,
                    opening_hours=json.dumps(opening_hours),
                )
                change_password(user['id'], new_password)
                mark_profile_completed(user['id'])
            except Exception:
                app.logger.exception('Pharmacy profile setup failed')
                error = ('We could not save your profile right now. '
                         'Please try again in a moment.')
            else:
                return redirect(url_for('dashboard'))

    parsed_hours = _parse_hours(pharmacy.get('opening_hours')) if pharmacy else {}
    return render_template(
        'profile_setup.html',
        active_page=None,
        error=error,
        pharmacy=pharmacy or {},
        hours_weekday_open=(parsed_hours.get('weekdayOpen') or '08:00'),
        hours_weekday_close=(parsed_hours.get('weekdayClose') or '20:00'),
        hours_weekend_open=(parsed_hours.get('weekendOpen') or '09:00'),
        hours_weekend_close=(parsed_hours.get('weekendClose') or '15:00'),
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def _safe_next(raw):
    """Only allow same-site relative redirects after sign-in, so a crafted
    'next' value can never bounce the user to an external site. Falls back
    to the dashboard for any value that is missing, external, or scheme-http
    (e.g. '//evil.example')."""
    candidate = (raw or '').strip()
    if not candidate.startswith('/') or candidate.startswith('//'):
        return url_for('dashboard')
    return candidate


# --- Friendly error pages (404 / 403 / 500) ---
# Any unhandled exception renders a branded page instead of a bare browser
# error. JSON API callers still get JSON so the customer app can parse it.

def _wants_json():
    return request.path.startswith('/api/')


def _render_error(code, title, message, detail=None):
    return render_template(
        'error.html',
        code=code, title=title, message=message, detail=detail,
        signed_in=bool(session.get('user_id')),
    ), code


@app.errorhandler(404)
def not_found(e):
    if _wants_json():
        return jsonify(error='Not found.'), 404
    return _render_error(
        404, 'Page not found',
        'The page you are looking for may have moved or never existed.')


@app.errorhandler(403)
def forbidden(e):
    if _wants_json():
        return jsonify(error='Forbidden.'), 403
    return _render_error(
        403, 'Access denied',
        'Your account is not allowed to view this page.')


@app.errorhandler(500)
def server_error(e):
    app.logger.exception('Unhandled server error: %s', e)
    if _wants_json():
        return jsonify(error='Internal server error.'), 500
    return _render_error(
        500, 'Something went wrong',
        'An unexpected error occurred. Please try again in a moment.')


@app.errorhandler(Exception)
def unhandled_exception(e):
    # Let Flask's built-in handlers deal with 4xx/5xx HTTP exceptions; only
    # truly unexpected errors are converted into the branded 500 page.
    if isinstance(e, HTTPException):
        return e
    app.logger.exception('Unhandled exception during request')
    if _wants_json():
        return jsonify(error='Internal server error.'), 500
    return _render_error(
        500, 'Something went wrong',
        'An unexpected error occurred. Please try again in a moment.')


# --- Public APK download (Phase 4: hosted on Render) ---

APK_DIR = os.environ.get('PHARMATRACK_APK_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'static', 'downloads')
APK_FILENAME = 'pharmafinder-v1.0.0.apk'
APK_VERSION = '1.0.0'


def _apk_path():
    return os.path.join(APK_DIR, APK_FILENAME)


@app.route('/download')
def download_landing():
    """Landing page describing the customer app, with a download button."""
    return render_template('download.html', apk_version=APK_VERSION,
                           apk_url=url_for('apk_latest'))


@app.route('/apk/latest')
def apk_latest():
    """Serves the latest customer-app APK, forcing a browser download."""
    if not os.path.isfile(_apk_path()):
        abort(404, description="APK not available yet.")
    return send_from_directory(
        APK_DIR, APK_FILENAME,
        as_attachment=True,
        mimetype='application/vnd.android.package-archive',
        download_name=f'pharmafinder-v{APK_VERSION}.apk',
        max_age=0,  # clients must re-check: the file is replaced on release
    )


@app.route('/')
@login_required
def dashboard():
    if session.get('role') != 'admin':
        user = get_user_by_id(session.get('user_id'))
        if user and user.get('must_update_profile'):
            return redirect(url_for('profile_setup'))
    if session.get('role') == 'admin':
        return _admin_dashboard()
    user = get_user_by_id(session.get('user_id'))
    pharmacy_id = user.get('pharmacy_id') if user else None
    data = get_dashboard_data(pharmacy_id=pharmacy_id)
    return render_template('dashboard.html', active_page='dashboard', **data)


def _admin_dashboard():
    """The platform administrator's console: pharmacy accounts only.

    Mirrors the AdminDashboardPage of the reference app - an overview of
    every tenant (total/pending-approval/pending-setup/active/suspended/rejected)
    plus the most recently registered pharmacies. Day-to-day inventory and
    movement entry is the pharmacists' job; admins manage accounts."""

    def _status_counts(rows):
        counts = {"total": len(rows), "pending": 0, "pending_setup": 0,
                  "active": 0, "suspended": 0, "rejected": 0}
        for r in rows:
            ds = r.get("display_status", r["status"])
            counts[ds] = counts.get(ds, 0) + 1
        return counts

    pharmacies = get_pharmacies_with_applicant()
    stats = _status_counts(pharmacies)
    recent = sorted(
        (p for p in pharmacies if p["status"] != "rejected"),
        key=lambda p: p["created_at"] or "",
        reverse=True,
    )[:6]
    return render_template(
        'admin_dashboard.html',
        active_page='overview',
        topbar_title='Administrator Overview',
        stats=stats,
        recent=recent,
    )

@app.route('/products')
@pharmacist_required
def product_list():
    search = request.args.get('q', '').strip()
    user = get_user_by_id(session.get('user_id'))
    pharmacy_id = user.get('pharmacy_id') if user else None
    products = get_product_list(search=search if search else None, pharmacy_id=pharmacy_id)
    expiring_soon_count = sum(1 for p in products if p["expiry_status"] in ("Expired", "Expiring Soon"))
    low_stock_count = sum(1 for p in products if p["is_low_stock"])
    expiring_products = [p for p in products if p["expiry_status"] in ("Expired", "Expiring Soon")]
    low_stock_products = [p for p in products if p["is_low_stock"]]
    return render_template(
        'product_list.html', active_page='inventory', products=products, search_query=search,
        expiring_soon_count=expiring_soon_count, low_stock_count=low_stock_count,
        expiring_products=expiring_products, low_stock_products=low_stock_products)

@app.route('/products/<product_id>')
@pharmacist_required
def product_details(product_id):
    product = get_product_detail(product_id)
    if product is None:
        abort(404)
    return render_template('product_details.html', active_page='inventory', product=product)


def _parse_low_stock_threshold(raw):
    """Blank -> None (use the global setting). A positive whole number is
    returned as int; anything else (bad value) returns the string 'invalid'."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 'invalid'
    if value < 1:
        return 'invalid'
    return value


@app.route('/products/new', methods=['GET', 'POST'])
@pharmacist_required
def add_product():
    if request.method == 'POST':
        user = get_user_by_id(session.get('user_id'))
        pharmacy_id = user.get('pharmacy_id') if user else None
        required_label = {
            'name': 'Product Name', 'category': 'Category', 'strength': 'Strength',
            'dosage_form': 'Dosage Form', 'batch_number': 'Batch Number',
            'expiry_date': 'Expiry Date',
        }
        missing = [label for field, label in required_label.items()
                   if not (request.form.get(field) or '').strip()]
        if missing:
            return render_template(
                'add_product.html', active_page='inventory', error="Please fill in: " + ", ".join(missing),
                form_data=request.form), 400
        low_stock_threshold = _parse_low_stock_threshold(request.form.get('low_stock_threshold'))
        if low_stock_threshold == 'invalid':
            return render_template(
                'add_product.html', active_page='inventory',
                error="Low stock alert amount must be a whole number of 1 or more.",
                form_data=request.form), 400
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
            price_per_unit=request.form.get('price_per_unit') or None,
            price_per_packet=request.form.get('price_per_packet') or None,
            packet_size=request.form.get('packet_size') or None,
            unit_label=request.form.get('unit_label') or None,
            image_url=request.form.get('image_url') or None,
            low_stock_threshold=low_stock_threshold,
            pharmacy_id=pharmacy_id,
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
    user = get_user_by_id(session.get('user_id'))
    pharmacy_id = user.get('pharmacy_id') if user else None
    return jsonify(get_batches_for_product(product_id, pharmacy_id=pharmacy_id))

@app.route('/movements/new', methods=['GET', 'POST'])
@pharmacist_required
def record_movement():
    error = None
    form_data = {}
    user = get_user_by_id(session.get('user_id'))
    pharmacy_id = user.get('pharmacy_id') if user else None
    if request.method == 'POST':
        batch_id = request.form['batch_id']
        form_data = request.form
        try:
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
        except ValueError as exc:
            products = get_products_for_dropdown(pharmacy_id=pharmacy_id)
            return render_template(
                'record_movement.html', active_page='movements', products=products,
                error=str(exc), form_data=form_data), 400
        product_id = get_product_id_for_batch_owned_by(batch_id, pharmacy_id)
        if product_id is None:
            products = get_products_for_dropdown(pharmacy_id=pharmacy_id)
            return render_template(
                'record_movement.html', active_page='movements', products=products,
                error="This batch does not belong to your pharmacy.", form_data=form_data), 403
        return redirect(url_for('product_details', product_id=product_id))

    products = get_products_for_dropdown(pharmacy_id=pharmacy_id)
    return render_template('record_movement.html', active_page='movements', products=products, error=error, form_data=form_data)

@app.route('/products/<product_id>/edit', methods=['GET', 'POST'])
@pharmacist_required
def edit_product(product_id):
    # Deliberately NOT admin-only: editing product info is everyday
    # pharmacist work, same as adding products or recording movements.
    user = get_user_by_id(session.get('user_id'))
    pharmacy_id = user.get('pharmacy_id') if user else None
    product = get_product_detail(product_id, pharmacy_id=pharmacy_id)
    if product is None:
        abort(404)
    error = None
    if request.method == 'POST':
        batch_ids = request.form.getlist('batch_id')
        batch_nums = request.form.getlist('batch_number')
        expiries = request.form.getlist('expiry_date')
        # Validate every expiry date first so a bad value never causes a
        # partial save (product updated but batches not, or vice versa).
        for bid, bnum, exp in zip(batch_ids, batch_nums, expiries):
            if exp:
                try:
                    date.fromisoformat(exp)
                except ValueError:
                    error = f"Batch \"{bnum or bid}\" has an invalid expiry date (use YYYY-MM-DD)."
                    break
        low_stock_threshold = _parse_low_stock_threshold(request.form.get('low_stock_threshold'))
        if low_stock_threshold == 'invalid':
            error = "Low stock alert amount must be a whole number of 1 or more."
        if error:
            for key in ("name", "category", "strength", "dosage_form", "barcode",
                        "requires_prescription", "is_controlled", "price_per_unit",
                        "price_per_packet", "packet_size", "unit_label", "image_url",
                        "low_stock_threshold"):
                product[key] = request.form.get(key, product.get(key))
            product["requires_prescription"] = 1 if request.form.get('requires_prescription') == 'on' else 0
            product["is_controlled"] = 1 if request.form.get('is_controlled') == 'on' else 0
            product["batches"] = [
                {"id": bid, "batch_number": bnum or "", "expiry_date": exp or "",
                 "status": "Healthy", "quantity_remaining": 0}
                for bid, bnum, exp in zip(batch_ids, batch_nums, expiries)
            ]
            return render_template(
                'edit_product.html', active_page='inventory', product=product,
                error=error), 400
        update_product(
            product_id=product_id,
            name=request.form['name'],
            category=request.form.get('category'),
            strength=request.form.get('strength'),
            dosage_form=request.form.get('dosage_form'),
            barcode=request.form.get('barcode') or None,
            requires_prescription=request.form.get('requires_prescription') == 'on',
            is_controlled=request.form.get('is_controlled') == 'on',
            price_per_unit=request.form.get('price_per_unit') or None,
            price_per_packet=request.form.get('price_per_packet') or None,
            packet_size=request.form.get('packet_size') or None,
            unit_label=request.form.get('unit_label') or None,
            image_url=request.form.get('image_url') or None,
            low_stock_threshold=low_stock_threshold,
        )
        for bid, bnum, exp in zip(batch_ids, batch_nums, expiries):
            if bid:
                update_batch(bid, batch_number=bnum or None, expiry_date=exp or None)
        return redirect(url_for('product_details', product_id=product_id))

    return render_template('edit_product.html', active_page='inventory',
                           product=product, error=error)

@app.route('/preferences')
@login_required
def preferences():
    """Open to every logged-in user: personal display preferences
    (theme, language) - NOT system configuration, which lives in /settings."""
    return render_template('preferences.html', active_page='preferences')

@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """Admin-only: system-wide configuration, not personal preferences.

    Pharmacy profile lives in the shared `pharmacy` table (name, address,
    contact, location, hours, status) so the hosted customer API can serve
    the same data. Display-only settings (low-stock threshold) stay in the
    per-device settings table, which is fine for both desktop and hosted.
    """
    pharmacy_id = _current_admin_pharmacy_id()
    pharmacy = get_pharmacy(pharmacy_id) if pharmacy_id else None
    if pharmacy is None:
        # No pharmacy tenant yet - the auto-migration should have created
        # one, but be defensive about a deliberately empty database.
        pharmacy = {}

    if request.method == 'POST':
        opening_hours = {
            'weekdayOpen': request.form.get('hours_weekday_open') or None,
            'weekdayClose': request.form.get('hours_weekday_close') or None,
            'weekendOpen': request.form.get('hours_weekend_open') or None,
            'weekendClose': request.form.get('hours_weekend_close') or None,
        }
        update_pharmacy(
            pharmacy_id,
            name=request.form.get('pharmacy_name', ''),
            address=request.form.get('pharmacy_address', ''),
            city=request.form.get('pharmacy_city', ''),
            phone=request.form.get('pharmacy_phone', ''),
            emergency_phone=request.form.get('pharmacy_emergency_phone', ''),
            latitude=request.form.get('pharmacy_latitude') or None,
            longitude=request.form.get('pharmacy_longitude') or None,
            opening_hours=json.dumps(opening_hours),
            status=request.form.get('pharmacy_status') or 'active',
        )
        set_setting('low_stock_threshold', request.form.get('low_stock_threshold', '10'))
        return redirect(url_for('settings'))

    parsed_hours = _parse_hours(pharmacy.get('opening_hours'))
    return render_template(
        'settings.html',
        active_page='settings',
        pharmacy_id=pharmacy.get('id'),
        pharmacy_name=pharmacy.get('name', ''),
        pharmacy_address=pharmacy.get('address', ''),
        pharmacy_city=pharmacy.get('city', ''),
        pharmacy_phone=pharmacy.get('phone', ''),
        pharmacy_emergency_phone=pharmacy.get('emergency_phone', ''),
        pharmacy_latitude=pharmacy.get('latitude', ''),
        pharmacy_longitude=pharmacy.get('longitude', ''),
        pharmacy_status=pharmacy.get('status', 'active'),
        hours_weekday_open=parsed_hours.get('weekdayOpen') or '',
        hours_weekday_close=parsed_hours.get('weekdayClose') or '',
        hours_weekend_open=parsed_hours.get('weekendOpen') or '',
        hours_weekend_close=parsed_hours.get('weekendClose') or '',
        low_stock_threshold=get_setting('low_stock_threshold', '10'),
        total_products=len(get_product_list(pharmacy_id=pharmacy.get('id'))),
    )


def _current_admin_pharmacy_id():
    """The pharmacy the signed-in admin belongs to, or None."""
    user = get_user_by_id(session.get('user_id'))
    if user and user.get('pharmacy_id'):
        return user['pharmacy_id']
    # Fall back to the mailing tenant so a freshly-created admin with no
    # explicit pharmacy still lands on the default one.
    pharmacies = get_pharmacies()
    return pharmacies[0]['id'] if pharmacies else None


def _parse_hours(raw):
    """opening_hours is stored as JSON text. Never raises."""
    if not raw:
        return {}
    try:
        import json as _json
        return _json.loads(raw)
    except (TypeError, ValueError):
        return {}

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


# Approval actions for the pharmacy application queue. Each maps to the new
# pharmacy.status and the linked user status (kept in lock-step).
_PHARMACY_STATUS_ACTIONS = {
    'approve': ('active', 'active'),
    'activate': ('active', 'active'),
    'suspend': ('suspended', 'suspended'),
    'reject': ('rejected', 'pending'),
}


@app.route('/settings/pharmacies')
@admin_required
def manage_pharmacies():
    """Admin-only: every pharmacy account (or application), sorted with
    pending applications first, with Approve/Suspend/Reject controls."""
    return render_template(
        'manage_pharmacies.html',
        active_page='pharmacies',
        topbar_title='Manage Pharmacies',
        pharmacies=get_pharmacies_with_applicant(),
    )


@app.route('/settings/pharmacies/add', methods=['POST'])
@admin_required
def add_pharmacy():
    """Admin-provisioned pharmacy account (mirrors the Flutter dialog):
    name + contact email + a temporary password.  The new pharmacy is
    'active' immediately - no application step."""
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    error = None
    if not name or not email:
        error = "Pharmacy name and contact email are required."
    elif '@' not in email:
        error = "Enter a valid contact email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif user_name_exists(email):
        error = f"A login with {email} already exists. Use a different email."
    else:
        try:
            create_pharmacy_account(
                name=name,
                email=email,
                password=password,
                address=request.form.get('address', '').strip() or None,
                city=request.form.get('city', '').strip() or None,
                phone=request.form.get('phone', '').strip() or None,
            )
        except Exception:
            app.logger.exception('Admin-provisioned pharmacy creation failed')
            error = ('We could not create the pharmacy account right now. '
                     'Please try again in a moment.')
        else:
            message = f"Pharmacy account created. Send the credentials to {email} — they will be required to set up their profile and a new password on first login."
            return render_template(
                'manage_pharmacies.html',
                active_page='pharmacies',
                topbar_title='Manage Pharmacies',
                pharmacies=get_pharmacies_with_applicant(),
                success=message,
            )

    return render_template(
        'manage_pharmacies.html',
        active_page='pharmacies',
        topbar_title='Manage Pharmacies',
        pharmacies=get_pharmacies_with_applicant(),
        error=error,
        form=request.form,
    )


@app.route('/settings/pharmacies/<pharmacy_id>/<action>', methods=['POST'])
@admin_required
def pharmacy_action(pharmacy_id, action):
    if action == 'delete':
        delete_pharmacy_application(pharmacy_id)
        return redirect(url_for('manage_pharmacies'))
    if action not in _PHARMACY_STATUS_ACTIONS:
        abort(404)
    status, user_status = _PHARMACY_STATUS_ACTIONS[action]
    update_pharmacy_status(
        pharmacy_id, status,
        user_status=user_status,
        setup=(action == 'approve'),  # approved applicants must finish setup
    )
    return redirect(url_for('manage_pharmacies'))

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
    app.run(port=5000, debug=False, use_reloader=True)

def _open_desktop_app(app_url):
    try:
        import webview
        webview.create_window('PharmaTrack', app_url)
        webview.start()
    except Exception:
        print('pywebview has no GTK or Qt backend; opening PharmaTrack in your browser.')
        webbrowser.open(app_url)
        threading.Event().wait()

if __name__ == '__main__':
    import threading
    import webbrowser

    app_url = 'http://127.0.0.1:5000'

    # The reloader re-executes this file in a child process (marked by
    # WERKZEUG_RUN_MAIN) to serve the app. Open the window/browser only in the
    # original process so a file save doesn't spawn duplicate windows. Flask
    # itself must run in the main thread, because the reloader installs signal
    # handlers that fail from a background thread.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        threading.Thread(target=_open_desktop_app, args=(app_url,), daemon=True).start()

    start_flask()
