from django import forms
from .models import IncidentReport, IncidentUpdate

class IncidentReportForm(forms.ModelForm):
    class Meta:
        model = IncidentReport
        fields = [
            "title", "description", "category", "location",
            "occurred_at", "severity", "attachment"
        ]
        widgets = {
            "occurred_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "description": forms.Textarea(attrs={"rows": 5}),
        }

class IncidentUpdateForm(forms.ModelForm):
    class Meta:
        model = IncidentUpdate
        fields = ["note", "is_internal"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}
