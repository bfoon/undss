from django import forms
from django.contrib.auth import get_user_model
from .models import EmployeeIDCardRequest

User = get_user_model()


# -------------------------------------------------------------
# 1. Staff Self-Service Form  (Renewal / Replacement Only)
# -------------------------------------------------------------
class EmployeeIDCardRequestForm(forms.ModelForm):
    """
    Used by normal staff (requesting for themselves).
    for_user + requested_by are assigned in the view.
    """

    class Meta:
        model = EmployeeIDCardRequest
        fields = ["request_type", "reason", "request_form"]

        widgets = {
            "request_type": forms.Select(
                attrs={"class": "form-select"}
            ),
            "reason": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": (
                        "Explain briefly why you need this ID card "
                        "(lost, damaged, renewal, name change, etc.)"
                    ),
                }
            ),
            "request_form": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": ".pdf,.doc,.docx",
                }
            ),
        }

        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
            "request_form": "Signed UN ID Card Request Form (PDF / Word)",
        }

        help_texts = {
            "request_form": (
                "Upload the completed and signed UN Identification Card Request Form "
                "(Section A filled by staff, Section B signed by Agency Focal Point)."
            ),
        }


# -------------------------------------------------------------
# 2. Admin (LSA / SOC / Agency HR) Form
# -------------------------------------------------------------
class EmployeeIDCardAdminRequestForm(forms.ModelForm):
    """
    Used by LSA/SOC/Agency HR to create a request for any employee.
    Includes:
    - Select2 search dropdown
    - Agency-based filtering (done in the view)
    """

    # 🔹 The Select2 dropdown field
    for_user = forms.ModelChoiceField(
        queryset=User.objects.filter(is_active=True)
            .order_by("agency__code", "last_name", "first_name"),
        widget=forms.Select(
            attrs={
                "class": "form-select select2-employee",   # 👈 enables Select2
                "data-placeholder": "Search employee...",
            }
        ),
        label="Employee",
        help_text="Search for the staff member needing an ID card.",
    )

    class Meta:
        model = EmployeeIDCardRequest
        fields = ["for_user", "request_type", "reason", "request_form"]

        widgets = {
            "request_type": forms.Select(
                attrs={"class": "form-select"}
            ),
            "reason": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": (
                        "Reason for this ID card request "
                        "(lost, damaged, renewal, etc.)"
                    ),
                }
            ),
            "request_form": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": ".pdf,.doc,.docx",
                }
            ),
        }

        labels = {
            "request_type": "Request Type",
            "reason": "Reason / Comments",
            "request_form": "Signed UN ID Card Request Form (PDF / Word)",
        }

        help_texts = {
            "request_form": (
                "Attach the staff member’s signed UN Identification Card Request Form "
                "(Section A/B signed)."
            ),
        }

    # -------------------------------------------------------------
    # Customize label text inside the dropdown
    # -------------------------------------------------------------
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def _label(user):
            """
            Example:
            UNDP – Baboucarr Foon
            UNICEF – John Mendy
            """
            full_name = user.get_full_name().strip() or user.username
            agency_code = getattr(getattr(user, "agency", None), "code", "")

            if agency_code:
                return f"{agency_code} – {full_name}"
            return full_name

        self.fields["for_user"].label_from_instance = _label
