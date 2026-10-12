import json

from django.core.management.base import BaseCommand

from inventory.services import reconcile_stock


class Command(BaseCommand):
    help = "Check stock counters against the movement ledger and active holds."

    def handle(self, *args, **options):
        result = reconcile_stock()
        self.stdout.write(json.dumps(result))
        if result["mismatch_count"]:
            raise SystemExit(1)
