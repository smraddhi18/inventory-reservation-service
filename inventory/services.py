"""Inventory state transitions. All stock changes and outbox writes share a transaction."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from .errors import ConflictError, NotFoundError
from .models import Item, OutboxEvent, Reservation, StockMovement

MAX_STOCK = 2_000_000_000


def item_data(item):
    return {
        "sku": item.sku,
        "on_hand": item.on_hand,
        "reserved": item.reserved,
        "available": item.available,
        "updated_at": item.updated_at.isoformat(),
    }


def reservation_data(reservation):
    return {
        "id": str(reservation.pk),
        "sku": reservation.item.sku,
        "quantity": reservation.quantity,
        "status": reservation.status,
        "client_token": reservation.client_token,
        "expires_at": reservation.expires_at.isoformat(),
        "created_at": reservation.created_at.isoformat(),
        "updated_at": reservation.updated_at.isoformat(),
    }


def _emit(tenant, kind, payload):
    return OutboxEvent.objects.create(tenant=tenant, kind=kind, payload=payload)


def _movement(tenant, item, kind, *, reservation=None, on_hand_delta=0, reserved_delta=0, request_token=""):
    return StockMovement.objects.create(
        tenant=tenant,
        item=item,
        reservation=reservation,
        kind=kind,
        on_hand_delta=on_hand_delta,
        reserved_delta=reserved_delta,
        on_hand_after=item.on_hand,
        reserved_after=item.reserved,
        request_token=request_token,
    )


def create_item(tenant, sku, initial_stock):
    with transaction.atomic():
        if Item.objects.filter(tenant=tenant, sku=sku).exists():
            raise ConflictError("SKU already exists", code="sku_exists")
        try:
            with transaction.atomic():
                item = Item.objects.create(tenant=tenant, sku=sku, on_hand=initial_stock)
        except IntegrityError as exc:
            raise ConflictError("SKU already exists", code="sku_exists") from exc
        _movement(tenant, item, StockMovement.Kind.INITIAL, on_hand_delta=initial_stock)
        _emit(tenant, "inventory.item_created", {"sku": sku, "on_hand": initial_stock})
        return item_data(item)


def get_item(tenant, sku):
    item = Item.objects.filter(tenant=tenant, sku=sku).first()
    if item is None:
        raise NotFoundError("SKU not found", code="sku_not_found")
    return item_data(item)


def item_movements(tenant, sku, limit=50):
    item = Item.objects.filter(tenant=tenant, sku=sku).first()
    if item is None:
        raise NotFoundError("SKU not found", code="sku_not_found")
    records = StockMovement.objects.filter(tenant=tenant, item=item).order_by("-created_at", "-pk")[:limit]
    return [
        {
            "id": record.pk,
            "kind": record.kind,
            "reservation_id": str(record.reservation_id) if record.reservation_id else None,
            "on_hand_delta": record.on_hand_delta,
            "reserved_delta": record.reserved_delta,
            "on_hand_after": record.on_hand_after,
            "reserved_after": record.reserved_after,
            "created_at": record.created_at.isoformat(),
        }
        for record in records
    ]


def _locked_item(tenant, sku):
    item = Item.objects.select_for_update().filter(tenant=tenant, sku=sku).first()
    if item is None:
        raise NotFoundError("SKU not found", code="sku_not_found")
    return item


def adjust_stock(tenant, sku, delta, request_token):
    with transaction.atomic():
        item = _locked_item(tenant, sku)
        previous = StockMovement.objects.filter(tenant=tenant, request_token=request_token).first()
        if previous is not None:
            if (
                previous.item_id != item.pk
                or previous.on_hand_delta != delta
                or previous.kind != StockMovement.Kind.ADJUSTMENT
            ):
                raise ConflictError(
                    "Request token was reused with different input", code="idempotency_conflict"
                )
            return {
                "sku": sku,
                "on_hand": previous.on_hand_after,
                "reserved": previous.reserved_after,
                "available": previous.on_hand_after - previous.reserved_after,
                "replayed": True,
            }
        new_stock = item.on_hand + delta
        if new_stock < item.reserved:
            raise ConflictError(
                "Adjustment would reduce stock below active holds", code="stock_below_reserved"
            )
        if new_stock > MAX_STOCK:
            raise ConflictError("Stock limit exceeded", code="stock_limit")
        item.on_hand = new_stock
        item.updated_at = timezone.now()
        item.save(update_fields=["on_hand", "updated_at"])
        try:
            with transaction.atomic():
                _movement(
                    tenant,
                    item,
                    StockMovement.Kind.ADJUSTMENT,
                    on_hand_delta=delta,
                    request_token=request_token,
                )
        except IntegrityError as exc:
            raise ConflictError("Request token was reused", code="idempotency_conflict") from exc
        _emit(
            tenant,
            "inventory.stock_adjusted",
            {"sku": sku, "delta": delta, "on_hand": new_stock, "request_token": request_token},
        )
        return {**item_data(item), "replayed": False}


def create_reservation(tenant, sku, quantity, client_token, hold_seconds):
    with transaction.atomic():
        item = _locked_item(tenant, sku)
        previous = (
            Reservation.objects.filter(tenant=tenant, client_token=client_token)
            .select_related("item")
            .first()
        )
        if previous is not None:
            if (
                previous.item_id != item.pk
                or previous.quantity != quantity
                or previous.hold_seconds != hold_seconds
            ):
                raise ConflictError(
                    "Client token was reused with different input", code="idempotency_conflict"
                )
            return {**reservation_data(previous), "replayed": True}
        if item.available < quantity:
            raise ConflictError("Insufficient available stock", code="insufficient_stock")
        now = timezone.now()
        try:
            with transaction.atomic():
                reservation = Reservation.objects.create(
                    tenant=tenant,
                    item=item,
                    quantity=quantity,
                    hold_seconds=hold_seconds,
                    client_token=client_token,
                    expires_at=now + timedelta(seconds=hold_seconds),
                    created_at=now,
                    updated_at=now,
                )
        except IntegrityError as exc:
            raise ConflictError("Client token was reused", code="idempotency_conflict") from exc
        item.reserved += quantity
        item.updated_at = now
        item.save(update_fields=["reserved", "updated_at"])
        _movement(tenant, item, StockMovement.Kind.RESERVED, reservation=reservation, reserved_delta=quantity)
        _emit(
            tenant,
            "inventory.reservation_held",
            {
                "reservation_id": str(reservation.pk),
                "sku": sku,
                "quantity": quantity,
                "expires_at": reservation.expires_at.isoformat(),
            },
        )
        return {**reservation_data(reservation), "replayed": False}


def get_reservation(tenant, reservation_id):
    reservation = Reservation.objects.filter(tenant=tenant, pk=reservation_id).select_related("item").first()
    if reservation is None:
        raise NotFoundError("Reservation not found", code="reservation_not_found")
    return reservation_data(reservation)


def _locked_reservation_and_item(tenant, reservation_id):
    item_id = (
        Reservation.objects.filter(tenant=tenant, pk=reservation_id).values_list("item_id", flat=True).first()
    )
    if item_id is None:
        raise NotFoundError("Reservation not found", code="reservation_not_found")
    item = Item.objects.select_for_update().get(pk=item_id, tenant=tenant)
    reservation = (
        Reservation.objects.select_for_update().select_related("item").get(pk=reservation_id, tenant=tenant)
    )
    return reservation, item


def _expire_locked(reservation, item, now):
    item.reserved -= reservation.quantity
    item.updated_at = now
    item.save(update_fields=["reserved", "updated_at"])
    reservation.status = Reservation.Status.EXPIRED
    reservation.updated_at = now
    reservation.save(update_fields=["status", "updated_at"])
    _movement(
        reservation.tenant,
        item,
        StockMovement.Kind.EXPIRED,
        reservation=reservation,
        reserved_delta=-reservation.quantity,
    )
    _emit(
        reservation.tenant,
        "inventory.reservation_expired",
        {"reservation_id": str(reservation.pk), "sku": item.sku, "quantity": reservation.quantity},
    )


def transition_reservation(tenant, reservation_id, action):
    expired_now = False
    with transaction.atomic():
        reservation, item = _locked_reservation_and_item(tenant, reservation_id)
        now = timezone.now()
        if reservation.status == Reservation.Status.HELD and reservation.expires_at <= now:
            _expire_locked(reservation, item, now)
            expired_now = True
        if not expired_now:
            if reservation.status == action:
                return {**reservation_data(reservation), "replayed": True}
            if reservation.status != Reservation.Status.HELD:
                raise ConflictError("Reservation is no longer active", code="invalid_reservation_state")
            item.reserved -= reservation.quantity
            if action == Reservation.Status.COMMITTED:
                item.on_hand -= reservation.quantity
            item.updated_at = now
            item.save(update_fields=["on_hand", "reserved", "updated_at"])
            reservation.status = action
            reservation.updated_at = now
            reservation.save(update_fields=["status", "updated_at"])
            kind = (
                StockMovement.Kind.COMMITTED
                if action == Reservation.Status.COMMITTED
                else StockMovement.Kind.RELEASED
            )
            hand_delta = -reservation.quantity if action == Reservation.Status.COMMITTED else 0
            _movement(
                tenant,
                item,
                kind,
                reservation=reservation,
                on_hand_delta=hand_delta,
                reserved_delta=-reservation.quantity,
            )
            _emit(
                tenant,
                f"inventory.reservation_{action}",
                {"reservation_id": str(reservation.pk), "sku": item.sku, "quantity": reservation.quantity},
            )
            return {**reservation_data(reservation), "replayed": False}
    if expired_now:
        raise ConflictError("Reservation expired", code="reservation_expired")
    raise RuntimeError("Unhandled reservation transition")


def expire_due_reservations(limit=100):
    candidate_ids = list(
        Reservation.objects.filter(status=Reservation.Status.HELD, expires_at__lte=timezone.now())
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    expired = 0
    for reservation_id in candidate_ids:
        with transaction.atomic():
            item_id = Reservation.objects.filter(pk=reservation_id).values_list("item_id", flat=True).first()
            if item_id is None:
                continue
            item = Item.objects.select_for_update().get(pk=item_id)
            reservation = (
                Reservation.objects.select_for_update().select_related("tenant").get(pk=reservation_id)
            )
            now = timezone.now()
            if reservation.status != Reservation.Status.HELD or reservation.expires_at > now:
                continue
            _expire_locked(reservation, item, now)
            expired += 1
    return expired


def reconcile_stock():
    movement_totals = {
        row["item_id"]: row
        for row in StockMovement.objects.values("item_id").annotate(
            on_hand_total=Sum("on_hand_delta"),
            reserved_total=Sum("reserved_delta"),
        )
    }
    held_totals = {
        row["item_id"]: row["quantity_total"]
        for row in Reservation.objects.filter(status=Reservation.Status.HELD)
        .values("item_id")
        .annotate(quantity_total=Sum("quantity"))
    }
    mismatches = []
    for item in Item.objects.all().iterator():
        movement = movement_totals.get(item.pk, {})
        expected_on_hand = movement.get("on_hand_total", 0)
        expected_reserved = movement.get("reserved_total", 0)
        held_quantity = held_totals.get(item.pk, 0)
        if (item.on_hand, item.reserved) != (
            expected_on_hand,
            expected_reserved,
        ) or item.reserved != held_quantity:
            mismatches.append(
                {
                    "tenant_id": item.tenant_id,
                    "sku": item.sku,
                    "actual_on_hand": item.on_hand,
                    "movement_on_hand": expected_on_hand,
                    "actual_reserved": item.reserved,
                    "movement_reserved": expected_reserved,
                    "held_quantity": held_quantity,
                }
            )
    return {
        "checked_at": timezone.now().isoformat(),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }
