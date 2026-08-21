from django.db import migrations

from core.models import normalise_tags


def tidy(apps, schema_editor):
    """Rewrite existing tags in the canonical form save() now produces.

    Without this, parts saved before normalisation keep whatever spacing they
    were typed with, and exact tag filtering silently misses them.
    """
    Part = apps.get_model("core", "Part")
    changed = []
    for part in Part.objects.exclude(tags=""):
        tidied = normalise_tags(part.tags)
        if tidied != part.tags:
            part.tags = tidied
            changed.append(part)
    Part.objects.bulk_update(changed, ["tags"], batch_size=500)


def leave_alone(apps, schema_editor):
    """Nothing to undo: the tidy form is still valid input."""


class Migration(migrations.Migration):
    dependencies = [("core", "0005_part_qty_to_buy")]
    operations = [migrations.RunPython(tidy, leave_alone)]
