import hashlib
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import APIKey, Tenant
from inventory.outbox import _safe_destination


class Command(BaseCommand):
    help = "Create a tenant and one API key. Capture the key once from stdout and store it securely."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("name")
        parser.add_argument("--webhook-url", default="")

    def handle(self, *args, **options):
        webhook_url = options["webhook_url"]
        if webhook_url and not _safe_destination(webhook_url):
            raise CommandError("Webhook URL must use HTTPS and have no credentials or fragment")
        raw_key = "inv_" + secrets.token_urlsafe(32)
        with transaction.atomic():
            if Tenant.objects.filter(slug=options["slug"]).exists():
                raise CommandError("Tenant slug already exists")
            tenant = Tenant.objects.create(
                slug=options["slug"], name=options["name"], webhook_url=webhook_url
            )
            APIKey.objects.create(
                tenant=tenant, name="default", key_hash=hashlib.sha256(raw_key.encode()).hexdigest()
            )
        self.stdout.write(f"tenant={tenant.slug}")
        self.stdout.write(f"api_key={raw_key}")
