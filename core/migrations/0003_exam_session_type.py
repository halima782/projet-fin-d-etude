from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_alter_mark_unique_together_mark_corrector_role_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="exam",
            name="session_type",
            field=models.CharField(
                choices=[("partial", "Partial exam"), ("final", "Final exam")],
                default="final",
                max_length=10,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="exam",
            unique_together={
                ("material_section", "academic_year", "exam_number", "session_type")
            },
        ),
    ]
