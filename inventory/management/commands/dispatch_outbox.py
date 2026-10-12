import time

from django.core.management.base import BaseCommand

from inventory.outbox import dispatch_once


class Command(BaseCommand):
    help = "Deliver signed outbox webhooks; use --forever for a long-running worker."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--forever", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000 or options["poll_seconds"] <= 0:
            raise ValueError("invalid worker options")
        while True:
            result = dispatch_once(options["limit"])
            self.stdout.write(str(result))
            if not options["forever"]:
                return
            time.sleep(options["poll_seconds"])
