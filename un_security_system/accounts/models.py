from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from datetime import timedelta, datetime
from django.db.models import F, Q
import uuid

class Agency(models.Model):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=20, unique=True, help_text="Short code e.g. UNDP, UNICEF")
    def __str__(self):
        return self.code or self.name

class User(AbstractUser):
    ROLE_CHOICES = [
        ('requester', 'Requester (Staff)'),
        ('data_entry', 'Data Entry (Security Guard)'),
        ('lsa', 'Local Security Associate'),
        ('soc', 'Security Operations Center'),
        ('reception', 'Receptionist'),
        ('registry', 'Registry'),
        ('ict_focal', 'ICT Focal Point'),

        # 🔹 NEW ROLE
        ('agency_hr', 'Agency HR'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='requester')
    phone = models.CharField(max_length=20, blank=True)

    employee_id = models.CharField(
        max_length=20,
        unique=True,
        blank=True,
        null=True,
        help_text="Staff ID number / badge ID"
    )

    # 🔹 New field to track expiry of ID card (or badge)
    employee_id_expiry = models.DateField(
        blank=True,
        null=True,
        help_text="Date the physical ID card expires"
    )

    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        help_text="UN Agency the user belongs to"
    )
    unit = models.ForeignKey(
        "Unit",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="users",
    )
    # Password policy
    must_change_password = models.BooleanField(default=False)
    temp_password_set_at = models.DateTimeField(null=True, blank=True)

    # NEW: OTP fields
    otp_code = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        help_text="Last login OTP sent to the user",
    )
    otp_expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Expiry time for the last OTP",
    )

    def mark_temp_password(self):
        self.must_change_password = True
        self.temp_password_set_at = timezone.now()
        self.save(update_fields=['must_change_password', 'temp_password_set_at'])

    def otp_is_valid(self, code: str) -> bool:
        if not self.otp_code or not self.otp_expires_at:
            return False
        if self.otp_code != code:
            return False
        return timezone.now() <= self.otp_expires_at

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class OneTimeCode(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    device_id = models.CharField(max_length=64)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)


    class Meta:
        indexes = [
            models.Index(fields=["user", "device_id", "code", "is_used"]),
        ]

    def is_valid(self):
        return (
            not self.is_used and
            self.expires_at > timezone.now()
        )

    def __str__(self):
        return f"OTP for {self.user} ({self.code})"


class TrustedDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    device_id = models.CharField(max_length=64, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'device_id')

    def is_valid(self):
        return self.is_active and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.user} – {self.device_id}"

class SecurityIncident(models.Model):
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    reported_by = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField()
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    location = models.CharField(max_length=100)
    reported_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='resolved_incidents')
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-reported_at']

    def __str__(self):
        return f"{self.title} - {self.severity}"

def generate_invite_code():
    """Serializable default for invite codes."""
    return uuid.uuid4().hex


class RegistrationInvite(models.Model):
    """
    Registration link generated by ICT focal point.
    Can be used N times within a limited time window (< 24h).
    """
    code = models.CharField(
        max_length=64,
        unique=True,
        default=generate_invite_code,   # ✅ no lambda, Django can serialize this
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="created_invites",
    )
    # Default 100 registrations
    max_uses = models.PositiveIntegerField(default=100)
    used_count = models.PositiveIntegerField(default=0)

    # ICT focal point chooses 1–23 hours in the form
    valid_for_hours = models.PositiveIntegerField(default=12)

    is_active = models.BooleanField(default=True)

    # Calculated from created_at + valid_for_hours
    expires_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        """
        On first save, compute expires_at based on valid_for_hours.
        Enforce max 23 hours (your requirement: less than 24h).
        """
        if not self.pk or not self.expires_at:
            hours = self.valid_for_hours or 12
            # your rule: less than 24 hours
            if hours >= 24:
                hours = 23
            self.valid_for_hours = hours
            self.expires_at = timezone.now() + timedelta(hours=hours)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def remaining_uses(self):
        return max(self.max_uses - self.used_count, 0)

    @property
    def can_be_used(self):
        return (not self.is_expired) and  self.is_active and self.remaining_uses > 0

    def mark_used(self):
        """
        Safely increment usage (for concurrency).
        Call this after a successful registration.
        """
        type(self).objects.filter(pk=self.pk).update(used_count=F("used_count") + 1)
        self.refresh_from_db()

