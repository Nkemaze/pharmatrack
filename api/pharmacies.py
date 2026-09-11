"""Public pharmacy-endpoint routes for the customer-facing API.

These are deliberately unauthenticated and safe: they only ever expose
active pharmacies and public, non-controlled product listings, never
exact stock quantities or admin data.
"""

from flask import jsonify, request

from api import api_v1_bp
from api.auth import _api_error
from database.queries import (
    get_popular_products, get_public_inventory, get_public_pharmacies,
    get_public_pharmacy,
)


@api_v1_bp.get('/pharmacies')
def list_pharmacies():
    """Every active pharmacy, for the customer app's pharmacy list."""
    return jsonify(pharmacies=get_public_pharmacies())


@api_v1_bp.get('/pharmacies/<pharmacy_id>')
def get_pharmacy(pharmacy_id):
    """One active pharmacy's profile (hours included)."""
    pharmacy = get_public_pharmacy(pharmacy_id)
    if pharmacy is None:
        return _api_error('Pharmacy not found.', 404)
    return jsonify(pharmacy=pharmacy)


@api_v1_bp.get('/pharmacies/<pharmacy_id>/products')
def pharmacy_products(pharmacy_id):
    """The public (safe) product listing for one pharmacy. Returns an
    empty list - not an error - for a valid active pharmacy with no
    stock; a 404 for an unknown or suspended pharmacy."""
    if get_public_pharmacy(pharmacy_id) is None:
        return _api_error('Pharmacy not found.', 404)
    search = request.args.get('q', '').strip() or None
    return jsonify(products=get_public_inventory(search=search, pharmacy_id=pharmacy_id))


@api_v1_bp.get('/products/search')
def search_products():
    """Cross-pharmacy, public product search used by the app's home screen."""
    term = request.args.get('q', '').strip()
    if not term:
        return jsonify(products=[])
    return jsonify(products=get_public_inventory(search=term))


@api_v1_bp.get('/products/popular')
def popular_products():
    """Aggregated 'popular medicines' list across all active pharmacies."""
    return jsonify(products=get_popular_products())