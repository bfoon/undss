# accounts/forms.py  (or hr/forms.py depending on where you put the views)
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
        fields = ["request_type", "reason"]
        widgets = {
            "request_type": forms.Select(attrs={
                "class": "form-select",
            }),
            "reason": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Explain briefly why you need this ID card (lost, damaged, renewal, name change, etc.)",
            }),
        }
        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
        }


class EmployeeIDCardAdminRequestForm(forms.ModelForm):
    """
    Used by LSA / SOC / Agency HR to create a request for any employee.
    Includes for_user + same fields as above.
    """
    for_user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True).order_by("agency__code", "last_name", "first_name"),
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Employee",
        help_text="Select the staff member this ID card is for.",
    )

    class Meta:
        model = EmployeeIDCardRequest
        fields = ["for_user", "request_type", "reason"]
        widgets = {
            "request_type": forms.Select(attrs={
                "class": "form-select",
            }),
            "reason": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Reason for this ID card request (lost, damaged, renewal, etc.)",
            }),
        }
        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
        }
