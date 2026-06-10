from django import forms
from django.contrib.auth.forms import AdminUserCreationForm, UsernameField

from .models import Department, Section, AcademicYear, Material, User


class CustomAdminUserCreationForm(AdminUserCreationForm):
    class Meta:
        model = User
        fields = ("username", "email", "phone_number")
        field_classes = {"username": UsernameField}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        f = self.fields.get("phone_number")
        if f:
            f.label = "WhatsApp / phone number"
            f.help_text = "With country code (digits only), e.g. 96171234567 — used for wa.me and in-app notifications."
            f.required = False


class FilterMaterialForm(forms.Form):
    department = forms.ModelChoiceField(
        queryset=Department.objects.all(), 
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    section = forms.ModelChoiceField(
        queryset=Section.objects.all(), 
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.all(), 
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    level = forms.ChoiceField(
        choices=Material.LEVEL_CHOICES, 
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'})
    )