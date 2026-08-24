from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from core.models import MovementReason, Part


class Command(BaseCommand):
    help = "Check that every part's quantity matches its movement history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Write a correcting movement for any part that disagrees.",
        )
        parser.add_argument("--user", default=None, help="Limit to one username.")

    def handle(self, *args, **options):
        parts = Part.objects.all()
        if options["user"]:
            parts = parts.filter(user__username=options["user"])

        parts = parts.annotate(ledger=Sum("movements__delta"))

        drifted = []
        for part in parts:
            ledger = part.ledger or 0
            if ledger != part.qty_owned:
                drifted.append((part, ledger))

        if not drifted:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{parts.count()} parts checked, ledger and quantities agree."
                )
            )
            return

        self.stdout.write(
            self.style.WARNING(f"{len(drifted)} part(s) disagree with their history:")
        )
        for part, ledger in drifted:
            self.stdout.write(
                f"  {part}: quantity {part.qty_owned}, ledger says {ledger} "
                f"(off by {part.qty_owned - ledger})"
            )

        if not options["fix"]:
            self.stdout.write(
                "\nRe-run with --fix to reconcile. That writes a movement "
                "explaining the gap rather than quietly editing the number."
            )
            return

        with transaction.atomic():
            for part, ledger in drifted:
                gap = part.qty_owned - ledger
                part.movements.create(
                    delta=gap,
                    balance_after=part.qty_owned,
                    reason=MovementReason.CORRECTION,
                    note="Reconciled by check_stock: history was missing this.",
                )
        self.stdout.write(self.style.SUCCESS(f"Reconciled {len(drifted)} part(s)."))
