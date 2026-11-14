from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramuser",
            name="timezone",
            field=models.CharField(
                default="Europe/Kyiv",
                help_text="IANA timezone, наприклад Europe/Kyiv.",
                max_length=64,
                verbose_name="Часовий пояс",
            ),
        ),
    ]
