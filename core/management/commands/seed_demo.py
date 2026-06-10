from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.models import (
    AcademicYear,
    Department,
    Material,
    MaterialSection,
    MaterialSectionCorrector,
    Section,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Create demo departments, sections, materials, year, users, and corrector assignments."

    def handle(self, *args, **options):
        year, _ = AcademicYear.objects.get_or_create(year_label="2025-2026")

        cs, _ = Department.objects.get_or_create(name="Computer Science")
        law, _ = Department.objects.get_or_create(name="Law")

        for dept, names in [
            (cs, ["French", "English"]),
            (law, ["French", "English"]),
        ]:
            for n in names:
                Section.objects.get_or_create(department=dept, name=n)

        mat_cs, _ = Material.objects.get_or_create(
            department=cs,
            material_name="Algorithms",
            defaults={"level": "2"},
        )
        mat_cs.level = "2"
        mat_cs.save(update_fields=["level"])
        mat_law, _ = Material.objects.get_or_create(
            department=law,
            material_name="Civil Law",
            defaults={"level": "1"},
        )

        sec_cs_fr = Section.objects.get(department=cs, name="French")
        sec_cs_en = Section.objects.get(department=cs, name="English")

        ms1, _ = MaterialSection.objects.get_or_create(
            material=mat_cs, section=sec_cs_fr
        )
        ms2, _ = MaterialSection.objects.get_or_create(
            material=mat_cs, section=sec_cs_en
        )

        doc1, _ = User.objects.get_or_create(
            username="doctor1",
            defaults={"email": "doctor1@example.com"},
        )
        if not doc1.has_usable_password():
            doc1.set_password("demo1234")
            doc1.save()

        doc2, _ = User.objects.get_or_create(
            username="doctor2",
            defaults={"email": "doctor2@example.com"},
        )
        if not doc2.has_usable_password():
            doc2.set_password("demo1234")
            doc2.save()

        MaterialSectionCorrector.objects.get_or_create(
            material_section=ms1,
            academic_year=year,
            role="first",
            defaults={"corrector": doc1},
        )
        MaterialSectionCorrector.objects.get_or_create(
            material_section=ms1,
            academic_year=year,
            role="second",
            defaults={"corrector": doc2},
        )
        MaterialSectionCorrector.objects.get_or_create(
            material_section=ms2,
            academic_year=year,
            role="first",
            defaults={"corrector": doc1},
        )
        MaterialSectionCorrector.objects.get_or_create(
            material_section=ms2,
            academic_year=year,
            role="second",
            defaults={"corrector": doc2},
        )

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write("Users: doctor1 / demo1234 (first), doctor2 / demo1234 (second)")
