from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Dump all app and account data to a JSON fixture."

    APPS = ["core", "auth.user", "auth.group"]

    def add_arguments(self, parser):
        parser.add_argument("--out", default=None, help="File to write.")
        parser.add_argument(
            "--to-stdout",
            action="store_true",
            dest="to_stdout",
            help="Write to standard output instead of a file.",
        )

    def handle(self, *args, **options):
        if options["to_stdout"]:
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
