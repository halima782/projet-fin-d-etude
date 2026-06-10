from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_exam_session_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="material",
            name="semester",
            field=models.CharField(
                choices=[("1", "Semester 1"), ("2", "Semester 2")],
                default="1",
                max_length=1,
            ),
        ),
    ]
