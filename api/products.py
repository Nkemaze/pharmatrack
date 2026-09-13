"""Inventory endpoints. Query helpers keep SQL out of the API layer."""

from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required, get_jwt

from api import api_v1_bp
from api.auth import _api_error, get_api_pharmacy_id, role_required
from api.validation import (
    ValidationError, boolean, get_json_object, integer, iso_date,
    optional_string, reject_unknown_fields, required_string,
)
from database.queries import (
    create_product, get_batches_for_product, get_product_detail, get_product_list,
    get_public_inventory, update_product,
)

@api_v1_bp.get('/products')
@jwt_required(optional=True)
def list_products():
    """
    No token: public, safe view - no exact stock counts, no controlled
    substances. This is what the mobile app's customers use, with no
    login required at all.

    Valid pharmacy/admin token: full detailed view, same as the web app,
    scoped to the token's pharmacy (admin also sees only their own
    pharmacy unless they browse another's products).
    """
    search = request.args.get('q', '').strip() or None
    claims = get_jwt() or {}
    role = claims.get('role')

    if role in ('pharmacy', 'admin'):
        products = get_product_list(search, pharmacy_id=get_api_pharmacy_id())
    else:
        products = get_public_inventory(search)

    return jsonify(products=products)


@api_v1_bp.get('/products/<product_id>')
@role_required('pharmacy', 'admin')
def get_product(product_id):
    product = get_product_detail(product_id, pharmacy_id=get_api_pharmacy_id())
    if product is None:
        return _api_error('Product not found.', 404)
    return jsonify(product=product)


@api_v1_bp.post('/products')
@role_required('pharmacy', 'admin')
def add_product():
    try:
        data = get_json_object()
        reject_unknown_fields(data, {
            'name', 'category', 'strength', 'dosage_form', 'barcode',
            'requires_prescription', 'is_controlled', 'batch_number',
            'expiry_date', 'initial_quantity',
            'price_per_unit', 'price_per_packet', 'packet_size',
            'unit_label', 'image_url', 'low_stock_threshold',
        })
        missing = [f for f in ('name', 'category', 'strength', 'dosage_form',
                               'batch_number', 'expiry_date')
                   if not (data.get(f) or '').strip()]
        if missing:
            return _api_error('Missing required fields: ' + ', '.join(missing))
        product_id = create_product(
            name=required_string(data, 'name'),
            category=optional_string(data, 'category'),
            strength=optional_string(data, 'strength'),
            dosage_form=optional_string(data, 'dosage_form'),
            barcode=optional_string(data, 'barcode'),
            requires_prescription=boolean(data, 'requires_prescription'),
            is_controlled=boolean(data, 'is_controlled'),
            batch_number=optional_string(data, 'batch_number'),
            expiry_date=iso_date(data, 'expiry_date'),
            initial_quantity=integer(data, 'initial_quantity', default=0, minimum=0),
            performed_by_user_id=get_jwt_identity(),
            pharmacy_id=get_api_pharmacy_id(),
            price_per_unit=data.get('price_per_unit'),
            price_per_packet=data.get('price_per_packet'),
            packet_size=data.get('packet_size'),
            unit_label=optional_string(data, 'unit_label'),
            image_url=optional_string(data, 'image_url'),
            low_stock_threshold=integer(data, 'low_stock_threshold', default=None, minimum=1),
        )
    except (TypeError, ValidationError, ValueError) as exc:
        return _api_error(str(exc))
    return jsonify(product_id=product_id), 201


@api_v1_bp.put('/products/<product_id>')
@role_required('pharmacy', 'admin')
def edit_product(product_id):
    existing = get_product_detail(product_id, pharmacy_id=get_api_pharmacy_id())
    if existing is None:
        return _api_error('Product not found.', 404)
    try:
        data = get_json_object()
        reject_unknown_fields(data, {
            'name', 'category', 'strength', 'dosage_form', 'barcode',
            'requires_prescription', 'is_controlled',
            'price_per_unit', 'price_per_packet', 'packet_size',
            'unit_label', 'image_url', 'low_stock_threshold',
        })
        if not data:
            return _api_error('At least one field must be provided.')
        update_product(
            product_id,
            required_string(data, 'name') if 'name' in data else existing['name'],
            optional_string(data, 'category', default=existing['category']),
            optional_string(data, 'strength', default=existing['strength']),
            optional_string(data, 'dosage_form', default=existing['dosage_form']),
            optional_string(data, 'barcode', default=existing['barcode']),
            boolean(data, 'requires_prescription', default=bool(existing['requires_prescription'])),
            boolean(data, 'is_controlled', default=bool(existing['is_controlled'])),
            price_per_unit=data.get('price_per_unit', existing.get('price_per_unit')),
            price_per_packet=data.get('price_per_packet', existing.get('price_per_packet')),
            packet_size=data.get('packet_size', existing.get('packet_size')),
            unit_label=optional_string(data, 'unit_label', default=existing.get('unit_label')),
            image_url=optional_string(data, 'image_url', default=existing.get('image_url')),
            low_stock_threshold=integer(data, 'low_stock_threshold',
                                        default=existing.get('low_stock_threshold'), minimum=1),
        )
    except (ValidationError, ValueError) as exc:
        return _api_error(str(exc))
    return jsonify(product=get_product_detail(product_id))


@api_v1_bp.get('/products/<product_id>/batches')
@role_required('pharmacy', 'admin')
def list_batches(product_id):
    if get_product_detail(product_id, pharmacy_id=get_api_pharmacy_id()) is None:
        return _api_error('Product not found.', 404)
    return jsonify(batches=get_batches_for_product(product_id, pharmacy_id=get_api_pharmacy_id()))
