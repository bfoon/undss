from django import forms
from django.contrib.auth import get_user_model

from .models import EmployeeIDCardRequest

User = get_user_model()


class EmployeeIDCardRequestForm(forms.ModelForm):
    """
    Used by normal staff (requesting for themselves).
    We set for_user and requested_by in the view, so they are not on the form.
    """

    class Meta:
        model = EmployeeIDCardRequest
        # 🔸 include request_form so staff can upload the signed form
        fields = ["request_type", "reason", "request_form"]
        widgets = {
            "request_type": forms.Select(attrs={
                "class": "form-select",
            }),
            "reason": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Explain briefly why you need this ID card (lost, damaged, renewal, name change, etc.)",
            }),
            "request_form": forms.ClearableFileInput(attrs={
                "class": "form-control",
                "accept": ".pdf,.doc,.docx",
            }),
        }
        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
            "request_form": "Signed Request Form (PDF / Word)",
        }
        help_texts = {
            "request_form": "Upload the completed HR request form if applicable (PDF, DOC, DOCX).",
        }


class EmployeeIDCardAdminRequestForm(forms.ModelForm):
    """
    Used by LSA / SOC / Agency HR to create a request for any employee.
    Includes for_user + same fields as above + file upload.
    """

    for_user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("agency__code", "last_name", "first_name"),
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Employee",
        help_text="Select the staff member this ID card is for.",
    )

    class Meta:
        model = EmployeeIDCardRequest
        # 🔸 include request_form here as well
        fields = ["for_user", "request_type", "reason", "request_form"]
        widgets = {
            "request_type": forms.Select(attrs={
                "class": "form-select",
            }),
            "reason": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Reason for this ID card request (lost, damaged, renewal, etc.)",
            }),
            "request_form": forms.ClearableFileInput(attrs={
                "class": "form-control",
                "accept": ".pdf,.doc,.docx",
            }),
        }
        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
            "request_form": "Signed Request Form (PDF / Word)",
        }
        help_texts = {
            "request_form": "Attach the staff’s signed request form if available.",
        }