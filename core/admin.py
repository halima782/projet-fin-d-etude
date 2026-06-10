from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .forms import CustomAdminUserCreationForm
from .models import (
    AcademicYear,
    Department,
    Exam,
    FinalResult,
    Mark,
    MarksReport,
    Material,
    MaterialSection,
    MaterialSectionCorrector,
    Notification,
    Question,
    Section,
    User,
)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = CustomAdminUserCreationForm
    # Two blocks so "WhatsApp / phone" is impossible to miss on the add screen.
    add_fieldsets = (
        (
            _("Login & contact"),
            {
                "classes": ("wide",),
                "fields": ("username", "email", "phone_number"),
            },
        ),
        (
            _("Password"),
            {
                "classes": ("wide",),
                "fields": ("usable_password", "password1", "password2"),
            },
        ),
    )
    fieldsets = tuple(BaseUserAdmin.fieldsets) + (
        (
            "WhatsApp & administration panel",
            {
                "fields": ("phone_number", "panel_manager"),
                "description": "Phone with country code (e.g. +961…) for notifications and wa.me links.",
            },
        ),
    )
    ordering = ("username",)
    list_display = (
        "username",
        "email",
        "phone_number",
        "panel_manager",
        "is_staff",
        "is_superuser",
        "is_active",
    )


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("exam_number", "session_type", "material_section", "academic_year")
    list_filter = ("session_type", "academic_year")


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("material", "session_type", "question_title", "part_title", "part_mark")
    list_filter = ("session_type", "material__department")


admin.site.register(Department)
admin.site.register(AcademicYear)
admin.site.register(Material)
admin.site.register(Section)
admin.site.register(MaterialSection)
admin.site.register(MaterialSectionCorrector)
admin.site.register(Mark)
admin.site.register(FinalResult)
admin.site.register(Notification)


@admin.register(MarksReport)
class MarksReportAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "material_section",
        "academic_year",
        "session_type",
        "role",
        "report_kind",
        "created_at",
    )
    list_filter = ("report_kind", "session_type", "role")
