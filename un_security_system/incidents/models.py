from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


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
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    attachment = models.FileField(upload_to="incidents/", blank=True, null=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="incidents_reported"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incidents_assigned"
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
    agency = models.OneToOneField(
        "accounts.Agency", on_delete=models.CASCADE, related_name="common_service_config"
    )
    approval_levels = models.PositiveIntegerField(default=1)
    level_1_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_level1_manager_for_agencies",
        help_text="Default Common Service Manager (Level 1 approver)."
    )
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
    config = models.ForeignKey(
        "CommonServiceConfig", on_delete=models.CASCADE, related_name="approvers"
    )
    agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE, related_name="cs_approvers"
    )
    level = models.PositiveIntegerField(help_text="Approval level: 1..N")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cs_approver_roles"
    )
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "level", "user"],
                name="unique_cs_approver_per_level"
            ),
        ]
        ordering = ["agency", "level", "-is_primary"]

    def __str__(self):
        return f"{self.agency.code} L{self.level} approver: {self.user}"


class CommonServiceRequestType(models.Model):
    """Dynamic catalogue of selectable Common Service Request types."""

    code = models.SlugField(
        max_length=40,
        unique=True,
        help_text="Stable internal code. Generated automatically when first created."
    )
    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Inactive types remain visible on historical requests but disappear "
            "from new request forms."
        ),
    )
    requires_disruption_window = models.BooleanField(
        default=False,
        help_text="Show disruption start/end fields and treat this request type as a notice.",
    )
    sort_order = models.PositiveIntegerField(default=100, help_text="Lower numbers appear first.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="csr_request_types_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "CSR request type"
        verbose_name_plural = "CSR request types"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Code is immutable after first save because historical CSR rows store it.
        if not self.pk:
            supplied = (self.code or "").strip()
            base = supplied or slugify(self.name).replace("-", "_") or "request_type"
            base = base.replace("-", "_")[:34]
            candidate = base
            suffix = 2
            while type(self).objects.filter(code=candidate).exists():
                tail = f"_{suffix}"
                candidate = f"{base[:40-len(tail)]}{tail}"
                suffix += 1
            self.code = candidate
        else:
            old_code = (
                type(self).objects.filter(pk=self.pk).values_list("code", flat=True).first()
            )
            if old_code:
                self.code = old_code
        super().save(*args, **kwargs)


class CommonServiceRequest(models.Model):
    # Kept as legacy constants so old code/data remain compatible.
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

    incident = models.ForeignKey(
        "IncidentReport",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="common_service_requests",
    )
    title = models.CharField(max_length=200)

    # Dynamic catalogue: no hard-coded choices on the field.
    category = models.CharField(
        max_length=40,
        default=Category.COMMON_PREMISES,
    )

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
    disruption_start = models.DateTimeField(null=True, blank=True)
    disruption_end = models.DateTimeField(null=True, blank=True)
    is_notice = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    agency = models.ForeignKey(
        "accounts.Agency", on_delete=models.CASCADE, related_name="common_service_requests"
    )
    current_level = models.PositiveIntegerField(default=1)
    requires_approval = models.BooleanField(default=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cs_requests_approved"
    )
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

    def get_category_display(self):
        try:
            request_type = (
                CommonServiceRequestType.objects
                .filter(code=self.category)
                .only("name")
                .first()
            )
        except Exception:
            request_type = None

        if request_type:
            return request_type.name

        legacy = dict(self.Category.choices)
        return legacy.get(
            self.category,
            (self.category or "Unknown").replace("_", " ").title(),
        )

    def clean(self):
        errors = {}

        request_type = (
            CommonServiceRequestType.objects
            .filter(code=self.category)
            .first()
        )
        if request_type is None:
            errors["category"] = "Select a valid Common Service Request type."
        elif not request_type.is_active:
            errors["category"] = "That Common Service Request type is currently disabled."
        else:
            # Notice behavior comes from the catalogue, not a hard-coded code.
            self.is_notice = bool(request_type.requires_disruption_window)

        if self.is_notice and (not self.disruption_start or not self.disruption_end):
            errors["disruption_start"] = (
                "For notice request types, provide disruption start and end time."
            )
        if self.disruption_start and self.disruption_end:
            if self.disruption_end <= self.disruption_start:
                errors["disruption_end"] = "Disruption end must be after the start."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"CSR#{self.pk} {self.title}"
