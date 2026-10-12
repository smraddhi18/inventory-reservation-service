import uuid

from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Tenant(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    webhook_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class APIKey(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=80)
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)


class Item(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="items")
    sku = models.SlugField(max_length=100)
    on_hand = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "sku"], name="unique_tenant_sku"),
            models.CheckConstraint(condition=Q(reserved__lte=F("on_hand")), name="reserved_not_above_stock"),
        ]

    @property
    def available(self):
        return self.on_hand - self.reserved


class Reservation(models.Model):
    class Status(models.TextChoices):
        HELD = "held"
        COMMITTED = "committed"
        CANCELED = "canceled"
        EXPIRED = "expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="reservations")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="reservations")
    client_token = models.CharField(max_length=120)
    quantity = models.PositiveIntegerField()
    hold_seconds = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.HELD)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "client_token"], name="unique_reservation_token"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_quantity_positive"),
        ]
        indexes = [models.Index(fields=["status", "expires_at"], name="reservation_due_idx")]


class StockMovement(models.Model):
    class Kind(models.TextChoices):
        INITIAL = "initial"
        ADJUSTMENT = "adjustment"
        RESERVED = "reserved"
        COMMITTED = "committed"
        RELEASED = "released"
        EXPIRED = "expired"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    reservation = models.ForeignKey(Reservation, on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    on_hand_delta = models.IntegerField(default=0)
    reserved_delta = models.IntegerField(default=0)
    on_hand_after = models.PositiveIntegerField()
    reserved_after = models.PositiveIntegerField()
    request_token = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "request_token"],
                condition=~Q(request_token=""),
                name="unique_stock_request_token",
            ),
            models.UniqueConstraint(
                fields=["reservation", "kind"],
                condition=Q(reservation__isnull=False),
                name="unique_reservation_movement",
            ),
        ]
        indexes = [models.Index(fields=["item", "created_at"], name="movement_item_time_idx")]


class OutboxEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PROCESSING = "processing"
        DELIVERED = "delivered"
        DEAD = "dead"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="outbox_events")
    kind = models.CharField(max_length=80)
    payload = models.JSONField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="outbox_due_idx")]
