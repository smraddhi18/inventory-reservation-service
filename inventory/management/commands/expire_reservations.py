from django.core.management.base import BaseCommand

from inventory.services import expire_due_reservations


class Command(BaseCommand):
    help = "Expire due holds and release their stock. Schedule this command every few seconds."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise ValueError("limit must be from 1 to 1000")
        self.stdout.write(f"expired={expire_due_reservations(options['limit'])}")
