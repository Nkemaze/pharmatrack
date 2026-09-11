"""JWT login and role checks shared by API endpoints."""

from functools import wraps

from flask import jsonify, request
from flask_jwt_extended import (
    create_access_token, create_refresh_token, get_jwt, get_jwt_identity,
    jwt_required, verify_jwt_in_request,
)

from database.queries import (
    authenticate_user, create_user, revoke_token, user_name_exists,
    login_attempt_lockout, record_login_attempt, clear_login_attempts,
    login_status_block,
)
from api import api_v1_bp
from api.validation import (
    ValidationError, get_json_object, reject_unknown_fields, required_string,
)


API_MAX_LOGIN_ATTEMPTS = 5
API_LOGIN_WINDOW_SECONDS = 60


def _api_error(message, status=400):
    """Return a JSON error body with its intended HTTP status code."""
    return jsonify(error=message), status


def normalize_api_role(role):
    """Maps the existing HTML application's 'pharmacist' role to 'pharmacy'."""
    return 'pharmacy' if role == 'pharmacist' else role


def get_api_pharmacy_id():
    """The pharmacy_id from the current JWT, or None for unscoped/admin roles."""
    return get_jwt().get('pharmacy_id')


def _api_login_key():
    """Rate-limit API sign-in attempts by the calling IP address."""
    return request.remote_addr or 'unknown'


def _remaining_login_lockout(key):
    return login_attempt_lockout('api', key, API_MAX_LOGIN_ATTEMPTS, API_LOGIN_WINDOW_SECONDS)


def _record_failed_api_login(key):
    record_login_attempt('api', key, API_MAX_LOGIN_ATTEMPTS, API_LOGIN_WINDOW_SECONDS)


def _clear_failed_api_logins(key):
    clear_login_attempts('api', key)


def role_required(*allowed_roles):
    """Require a valid JWT whose role is permitted for this endpoint."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            verify_jwt_in_request()
            if get_jwt().get('role') not in allowed_roles:
                return _api_error('This role is not permitted to access this resource.', 403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@api_v1_bp.post('/auth/login')
def login():
    """Exchange an existing PharmaTrack username and password for a JWT."""
    login_key = _api_login_key()
    retry_after = _remaining_login_lockout(login_key)
    if retry_after:
        response, status = _api_error('Too many login attempts. Try again later.', 429)
        response.headers['Retry-After'] = str(retry_after)
        return response, status

    try:
        data = get_json_object()
        reject_unknown_fields(data, {'name', 'password'})
        name = required_string(data, 'name')
        password = required_string(data, 'password')
    except ValidationError as exc:
        return _api_error(str(exc))

    account = authenticate_user(name, password)
    if not account:
        _record_failed_api_login(login_key)
        return _api_error('Invalid name or password.', 401)

    role = normalize_api_role(account['role'])
    if role not in {'pharmacy', 'admin', 'user'}:
        return _api_error('This account has no API role.', 403)

    # A valid credential does not mean the account may sign in yet: pharmacy
    # applications awaiting approval, and suspended/rejected tenants, are
    # blocked here just like the web UI blocks them.
    block_message = login_status_block(account)
    if block_message:
        return _api_error(block_message, 403)

    _clear_failed_api_logins(login_key)

    pharmacy_id = account.get('pharmacy_id')
    claims = {'role': role, 'name': account['name'], 'pharmacy_id': pharmacy_id}
    access_token = create_access_token(identity=account['id'], additional_claims=claims)
    refresh_token = create_refresh_token(identity=account['id'], additional_claims=claims)
    return jsonify(
        access_token=access_token, refresh_token=refresh_token,
        role=role, user_id=account['id'], pharmacy_id=pharmacy_id,
    )


@api_v1_bp.post('/auth/refresh')
@jwt_required(refresh=True)
def refresh():
    """Exchange a valid refresh token for a new access token."""
    claims = get_jwt()
    access_token = create_access_token(
        identity=get_jwt_identity(),
        additional_claims={
            'role': claims.get('role'),
            'name': claims.get('name'),
            'pharmacy_id': claims.get('pharmacy_id'),
        },
    )
    return jsonify(access_token=access_token)


@api_v1_bp.post('/auth/logout')
@jwt_required(verify_type=False)
def logout():
    """Revoke the current access or refresh token."""
    token = get_jwt()
    revoke_token(
        jti=token['jti'], token_type=token['type'], user_id=get_jwt_identity(),
        expires_at=token['exp'],
    )
    return jsonify(message='Token revoked successfully.')


@api_v1_bp.post('/users')
@role_required('admin')
def create_api_user():
    """Allow an admin to provision pharmacy or read-only API accounts."""
    try:
        data = get_json_object()
        reject_unknown_fields(data, {'name', 'password', 'role', 'pharmacy_id'})
        name = required_string(data, 'name')
        password = required_string(data, 'password')
        requested_role = required_string(data, 'role')
        pharmacy_id = data.get('pharmacy_id')
    except ValidationError as exc:
        return _api_error(str(exc))

    if len(password) < 8:
        return _api_error('password must be at least 8 characters.')
    if requested_role not in {'pharmacy', 'user'}:
        return _api_error('role must be pharmacy or user.')
    if user_name_exists(name):
        return _api_error('An account with that name already exists.', 409)

    # The desktop application calls pharmacy staff "pharmacist". Keep that
    # database value so web and API login continue to use the same account.
    database_role = 'pharmacist' if requested_role == 'pharmacy' else 'user'
    user_id = create_user(name=name, role=database_role, password=password,
                          pharmacy_id=pharmacy_id)
    return jsonify(user_id=user_id, name=name, role=requested_role,
                   pharmacy_id=pharmacy_id), 201
