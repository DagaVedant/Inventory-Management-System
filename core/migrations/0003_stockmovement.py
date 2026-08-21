# Generated, then given a backfill by hand. Django 6.1 on 2026-08-21 19:20

import django.db.models.deletion
from django.db import migrations, models


def opening_balances(apps, schema_editor):
    """Give every existing part a starting line so its history isn't blank.

    Without this the ledger begins mid-story: a part you own 180 of would show
    no movements at all, and the first recount would look like it came from
    nowhere.
    """
    Part = apps.get_model("core", "Part")
    StockMovement = apps.get_model("core", "StockMovement")
    StockMovement.objects.bulk_create(
        [
            StockMovement(
                part=part,
                delta=part.qty_owned,
                balance_after=part.qty_owned,
                reason="opening",
                note="Balance when history started.",
            )
            for part in Part.objects.all()
            if part.qty_owned
        ]
    )


def drop_openings(apps, schema_editor):
    """Nothing to undo: the table is about to be dropped."""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_remove_projectpart_allocated_is_positive_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockMovement",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "delta",
                    models.IntegerField(help_text="Signed. Negative means it left."),
                ),
                ("balance_after", models.PositiveIntegerField()),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("opening", "Opening balance"),
                            ("purchase", "Bought or found"),
                            ("correction", "Recount"),
                            ("teardown", "Consumed by a teardown"),
                            ("reopen", "Teardown reversed"),
                            ("merge", "Merged from a duplicate"),
                        ],
                        max_length=20,
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "part",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="movements",
                        to="core.part",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        help_text="Set when a teardown caused this.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="movements",
                        to="core.project",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
                "indexes": [
                    models.Index(
                        fields=["part", "-created_at"],
                        name="core_stockm_part_id_325629_idx",
                    )
                ],
            },
        ),
        migrations.RunPython(opening_balances, drop_openings),
    ]
