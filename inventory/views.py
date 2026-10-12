import hashlib
import json
import logging
import re
import uuid
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import services
from .errors import ApplicationError, PayloadTooLargeError, UnauthorizedError, UnexpectedError
from .models import APIKey, Tenant

logger = logging.getLogger(__name__)
SKU_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def _json_body(request, allowed_fields):
    if request.content_type != "application/json":
        raise ApplicationError("Content-Type must be application/json", code="unsupported_content_type")
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError as exc:
        raise ApplicationError("Invalid Content-Length", code="invalid_content_length") from exc
    if content_length > settings.MAX_REQUEST_BYTES:
        raise PayloadTooLargeError("Request body is too large")
    try:
        payload = json.loads(request.body)
    except RequestDataTooBig as exc:
        raise PayloadTooLargeError("Request body is too large") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApplicationError("Body must be valid JSON", code="invalid_json") from exc
    if not isinstance(payload, dict):
        raise ApplicationError("JSON body must be an object", code="invalid_json_shape")
    if set(payload) - set(allowed_fields):
        raise ApplicationError("JSON body has unknown fields", code="unknown_fields")
    return payload


def _sku(value):
    if not isinstance(value, str) or not SKU_PATTERN.fullmatch(value):
        raise ApplicationError(
            "sku must be 1-100 letters, digits, hyphens, or underscores", code="invalid_sku"
        )
    return value


def _integer(value, name, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ApplicationError(
            f"{name} must be an integer from {minimum} to {maximum}", code=f"invalid_{name}"
        )
    return value


def _idempotency_key(request):
    value = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(value) <= 120 or not all(32 <= ord(char) <= 126 for char in value):
        raise ApplicationError(
            "Idempotency-Key must be 1-120 printable ASCII characters", code="invalid_idempotency_key"
        )
    return value


def _authenticate(request):
    raw_key = request.headers.get("X-API-Key", "")
    if not raw_key.startswith("inv_") or len(raw_key) > 100:
        raise UnauthorizedError("Valid API key required")
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = APIKey.objects.select_related("tenant").filter(key_hash=key_hash, revoked_at__isnull=True).first()
    if key is None:
        raise UnauthorizedError("Valid API key required")
    return key.tenant


def api(*, methods, authenticated=True):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            request_id = str(uuid.uuid4())
            try:
                if request.method not in methods:
                    return JsonResponse(
                        {"error": {"code": "method_not_allowed", "message": "Method not allowed"}},
                        status=405,
                        headers={"Allow": ", ".join(methods), "X-Request-ID": request_id},
                    )
                tenant = _authenticate(request) if authenticated else None
                response = view(request, tenant, *args, **kwargs)
            except ApplicationError as exc:
                response = JsonResponse(exc.payload(), status=exc.status_code)
            except Exception:
                logger.exception("Unhandled API error", extra={"request_id": request_id})
                error = UnexpectedError()
                response = JsonResponse(error.payload(), status=error.status_code)
            response["X-Request-ID"] = request_id
            return response

        return csrf_exempt(wrapped)

    return decorate


@api(methods={"GET"}, authenticated=False)
def live(_request, _tenant):
    return JsonResponse({"status": "ok"})


@api(methods={"GET"}, authenticated=False)
def ready(_request, _tenant):
    Tenant.objects.exists()
    return JsonResponse({"status": "ready"})


@api(methods={"POST"})
def items(request, tenant):
    payload = _json_body(request, {"sku", "initial_stock"})
    sku = _sku(payload.get("sku"))
    stock = _integer(payload.get("initial_stock"), "initial_stock", minimum=0, maximum=services.MAX_STOCK)
    return JsonResponse(services.create_item(tenant, sku, stock), status=201)


@api(methods={"GET"})
def item_detail(_request, tenant, sku):
    return JsonResponse(services.get_item(tenant, _sku(sku)))


@api(methods={"GET"})
def movements(request, tenant, sku):
    try:
        raw_limit = int(request.GET.get("limit", "50"))
    except ValueError as exc:
        raise ApplicationError("limit must be an integer", code="invalid_limit") from exc
    limit = _integer(raw_limit, "limit", minimum=1, maximum=100)
    return JsonResponse({"movements": services.item_movements(tenant, _sku(sku), limit)})


@api(methods={"POST"})
def adjustments(request, tenant, sku):
    payload = _json_body(request, {"delta"})
    delta = _integer(payload.get("delta"), "delta", minimum=-services.MAX_STOCK, maximum=services.MAX_STOCK)
    if delta == 0:
        raise ApplicationError("delta cannot be zero", code="invalid_delta")
    result = services.adjust_stock(tenant, _sku(sku), delta, _idempotency_key(request))
    return JsonResponse(result, status=200 if result["replayed"] else 201)


@api(methods={"POST"})
def reservations(request, tenant):
    payload = _json_body(request, {"sku", "quantity", "hold_seconds"})
    sku = _sku(payload.get("sku"))
    quantity = _integer(payload.get("quantity"), "quantity", minimum=1, maximum=services.MAX_STOCK)
    hold_seconds = _integer(
        payload.get("hold_seconds", settings.DEFAULT_HOLD_SECONDS),
        "hold_seconds",
        minimum=1,
        maximum=settings.MAX_HOLD_SECONDS,
    )
    result = services.create_reservation(tenant, sku, quantity, _idempotency_key(request), hold_seconds)
    return JsonResponse(result, status=200 if result["replayed"] else 201)


@api(methods={"GET"})
def reservation_detail(_request, tenant, reservation_id):
    return JsonResponse(services.get_reservation(tenant, reservation_id))


@api(methods={"POST"})
def commit_reservation(request, tenant, reservation_id):
    _json_body(request, set())
    return JsonResponse(services.transition_reservation(tenant, reservation_id, "committed"))


@api(methods={"POST"})
def cancel_reservation(request, tenant, reservation_id):
    _json_body(request, set())
    return JsonResponse(services.transition_reservation(tenant, reservation_id, "canceled"))
