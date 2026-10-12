import time

from django.core.management.base import BaseCommand

from inventory.services import expire_due_reservations


class Command(BaseCommand):
    help = "Continuously expire due reservations and release stock."

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        if options["poll_seconds"] <= 0:
            raise ValueError("poll-seconds must be positive")
        while True:
            expired = expire_due_reservations()
            if expired:
                self.stdout.write(f"expired={expired}")
            time.sleep(options["poll_seconds"])
