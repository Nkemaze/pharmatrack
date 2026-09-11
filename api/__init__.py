"""Version 1 JSON API for PharmaTrack."""

from flask import Blueprint, jsonify
from database.db import get_db_connection


api_v1_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')


@api_v1_bp.route('/health')
def health():
    """Liveness probe used by Render's health check and by the customer app
    to confirm the hosted backend is reachable."""
    conn = get_db_connection()
    try:
        conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    finally:
        conn.close()
    return jsonify({"status": "ok" if db_ok else "degraded", "db": db_ok})


# Import route modules after the Blueprint exists so their decorators attach
# endpoints to this single, versioned API namespace.
from api import auth, movements, products, reports, pharmacies  # noqa: E402, F401