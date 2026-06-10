from django.db import migrations, models


def staff_to_panel_manager(apps, schema_editor):
    User = apps.get_model("core", "User")
    for u in User.objects.filter(is_staff=True, is_superuser=False):
        u.panel_manager = True
        u.is_staff = False
        u.save(update_fields=["panel_manager", "is_staff"])


def reverse_panel_to_staff(apps, schema_editor):
    User = apps.get_model("core", "User")
    for u in User.objects.filter(panel_manager=True, is_superuser=False):
        u.is_staff = True
        u.panel_manager = False
        u.save(update_fields=["panel_manager", "is_staff"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_alter_exam_session_type_three_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="panel_manager",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(staff_to_panel_manager, reverse_panel_to_staff),
    ]
