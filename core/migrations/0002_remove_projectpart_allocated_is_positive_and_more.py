from django.db import migrations, models


def backfill_wanted(apps, schema_editor):
    ProjectPart = apps.get_model("core", "ProjectPart")
    ProjectPart.objects.update(qty_wanted=models.F("qty_allocated"))


def unbackfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="projectpart",
            name="allocated_is_positive",
        ),
        migrations.AddField(
            model_name="projectpart",
            name="qty_wanted",
            field=models.PositiveIntegerField(
                default=1, help_text="How many this build needs."
            ),
        ),
        migrations.AlterField(
            model_name="projectpart",
            name="qty_allocated",
            field=models.PositiveIntegerField(
                help_text="How many it actually got. Less than wanted means short."
            ),
        ),
        migrations.RunPython(backfill_wanted, unbackfill),
        migrations.AddConstraint(
            model_name="projectpart",
            constraint=models.CheckConstraint(
                condition=models.Q(("qty_wanted__gt", 0)), name="wanted_is_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="projectpart",
            constraint=models.CheckConstraint(
                condition=models.Q(("qty_allocated__lte", models.F("qty_wanted"))),
                name="allocated_not_over_wanted",
            ),
        ),
    ]
