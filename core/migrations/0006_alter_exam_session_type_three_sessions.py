from django.db import migrations, models


def forwards_final_to_first_final(apps, schema_editor):
    Exam = apps.get_model("core", "Exam")
    Exam.objects.filter(session_type="final").update(session_type="first_final")


def backwards_first_final_to_final(apps, schema_editor):
    Exam = apps.get_model("core", "Exam")
    Exam.objects.filter(session_type="first_final").update(session_type="final")
    Exam.objects.filter(session_type="second_final").update(session_type="final")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_alter_material_semester"),
    ]

    operations = [
        migrations.AlterField(
            model_name="exam",
            name="session_type",
            field=models.CharField(
                max_length=15,
                choices=[
                    ("partial", "Partial exam"),
                    ("final", "Final exam"),
                ],
                default="final",
            ),
        ),
        migrations.RunPython(forwards_final_to_first_final, backwards_first_final_to_final),
        migrations.AlterField(
            model_name="exam",
            name="session_type",
            field=models.CharField(
                max_length=15,
                choices=[
                    ("partial", "Partial exam"),
                    ("first_final", "First final exam"),
                    ("second_final", "Second final exam"),
                ],
                default="first_final",
            ),
        ),
    ]
