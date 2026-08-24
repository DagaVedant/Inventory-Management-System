"""Take a copy of everything, because the host holds the only one.

    python manage.py backup                 # writes backup-<timestamp>.json
    python manage.py backup --out mine.json
    python manage.py backup --to-stdout     # pipe it somewhere yourself

Restore with:

    python manage.py loaddata backup-....json

Once your real bin is in here that data is irreplaceable and lives on a single
Postgres instance. This is not a substitute for a real backup schedule; it is
the thing you can run in ten seconds before doing something risky.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Dump all app and account data to a JSON fixture."

    # Sessions and content types are regenerated on load and only add noise.
    APPS = ["core", "auth.user", "auth.group"]

    def add_arguments(self, parser):
        parser.add_argument("--out", default=None, help="File to write.")
        # Deliberately not called --stdout. call_command() maps a flag's
        # name onto its dest, so a --stdout flag swallows the stdout= kwarg
        # every caller uses to redirect output, and the redirect silently
        # stops working.
        parser.add_argument(
            "--to-stdout",
            action="store_true",
            dest="to_stdout",
            help="Write to standard output instead of a file.",
        )

    def handle(self, *args, **options):
        if options["to_stdout"]:
            # Pass our own stdout down, or dumpdata writes straight to the real
            # one and piping or capturing the output gets nothing. Clearing
            # `ending` matters just as much: the wrapper appends a newline to
            # every chunk it is handed, which shreds the JSON mid-token.
            was = self.stdout.ending
            self.stdout.ending = ""
            try:
                call_command("dumpdata", *self.APPS, indent=2, stdout=self.stdout)
            finally:
                self.stdout.ending = was
            return

        path = options["out"]
        if not path:
            stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
            path = f"backup-{stamp}.json"

        call_command("dumpdata", *self.APPS, indent=2, output=path)
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {path}. Restore with: loaddata {path}")
        )
