"""Leased outbox delivery. Network calls happen outside database transactions."""

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import OutboxEvent

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _safe_destination(url):
    parsed = urllib.parse.urlsplit(url)
    allowed_schemes = {"https", "http"} if settings.WEBHOOK_ALLOW_HTTP else {"https"}
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        return False
    return (
        parsed.scheme in allowed_schemes
        and bool(parsed.hostname)
        and valid_port
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def _deliver(event):
    url = event.tenant.webhook_url
    if not _safe_destination(url):
        raise ValueError("Webhook destination is not a configured HTTPS URL")
    if not settings.WEBHOOK_SIGNING_SECRET:
        raise ValueError("WEBHOOK_SIGNING_SECRET is not configured")
    body = json.dumps(
        {
            "id": str(event.pk),
            "type": event.kind,
            "created_at": event.created_at.isoformat(),
            "data": event.payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.WEBHOOK_SIGNING_SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Event-Id": str(event.pk),
            "X-Event-Timestamp": timestamp,
            "X-Event-Signature": f"sha256={signature}",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"Webhook returned HTTP {response.status}")


def claim_due(limit=25):
    now = timezone.now()
    due = OutboxEvent.objects.filter(
        Q(status=OutboxEvent.Status.PENDING, next_attempt_at__lte=now)
        | Q(status=OutboxEvent.Status.PROCESSING, lease_until__lte=now)
    )
    with transaction.atomic():
        events = list(
            due.select_for_update(skip_locked=True, of=("self",))
            .select_related("tenant")
            .order_by("created_at")[:limit]
        )
        for event in events:
            event.status = OutboxEvent.Status.PROCESSING
            event.lease_token = uuid.uuid4()
            event.lease_until = now + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
            event.attempts += 1
            event.save(update_fields=["status", "lease_token", "lease_until", "attempts"])
    return events


def _finish(event_id, lease_token, error=None):
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.lease_token != lease_token or event.status != OutboxEvent.Status.PROCESSING:
            return None
        event.lease_token = None
        event.lease_until = None
        if error is None:
            event.status = OutboxEvent.Status.DELIVERED
            event.delivered_at = timezone.now()
            event.last_error = ""
        else:
            exhausted = event.attempts >= settings.OUTBOX_MAX_ATTEMPTS
            event.status = OutboxEvent.Status.DEAD if exhausted else OutboxEvent.Status.PENDING
            delay = min(30 * (2 ** min(event.attempts - 1, 8)), 3600)
            event.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            event.last_error = f"{type(error).__name__}: {str(error)[:180]}"[:240]
        event.save(
            update_fields=[
                "status",
                "lease_token",
                "lease_until",
                "delivered_at",
                "next_attempt_at",
                "last_error",
            ]
        )
        return event.status


def dispatch_once(limit=25, deliver=_deliver):
    events = claim_due(limit)
    result = {"claimed": len(events), "delivered": 0, "retrying": 0, "dead": 0, "stale": 0}
    for event in events:
        try:
            deliver(event)
        except (OSError, TimeoutError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            status = _finish(event.pk, event.lease_token, exc)
            logger.warning(
                "Outbox delivery failed", extra={"event_id": event.pk, "tenant_id": event.tenant_id}
            )
        else:
            status = _finish(event.pk, event.lease_token)
        result[
            {
                OutboxEvent.Status.DELIVERED: "delivered",
                OutboxEvent.Status.PENDING: "retrying",
                OutboxEvent.Status.DEAD: "dead",
                None: "stale",
            }[status]
        ] += 1
    return result
