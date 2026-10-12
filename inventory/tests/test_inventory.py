import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import skipUnless

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from inventory import services
from inventory.errors import ConflictError
from inventory.models import APIKey, Item, OutboxEvent, Reservation, StockMovement, Tenant
from inventory.outbox import _finish, dispatch_once


class InventoryAPITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(slug="shop-a", name="Shop A")
        self.other = Tenant.objects.create(slug="shop-b", name="Shop B")
        self.key = "inv_test_key_a"
        APIKey.objects.create(
            tenant=self.tenant, name="test", key_hash=hashlib.sha256(self.key.encode()).hexdigest()
        )
        self.client = Client()

    def request(self, method, path, payload=None, *, token=None, key=None):
        headers = {"HTTP_X_API_KEY": key or self.key}
        if token is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = token
        if method == "get":
            return self.client.get(path, **headers)
        return getattr(self.client, method)(
            path,
            data=json.dumps(payload if payload is not None else {}),
            content_type="application/json",
            **headers,
        )

    def test_reservation_commit_idempotency_and_ledger(self):
        self.assertEqual(
            self.request("post", "/v1/items", {"sku": "book", "initial_stock": 10}).status_code, 201
        )
        created = self.request("post", "/v1/reservations", {"sku": "book", "quantity": 4}, token="order-1")
        self.assertEqual(created.status_code, 201)
        reservation_id = created.json()["id"]
        replay = self.request("post", "/v1/reservations", {"sku": "book", "quantity": 4}, token="order-1")
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        conflict = self.request("post", "/v1/reservations", {"sku": "book", "quantity": 5}, token="order-1")
        self.assertEqual(conflict.status_code, 409)
        oversell = self.request("post", "/v1/reservations", {"sku": "book", "quantity": 7}, token="order-2")
        self.assertEqual(oversell.status_code, 409)
        committed = self.request("post", f"/v1/reservations/{reservation_id}/commit")
        self.assertEqual(committed.status_code, 200)
        self.assertEqual(
            self.request("post", f"/v1/reservations/{reservation_id}/commit").json()["replayed"], True
        )
        self.assertEqual(self.request("post", f"/v1/reservations/{reservation_id}/cancel").status_code, 409)
        self.assertEqual(self.request("get", "/v1/items/book").json()["available"], 6)
        self.assertEqual(StockMovement.objects.filter(tenant=self.tenant).count(), 3)
        self.assertEqual(services.reconcile_stock()["mismatch_count"], 0)

    def test_adjustment_replay_and_active_hold_guard(self):
        self.request("post", "/v1/items", {"sku": "chair", "initial_stock": 5})
        self.request("post", "/v1/reservations", {"sku": "chair", "quantity": 4}, token="hold-1")
        blocked = self.request("post", "/v1/items/chair/adjustments", {"delta": -2}, token="adjust-1")
        self.assertEqual(blocked.status_code, 409)
        adjusted = self.request("post", "/v1/items/chair/adjustments", {"delta": 2}, token="adjust-1")
        self.assertEqual(adjusted.status_code, 201)
        self.assertEqual(
            self.request("post", "/v1/items/chair/adjustments", {"delta": 2}, token="adjust-1").status_code,
            200,
        )
        self.assertEqual(
            self.request("post", "/v1/items/chair/adjustments", {"delta": 3}, token="adjust-1").status_code,
            409,
        )
        self.assertEqual(Item.objects.get(tenant=self.tenant, sku="chair").on_hand, 7)
        self.assertEqual(services.reconcile_stock()["mismatch_count"], 0)

    def test_expiry_releases_stock_before_commit_conflict(self):
        self.request("post", "/v1/items", {"sku": "lamp", "initial_stock": 1})
        reservation_id = self.request(
            "post", "/v1/reservations", {"sku": "lamp", "quantity": 1, "hold_seconds": 10}, token="hold-2"
        ).json()["id"]
        Reservation.objects.filter(pk=reservation_id).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.request("post", f"/v1/reservations/{reservation_id}/commit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "reservation_expired")
        self.assertEqual(Reservation.objects.get(pk=reservation_id).status, Reservation.Status.EXPIRED)
        self.assertEqual(Item.objects.get(tenant=self.tenant, sku="lamp").available, 1)
        self.assertEqual(services.reconcile_stock()["mismatch_count"], 0)

    def test_tenant_isolation_auth_and_validation(self):
        Item.objects.create(tenant=self.other, sku="private", on_hand=3)
        self.assertEqual(self.request("get", "/v1/items/private").status_code, 404)
        self.assertEqual(self.request("get", "/v1/items/private", key="invalid").status_code, 401)
        self.assertEqual(
            self.request("post", "/v1/items", {"sku": "good", "initial_stock": True}).status_code, 400
        )
        self.assertEqual(
            self.request(
                "post", "/v1/reservations", {"sku": "private", "quantity": 1}, token="tenant-leak"
            ).status_code,
            404,
        )
        too_large = self.request(
            "post", "/v1/items", {"sku": "large", "initial_stock": 0, "padding": "x" * 65_000}
        )
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(
            self.client.post(
                "/v1/items", data="{}", content_type="text/plain", HTTP_X_API_KEY=self.key
            ).status_code,
            400,
        )

    def test_outbox_retry_and_stale_lease(self):
        event = OutboxEvent.objects.create(tenant=self.tenant, kind="test.event", payload={"ok": True})

        def fail(_event):
            raise TimeoutError("simulated timeout")

        first = dispatch_once(deliver=fail)
        self.assertEqual(first["retrying"], 1)
        event.refresh_from_db()
        self.assertEqual(event.attempts, 1)
        OutboxEvent.objects.filter(pk=event.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        second = dispatch_once(deliver=lambda _event: None)
        self.assertEqual(second["delivered"], 1)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxEvent.Status.DELIVERED)
        self.assertIsNone(_finish(event.pk, event.lease_token))

    def test_dead_event_can_be_requeued_by_operator(self):
        event = OutboxEvent.objects.create(tenant=self.tenant, kind="test.event", payload={"ok": True})

        def fail(_event):
            raise TimeoutError("simulated timeout")

        with override_settings(OUTBOX_MAX_ATTEMPTS=1):
            self.assertEqual(dispatch_once(deliver=fail)["dead"], 1)
        call_command("requeue_dead_event", str(event.pk), stdout=io.StringIO())
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts), (OutboxEvent.Status.PENDING, 0))


@skipUnless(connection.vendor == "postgresql", "Row-lock concurrency requires PostgreSQL")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_two_concurrent_holds_cannot_oversell(self):
        tenant = Tenant.objects.create(slug="concurrent", name="Concurrent")
        Item.objects.create(tenant=tenant, sku="single", on_hand=1)
        barrier = threading.Barrier(2)

        def reserve(token):
            close_old_connections()
            barrier.wait()
            try:
                services.create_reservation(tenant, "single", 1, token, 30)
                return "held"
            except ConflictError:
                return "conflict"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("a", "b")))
        self.assertCountEqual(results, ["held", "conflict"])
        item = Item.objects.get(tenant=tenant, sku="single")
        self.assertEqual((item.on_hand, item.reserved), (1, 1))
