from django.db import migrations

from core.models import normalise_tags


def tidy(apps, schema_editor):
    Part = apps.get_model("core", "Part")
    changed = []
    for part in Part.objects.exclude(tags=""):
        tidied = normalise_tags(part.tags)
        if tidied != part.tags:
            part.tags = tidied
            changed.append(part)
    Part.objects.bulk_update(changed, ["tags"], batch_size=500)


def leave_alone(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0005_part_qty_to_buy")]
    operations = [migrations.RunPython(tidy, leave_alone)]
