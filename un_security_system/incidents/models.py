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


class CommonServiceConfig(models.Model):
    agency = models.OneToOneField("accounts.Agency", on_delete=models.CASCADE, related_name="common_service_config")

    approval_levels = models.PositiveIntegerField(default=1)  # 1..N
    level_1_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_level1_manager_for_agencies",
        help_text="Default Common Service Manager (Level 1 approver)."
    )

    # Optional: allow default escalation targets
    operations_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_ops_manager_for_agencies",
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.agency} common service config"


class CommonServiceApprover(models.Model):
    agency = models.ForeignKey("accounts.Agency", on_delete=models.CASCADE, related_name="cs_approvers")
    level = models.PositiveIntegerField(help_text="Approval level: 1..N")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cs_approver_roles"
    )

    is_primary = models.BooleanField(default=False)  # optional “main” for that level
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["agency", "level", "user"], name="unique_cs_approver_per_level"),
        ]
        ordering = ["agency", "level", "-is_primary"]

    def __str__(self):
        return f"{self.agency.code} L{self.level} approver: {self.user}"


class CommonServiceRequest(models.Model):
    class Category(models.TextChoices):
        COMMON_PREMISES = "common_premises", "Common Premises / General"
        CASH_POWER = "cash_power", "Cash Power Refill"
        FACILITY_NOTICE = "facility_notice", "Facility Work Notice (Noise/Disruption)"
        ELECTRICAL = "electrical", "Electrical (Bulbs, Switches, Outlets, Failover)"
        PLUMBING = "plumbing", "Plumbing / Toilets"
        CLEANING = "cleaning", "Cleaning Services"
        WASTE = "waste", "Dumpster / Waste Disposal"
        GROUNDS = "grounds", "Grounds (Trees Trim/Cut)"
        SOLAR = "solar", "Solar Issue"
        CCTV = "cctv", "CCTV Issue"
        OTHER = "other", "Other"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        NEW = "new", "New"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    # ✅ Optional link to SECURITY incident only (IncidentReport is security) :contentReference[oaicite:1]{index=1}
    incident = models.ForeignKey(
        "IncidentReport",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="common_service_requests",
    )

    title = models.CharField(max_length=200)
    category = models.CharField(max_length=40, choices=Category.choices, default=Category.COMMON_PREMISES)
    description = models.TextField()

    location = models.CharField(max_length=200, blank=True, default="")
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)

    attachment = models.FileField(upload_to="common_services/", blank=True, null=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cs_requests_made"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="cs_requests_assigned"
    )

    # For “inform users of ongoing facility work” notices
    disruption_start = models.DateTimeField(null=True, blank=True)
    disruption_end = models.DateTimeField(null=True, blank=True)
    is_notice = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    agency = models.ForeignKey("accounts.Agency",
                               on_delete=models.CASCADE,
                               related_name="common_service_requests")

    current_level = models.PositiveIntegerField(default=1)
    requires_approval = models.BooleanField(default=True)

    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_requests_approved"
    )

    # Escalation routing
    escalated_to = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="ops_manager / lsa / soc / ict / etc."
    )
    escalated_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_requests_escalated_to"
    )
    escalated_at = models.DateTimeField(null=True, blank=True)

    def get_config(self):
        return getattr(self.agency, "common_service_config", None)

    def total_levels(self):
        cfg = self.get_config()
        return int(cfg.approval_levels) if cfg else 1

    def is_final_level(self):
        return self.current_level >= self.total_levels()

    def clean(self):
        # If it's a notice, enforce schedule
        if self.is_notice and (not self.disruption_start or not self.disruption_end):
            raise ValidationError("For notices, please provide disruption start and end time.")

    def __str__(self):
        return f"CSR#{self.pk} {self.title}"