class RegistrationInviteUsage(models.Model):
    invite = models.ForeignKey(
        RegistrationInvite,
        related_name="registrations",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="registration_invite_usages",
        on_delete=models.CASCADE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

class RoomAmenity(models.Model):
    """
    Reusable amenity / feature that can be attached to one or more rooms.
    Example: projector, video conference, whiteboard, etc.
    """
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Machine-readable code, e.g. 'projector', 'video_conf'"
    )
    name = models.CharField(
        max_length=120,
        help_text="Human name, e.g. 'Projector', 'Video Conferencing'"
    )
    icon_class = models.CharField(
        max_length=80,
        blank=True,
        help_text="Bootstrap icon class, e.g. 'bi-projector', 'bi-camera-video'"
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional short description shown as tooltip"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Hide amenity from selection without deleting it"
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Room amenity"
        verbose_name_plural = "Room amenities"

    def __str__(self) -> str:
        return self.name


class Room(models.Model):
    """
    A bookable room (library, conference room, meeting room, etc.)
    """
    ROOM_TYPES = (
        ("meeting", "Meeting Room"),
        ("conference", "Conference Room"),
        ("library", "Library"),
        ("other", "Other"),
    )

    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Short code, e.g. CR-1, LIB-1"
    )
    room_type = models.CharField(
        max_length=20,
        choices=ROOM_TYPES,
        default="meeting",
    )
    location = models.CharField(
        max_length=255,
        blank=True,
        help_text="e.g. UN House 1st floor"
    )
    capacity = models.PositiveIntegerField(null=True, blank=True)
    description = models.TextField(blank=True)

    is_active = models.BooleanField(default=True)

    # 🔹 NEW: amenities / features
    amenities = models.ManyToManyField(
        "RoomAmenity",
        related_name="rooms",
        blank=True,
        help_text="Available features/amenities in this room"
    )

    # Users that can approve bookings for this room
    approvers = models.ManyToManyField(
        User,
        related_name="rooms_to_approve",
        blank=True,
        help_text="Users who can approve bookings for this room."
    )

    def __str__(self):
        return f"{self.name} ({self.code})"

    # --------- AMENITY HELPERS ---------

    def has_amenity(self, code: str) -> bool:
        """
        Convenience helper for templates / logic:
        room.has_amenity('projector'), room.has_amenity('video_conf'), etc.
        """
        return self.amenities.filter(code=code, is_active=True).exists()

    @property
    def amenities_for_display(self):
        """
        Amenity queryset already filtered for active ones.
        Use this in templates: for amenity in room.amenities_for_display
        """
        return self.amenities.filter(is_active=True)

    # --------- AVAILABILITY HELPERS ---------

    @property
    def next_meeting(self):
        """
        Returns the next approved meeting today, or None.
        """
        now = timezone.localtime()
        today = now.date()
        now_time = now.time()

        return self.bookings.filter(
            date=today,
            status="approved",
            start_time__gt=now_time
        ).order_by("start_time").first()

    @property
    def next_meeting_human(self):
        """
        Human-friendly text for the next meeting start time:
        - "in 15 min"
        - "in 1h 20m"
        """
        nm = self.next_meeting
        if not nm:
            return None

        now = timezone.localtime()
        start_dt = datetime.combine(nm.date, nm.start_time)
        start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())

        delta = start_dt - now
        seconds = delta.total_seconds()

        if seconds < 60:
            return "soon"

        minutes = int(seconds // 60)
        hours, minutes = divmod(minutes, 60)

        if hours and minutes:
            return f"in {hours}h {minutes}m"
        if hours:
            return f"in {hours}h"
        return f"in {minutes}m"

    @property
    def is_available_now(self):
        """
        Room is free right now if no APPROVED meeting is currently running.
        """
        now = timezone.localtime()
        today = now.date()
        now_time = now.time()

        return not self.bookings.filter(
            date=today,
            status="approved",
            start_time__lte=now_time,
            end_time__gt=now_time,
        ).exists()

    @property
    def time_until_free(self):
        """
        Returns human readable time until current APPROVED meeting ends.
        Example: "25 min", "1h 10m".
        """
        now = timezone.localtime()
        today = now.date()
        now_time = now.time()

        meeting = self.bookings.filter(
            date=today,
            status="approved",
            start_time__lte=now_time,
            end_time__gt=now_time,
        ).order_by("end_time").first()

        if not meeting:
            return ""

        end_dt = datetime.combine(meeting.date, meeting.end_time)
        end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())

        delta = end_dt - now
        seconds = delta.total_seconds()

        if seconds < 60:
            return "a few seconds"

        minutes = int(seconds // 60)
        hours, minutes = divmod(minutes, 60)

        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"

class RoomBooking(models.Model):
    """
    A booking request for a room (with approval workflow).
    """
    STATUS_CHOICES = (
        ("pending", "Pending approval"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    )

    room = models.ForeignKey(
        Room,
        related_name="bookings",
        on_delete=models.CASCADE,
    )
    title = models.CharField(max_length=200, help_text="Meeting title / purpose")
    description = models.TextField(blank=True)

    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    requested_by = models.ForeignKey(
        User,
        related_name="room_bookings",
        on_delete=models.CASCADE,
    )
    approved_by = models.ForeignKey(
        User,
        related_name="approved_room_bookings",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "start_time"]

    def __str__(self):
        return f"{self.room} – {self.title} on {self.date} ({self.start_time}-{self.end_time})"

    @property
    def is_future(self):
        """
        Simple helper to know if the booking is in the future.
        """
        from datetime import datetime
        dt = datetime.combine(self.date, self.start_time)
        return dt >= timezone.now()

    def clean(self):
        """
        Prevent overlapping bookings for APPROVED and PENDING bookings
        of the same room.
        """
        from django.core.exceptions import ValidationError
        if self.end_time <= self.start_time:
            raise ValidationError("End time must be after start time.")

        qs = RoomBooking.objects.filter(
            room=self.room,
            date=self.date,
            status__in=("approved", "pending"),
        ).exclude(pk=self.pk)

        # Time overlap:
        # (start < existing_end) and (end > existing_start)
        overlap = qs.filter(
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exists()

        if overlap:
            raise ValidationError(
                "This time range overlaps with an existing booking for this room."
            )

    def approve(self, user):
        self.status = "approved"
        self.approved_by = user
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_by", "approved_at"])

    def reject(self, user, reason=""):
        self.status = "rejected"
        self.approved_by = user
        self.rejection_reason = reason
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_by", "rejection_reason", "approved_at"])


class RoomApprover(models.Model):
    room = models.ForeignKey(
        "Room",
        on_delete=models.CASCADE,
        related_name="room_approver_links",  # changed from "approvers"
        help_text="Room that this user can approve bookings for.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="room_approver_roles",
        help_text="User who can approve bookings for this room.",
    )

    is_primary = models.BooleanField(
        default=False,
        help_text="Mark as primary approver / room owner."
    )

    can_approve_all_agency = models.BooleanField(
        default=True,
        help_text="If checked, can approve bookings regardless of requester agency."
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive approvers will be ignored by the approval workflow."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Room approver"
        verbose_name_plural = "Room approvers"
        constraints = [
            models.UniqueConstraint(
                fields=["room", "user"],
                name="unique_room_user_approver",
            )
        ]

    def __str__(self):
        return f"{self.user} → {self.room} ({'primary' if self.is_primary else 'approver'})"

# =========================
# ASSET MANAGEMENT MODELS
# =========================

class AgencyServiceConfig(models.Model):
    """
    Toggle services per agency + optional pricing.
    Superuser can enable/disable asset management per agency.
    """
    agency = models.OneToOneField(Agency, on_delete=models.CASCADE, related_name="service_config")

    asset_mgmt_enabled = models.BooleanField(default=False)

    # Optional billing (if you want to charge agencies)
    asset_mgmt_is_paid = models.BooleanField(default=False)
    asset_mgmt_cost_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    asset_mgmt_cost_currency = models.CharField(max_length=10, default="USD", blank=True)

    # Workflow toggles (agency-specific)
    require_manager_approval = models.BooleanField(default=True)
    require_ict_assignment = models.BooleanField(default=True)
    require_requester_verification = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.agency} service config"


class Unit(models.Model):
    """
    Units are agency-scoped. Unit head is the default asset manager.
    Some units can have extra managers (asset managers).
    """
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="units")
    name = models.CharField(max_length=120)

    unit_head = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="headed_units",
        help_text="Unit Head (default asset manager/approver for this unit)",
    )

    asset_managers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="managed_units",
        help_text="Additional asset managers for this unit (optional)",
    )

    is_core_unit = models.BooleanField(
        default=False,
        help_text="If true, assets/requests go to Operations Manager approval (agency-level).",
    )

    def __str__(self):
        return f"{self.agency.code} - {self.name}"


