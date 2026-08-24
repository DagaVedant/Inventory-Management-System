from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_stockmovement'),
    ]

    operations = [
        migrations.AddField(
            model_name='projectpart',
            name='teardown_returned',
            field=models.PositiveIntegerField(blank=True, help_text='How much of qty_returned came from the teardown. Null until torn down. Soldered and broken need no equivalent: nothing but a teardown ever sets them.', null=True),
        ),
    ]
