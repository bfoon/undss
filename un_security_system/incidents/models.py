from django.conf import settings
from django.db import models

class IncidentReport(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        IN_REVIEW = "in_review", "In Review"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    title = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)

    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.LOW
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.NEW
    )

    attachment = models.FileField(upload_to="incidents/", blank=True, null=True)

    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="incidents_reported"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="incidents_assigned"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"#{self.pk} {self.title}"

class IncidentUpdate(models.Model):
    incident = models.ForeignKey(IncidentReport, on_delete=models.CASCADE, related_name="updates")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_internal = models.BooleanField(
        default=False,
        help_text="If checked, only LSA/SOC see this note."
    )

    def __str__(self):
        return f"Update {self.pk} on Incident {self.incident_id}"
