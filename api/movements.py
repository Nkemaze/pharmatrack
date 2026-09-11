"""Stock-movement endpoints."""

from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity

from api import api_v1_bp
from api.auth import _api_error, get_api_pharmacy_id, role_required
from api.validation import (
    ValidationError, get_json_object, integer, optional_string,
    reject_unknown_fields, required_string,
)
from database.queries import (
    create_movement, get_product_id_for_batch, get_product_id_for_batch_owned_by,
)


@api_v1_bp.post('/movements')
@role_required('pharmacy', 'admin')
def add_movement():
    try:
        data = get_json_object()
        reject_unknown_fields(data, {
            'product_batch_id', 'movement_type', 'quantity', 'adjustment_direction',
            'counterparty_name', 'counterparty_address', 'reference_number',
            'prescription_number', 'reason',
        })
        batch_id = required_string(data, 'product_batch_id')
        movement_type = required_string(data, 'movement_type')
        if movement_type not in {'receipt', 'sale', 'adjustment', 'transfer', 'return', 'destruction', 'loss'}:
            return _api_error('movement_type is invalid.')
        adjustment_direction = optional_string(data, 'adjustment_direction')
        if movement_type == 'adjustment' and adjustment_direction not in {'add', 'remove'}:
            return _api_error('adjustment_direction must be add or remove for an adjustment.')
        if movement_type != 'adjustment' and adjustment_direction is not None:
            return _api_error('adjustment_direction is only allowed for an adjustment.')

        # An admin may record movements from any pharmacy (no JWT pharmacy
        # scope). A pharmacy-scoped account can only touch its own batches.
        pharmacy_id = get_api_pharmacy_id()
        if pharmacy_id:
            if get_product_id_for_batch_owned_by(batch_id, pharmacy_id) is None:
                return _api_error('Product batch not found.', 404)
        elif get_product_id_for_batch(batch_id) is None:
            return _api_error('Product batch not found.', 404)

        movement_id = create_movement(
            product_batch_id=batch_id, movement_type=movement_type,
            quantity=integer(data, 'quantity', minimum=1),
            adjustment_direction=adjustment_direction,
            counterparty_name=optional_string(data, 'counterparty_name'),
            counterparty_address=optional_string(data, 'counterparty_address'),
            reference_number=optional_string(data, 'reference_number'),
            prescription_number=optional_string(data, 'prescription_number'),
            reason=optional_string(data, 'reason'), performed_by_user_id=get_jwt_identity(),
        )
    except (TypeError, ValidationError, ValueError) as exc:
        return _api_error(str(exc))
    return jsonify(movement_id=movement_id), 201
