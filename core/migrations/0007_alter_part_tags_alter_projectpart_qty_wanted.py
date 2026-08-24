from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_normalise_tags'),
    ]

    operations = [
        migrations.AlterField(
            model_name='part',
            name='tags',
            field=models.CharField(blank=True, help_text='Comma separated. Normalised on save so filtering is exact.', max_length=200),
        ),
        migrations.AlterField(
            model_name='projectpart',
            name='qty_wanted',
            field=models.PositiveIntegerField(default=0, help_text='How many this build needs.'),
        ),
    ]
