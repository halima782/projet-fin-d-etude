from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.mail import send_mail
from django.conf import settings

# 1️⃣ Custom User (Doctors / Admin)
class User(AbstractUser):
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=30, blank=True, default="")
    panel_manager = models.BooleanField(default=False)

    def __str__(self):
        return self.username


# 2️⃣ Department (e.g., Computer Science, Law)
class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


# 3️⃣ Academic Year (e.g., 2024-2025)
class AcademicYear(models.Model):
    year_label = models.CharField(max_length=9, unique=True)

    def __str__(self):
        return self.year_label


# 4️⃣ Material (linked to Department and Year Level)
class Material(models.Model):
    LEVEL_CHOICES = [
        ('1', 'First Year'),
        ('2', 'Second Year'),
        ('3', 'Third Year'),
        ('4', 'Fourth Year'),
    ]

    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="materials")
    material_name = models.CharField(max_length=150)
    level = models.CharField(max_length=1, choices=LEVEL_CHOICES, default="1")
    semester = models.CharField(
        max_length=1,
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
    )

    def __str__(self):
        return f"{self.material_name} - Year {self.level} ({self.department.name})"


# 5️⃣ Section (French / English per Department)
class Section(models.Model):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.name} - {self.department.name}"


# 6️⃣ Material + Section Junction
class MaterialSection(models.Model):
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('material', 'section')

    def __str__(self):
        return f"{self.material.material_name} ({self.section.name})"


# 7️⃣ Assign Correctors Per Year
class MaterialSectionCorrector(models.Model):
    ROLE_CHOICES = (
        ('first', 'First Corrector'),
        ('second', 'Second Corrector'),
    )

    material_section = models.ForeignKey(MaterialSection, on_delete=models.CASCADE)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)
    corrector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)

    class Meta:
        unique_together = ("material_section", "academic_year", "role")

    def __str__(self):
        label = self.corrector.username if self.corrector else "—"
        return f"{label} - {self.material_section} ({self.role})"


# 8️⃣ Exams
class Exam(models.Model):
    SESSION_PARTIAL = "partial"
    SESSION_FIRST_FINAL = "first_final"
    SESSION_SECOND_FINAL = "second_final"
    SESSION_TYPE_CHOICES = [
        (SESSION_PARTIAL, "Partial exam"),
        (SESSION_FIRST_FINAL, "First final exam"),
        (SESSION_SECOND_FINAL, "Second final exam"),
    ]

    material_section = models.ForeignKey(MaterialSection, on_delete=models.CASCADE)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)
    exam_number = models.IntegerField()
    session_type = models.CharField(
        max_length=15,
        choices=SESSION_TYPE_CHOICES,
        default=SESSION_FIRST_FINAL,
    )

    class Meta:
        unique_together = ("material_section", "academic_year", "exam_number", "session_type")

    def __str__(self):
        return f"{self.material_section} - Exam {self.exam_number} ({self.academic_year})"


# 9️⃣ Questions (Template per Material)
class Question(models.Model):
    """Rubric parts: one set per material and exam session type (partial / finals)."""

    SESSION_PARTIAL = Exam.SESSION_PARTIAL
    SESSION_FIRST_FINAL = Exam.SESSION_FIRST_FINAL
    SESSION_SECOND_FINAL = Exam.SESSION_SECOND_FINAL
    SESSION_TYPE_CHOICES = Exam.SESSION_TYPE_CHOICES

    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    session_type = models.CharField(
        max_length=15,
        choices=SESSION_TYPE_CHOICES,
        default=SESSION_FIRST_FINAL,
    )
    question_title = models.CharField(max_length=100)
    part_title = models.CharField(max_length=100)
    part_mark = models.FloatField()

    class Meta:
        ordering = ["session_type", "id"]

    def __str__(self):
        return f"{self.material.material_name} - {self.question_title} ({self.part_title})"


# 🔟 Marks (Per Exam, Per Corrector)
class Mark(models.Model):
    CORRECTOR_ROLE_FIRST = "first"
    CORRECTOR_ROLE_SECOND = "second"
    CORRECTOR_ROLE_CHOICES = (
        (CORRECTOR_ROLE_FIRST, "First Corrector"),
        (CORRECTOR_ROLE_SECOND, "Second Corrector"),
    )

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE)
    question = models.ForeignKey(Question, on_delete=models.CASCADE)
    corrector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    corrector_role = models.CharField(
        max_length=10,
        choices=CORRECTOR_ROLE_CHOICES,
        default=CORRECTOR_ROLE_FIRST,
    )
    mark = models.FloatField()

    class Meta:
        unique_together = ("exam", "question", "corrector_role")

    def __str__(self):
        who = self.corrector.username if self.corrector else "—"
        return f"{self.exam} - {who} ({self.corrector_role}): {self.mark}"


# 1️⃣1️⃣ Final Results
class FinalResult(models.Model):
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE)
    first_total = models.FloatField(default=0)
    second_total = models.FloatField(default=0)
    average = models.FloatField(default=0)
    difference = models.FloatField(default=0)
    second_round_complete = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.average = (self.first_total + self.second_total) / 2
        self.difference = abs(self.first_total - self.second_total)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Final Result - {self.exam}"


# 1️⃣2️⃣ Notifications
class Notification(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.user_id:
            return f"Notification for {self.user.username}"
        return "Notification (no user)"

    def send_email(self):
        if self.user and self.user.email:
            send_mail(
                subject='University Grading Notification',
                message=self.message,
                from_email=settings.EMAIL_HOST_USER,
                recipient_list=[self.user.email],
                fail_silently=False,
            )


class MarksReport(models.Model):
    REPORT_CORRECTOR = "corrector"
    REPORT_FINAL_AVERAGE = "final_average"
    REPORT_KIND_CHOICES = [
        (REPORT_CORRECTOR, "Corrector marks sheet"),
        (REPORT_FINAL_AVERAGE, "Final average sheet"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    material_section = models.ForeignKey(MaterialSection, on_delete=models.CASCADE)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)
    role = models.CharField(max_length=10, choices=MaterialSectionCorrector.ROLE_CHOICES)
    session_type = models.CharField(max_length=15, choices=Exam.SESSION_TYPE_CHOICES)
    file_path = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    report_kind = models.CharField(
        max_length=20,
        choices=REPORT_KIND_CHOICES,
        default=REPORT_CORRECTOR,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"MarksReport {self.report_kind} — {self.material_section_id}"
