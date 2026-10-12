import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from inventory.models import OutboxEvent


class Command(BaseCommand):
    help = "Requeue one inspected dead outbox event after fixing its delivery failure."

    def add_arguments(self, parser):
        parser.add_argument("event_id")

    def handle(self, *args, **options):
        try:
            event_id = uuid.UUID(options["event_id"])
        except ValueError as exc:
            raise CommandError("event_id must be a UUID") from exc
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update().filter(pk=event_id).first()
            if event is None or event.status != OutboxEvent.Status.DEAD:
                raise CommandError("Dead event not found")
            event.status = OutboxEvent.Status.PENDING
            event.attempts = 0
            event.next_attempt_at = timezone.now()
            event.last_error = ""
            event.save(update_fields=["status", "attempts", "next_attempt_at", "last_error"])
        self.stdout.write(f"requeued={event.pk}")