class AgencyAssetRoles(models.Model):
    """
    Agency-level roles for asset workflow.
    - operations_manager handles core/unallocated approvals
    - ict_custodian assigns assets (can be ICT focal or other ICT staff)
    """
    agency = models.OneToOneField(Agency, on_delete=models.CASCADE, related_name="asset_roles")

    operations_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ops_manager_for_agencies",
    )

    ict_custodian = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="ict_custodian_for_agencies",
        help_text="Users allowed to assign assets (ICT custodians).",
    )

    def __str__(self):
        return f"{self.agency} asset roles"


class AssetCategory(models.Model):
    """
    Laptop / Phone / Accessory / Router / Tablet etc.
    Agency-scoped so each agency can have its own categories.
    """
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="asset_categories")
    name = models.CharField(max_length=80)
    service_life_months = models.PositiveIntegerField(
        default=36,
        help_text="Expected lifespan in months. When reached, asset should be replaced."
    )
    eol_enabled = models.BooleanField(
        default=True,
        help_text="If enabled, assets under this category will be flagged as end-of-life when due."
    )

    def __str__(self):
        return f"{self.agency.code} - {self.name}"


# models.py (inside Asset)
class Asset(models.Model):
    STATUS_CHOICES = (
        ("available", "Available"),
        ("assigned", "Assigned"),
        ("maintenance", "Maintenance"),
        ("retired", "Retired"),
    )

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="assets")
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="assets")
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets")

    name = models.CharField(max_length=150)
    serial_number = models.CharField(max_length=120, blank=True, null=True)
    asset_tag = models.CharField(max_length=80, blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="available")

    current_holder = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="held_assets",
    )

    # ✅ NEW: lifecycle dates
    acquired_at = models.DateField(null=True, blank=True, help_text="Purchase/receipt date (used for EOL)")
    retired_at = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def eol_due_date(self):
        """
        Returns computed end-of-life due date based on acquired_at + category.service_life_months.
        """
        if not self.acquired_at:
            return None
        if not self.category or not self.category.eol_enabled:
            return None
        # naive month add (safe enough for business flagging)
        months = int(self.category.service_life_months or 0)
        year = self.acquired_at.year + (self.acquired_at.month - 1 + months) // 12
        month = (self.acquired_at.month - 1 + months) % 12 + 1
        day = min(self.acquired_at.day, 28)  # avoid invalid dates
        from datetime import date
        return date(year, month, day)

    @property
    def is_eol_due(self):
        due = self.eol_due_date
        if not due:
            return False
        from django.utils import timezone
        return due <= timezone.localdate()

    def __str__(self):
        return f"{self.agency.code} - {self.name}"



