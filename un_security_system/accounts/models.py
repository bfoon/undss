from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from datetime import timedelta
from django.db.models import F
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

    # Users that can approve bookings for this room
    approvers = models.ManyToManyField(
        User,
        related_name="rooms_to_approve",
        blank=True,
        help_text="Users who can approve bookings for this room."
    )

    def __str__(self):
        return f"{self.name} ({self.code})"


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


from .hr.models import EmployeeIDCardRequest