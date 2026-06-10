from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_material_semester"),
    ]

    operations = [
        migrations.AlterField(
            model_name="material",
            name="semester",
            field=models.CharField(
                choices=[
                    ("1", "Semester 1"),
                    ("2", "Semester 2"),
                    ("3", "Semester 3"),
                    ("4", "Semester 4"),
                    ("5", "Semester 5"),
                    ("6", "Semester 6"),
                    ("7", "Semester 7"),
                    ("8", "Semester 8"),
                ],
                default="1",
                max_length=1,
            ),
        ),
    ]