class AssetRequest(models.Model):
    STATUS_CHOICES = (
        ("draft", "Draft"),
        ("pending_manager", "Pending Manager Approval"),
        ("rejected", "Rejected"),
        ("approved_manager", "Approved by Manager"),
        ("pending_ict", "Pending ICT Assignment"),
        ("assigned", "Asset Assigned"),
        ("received", "Received & Verified"),
        ("cancelled", "Cancelled"),
    )

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="asset_requests")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_requests")

    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_requests")
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name="asset_requests")

    justification = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending_manager")

    # Approval chain tracking
    manager_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="asset_requests_approved",
    )
    manager_decision_at = models.DateTimeField(null=True, blank=True)
    manager_reject_reason = models.TextField(blank=True)

    assigned_asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True, related_name="requests")
    ict_assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="asset_requests_assigned",
    )
    ict_assigned_at = models.DateTimeField(null=True, blank=True)

    requester_verified_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["agency", "requester"]),
        ]

    def __str__(self):
        return f"{self.agency.code} request #{self.id} by {self.requester}"

    # ---------- helpers ----------
    def get_required_manager(self):
        """
        Manager = Unit Head/Asset Manager, but Core/Unallocated uses Operations Manager.
        """
        if self.unit and self.unit.is_core_unit:
            roles = getattr(self.agency, "asset_roles", None)
            return getattr(roles, "operations_manager", None)

        if not self.unit:
            roles = getattr(self.agency, "asset_roles", None)
            return getattr(roles, "operations_manager", None)

        # unit head is default manager
        return self.unit.unit_head

    def can_user_approve_as_manager(self, user):
        if not user or not self.agency_id or user.agency_id != self.agency_id:
            return False

        # Ops manager for core/unallocated
        roles = getattr(self.agency, "asset_roles", None)
        if roles and roles.operations_manager_id and user.id == roles.operations_manager_id:
            if (not self.unit) or (self.unit and self.unit.is_core_unit):
                return True

        if not self.unit:
            return False

        # unit head OR unit asset managers
        if self.unit.unit_head_id and user.id == self.unit.unit_head_id:
            return True
        return self.unit.asset_managers.filter(id=user.id).exists()

    def can_user_assign_as_ict(self, user):
        if not user or user.agency_id != self.agency_id:
            return False
        roles = getattr(self.agency, "asset_roles", None)
        if not roles:
            return False
        return roles.ict_custodian.filter(id=user.id).exists() or getattr(user, "role", "") == "ict_focal"

    def approve(self, by_user):
        self.status = "approved_manager"
        self.manager_approved_by = by_user
        self.manager_decision_at = timezone.now()
        self.manager_reject_reason = ""
        # next step
        self.status = "pending_ict"
        self.save()

    def reject(self, by_user, reason=""):
        self.status = "rejected"
        self.manager_approved_by = by_user
        self.manager_decision_at = timezone.now()
        self.manager_reject_reason = reason or ""
        self.save()

    def assign_asset(self, by_user, asset: Asset):
        # mark asset assigned
        asset.status = "assigned"
        asset.current_holder = self.requester
        asset.unit = self.unit or asset.unit
        asset.save(update_fields=["status", "current_holder", "unit"])

        self.assigned_asset = asset
        self.ict_assigned_by = by_user
        self.ict_assigned_at = timezone.now()
        self.status = "assigned"
        self.save()

    def verify_receipt(self, by_user):
        if by_user.id != self.requester_id:
            raise ValueError("Only the requester can verify receipt.")
        self.requester_verified_at = timezone.now()
        self.status = "received"
        self.save(update_fields=["requester_verified_at", "status"])


