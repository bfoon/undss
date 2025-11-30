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


from .hr.models import EmployeeIDCardRequest