class AssetHistory(models.Model):
    EVENT_CHOICES = (
        ("registered", "Registered"),
        ("request_created", "Request Created"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("assigned", "Assigned"),
        ("receipt_verified", "Receipt Verified"),
        ("return_initiated", "Return Initiated"),
        ("return_received", "Return Received"),
        ("maintenance", "Marked Maintenance"),
        ("retired", "Retired/Disposed"),
        ("status_change", "Status Change"),
    )

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="asset_history")
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    event = models.CharField(max_length=40, choices=EVENT_CHOICES)
    note = models.TextField(blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["agency", "asset", "event"]),
            models.Index(fields=["agency", "created_at"]),
        ]

    def __str__(self):
        return f"{self.asset} - {self.event}"

class AssetReturnRequest(models.Model):
    STATUS_CHOICES = (
        ("pending_ict", "Pending ICT Verification"),
        ("received", "Received by ICT"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    )

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="asset_returns")
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="return_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_returns_requested")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_ict")
    reason = models.TextField(blank=True)

    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_returns_verified")
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["agency", "requested_by"]),
        ]

    def __str__(self):
        return f"Return #{self.id} - {self.asset}"

class AssetChangeRequest(models.Model):
    STATUS_CHOICES = (
        ("pending_manager", "Pending Asset Manager Approval"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    )

    agency = models.ForeignKey("Agency", on_delete=models.CASCADE, related_name="asset_change_requests")
    asset = models.ForeignKey("Asset", on_delete=models.CASCADE, related_name="change_requests")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_change_requests"
    )

    # store what ICT wants to change
    proposed_changes = models.JSONField(default=dict, blank=True)

    reason = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_manager")

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_change_requests_decided"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    manager_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["agency", "asset"]),
        ]

    def approve(self, manager_user, note=""):
        self.status = "approved"
        self.decided_by = manager_user
        self.decided_at = timezone.now()
        self.manager_note = note or ""
        self.save(update_fields=["status", "decided_by", "decided_at", "manager_note"])

    def reject(self, manager_user, note=""):
        self.status = "rejected"
        self.decided_by = manager_user
        self.decided_at = timezone.now()
        self.manager_note = note or ""
        self.save(update_fields=["status", "decided_by", "decided_at", "manager_note"])

    def __str__(self):
        return f"AssetChange #{self.id} - {self.asset}"



from .hr.models import EmployeeIDCardRequest