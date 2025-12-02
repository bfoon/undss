from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

class CommunicationDevice(models.Model):
    DEVICE_TYPES = (
        ("hf", "HF Radio"),
        ("vhf", "VHF Radio"),
        ("satphone", "Satellite Phone"),
    )
    STATUS = (
        ("available", "Available (in store)"),
        ("with_user", "Issued to user"),
        ("damaged", "Damaged"),
        ("repair", "Under repair"),
    )

    device_type   = models.CharField(max_length=10, choices=DEVICE_TYPES)
    call_sign     = models.CharField(max_length=40, blank=True, help_text="Required for radios")
    imei          = models.CharField(max_length=32, blank=True, help_text="Required for sat phones")
    serial_number = models.CharField(max_length=64, blank=True)
    assigned_to   = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="comms_devices"
    )
    status        = models.CharField(max_length=12, choices=STATUS, default="available")
    notes         = models.TextField(blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["device_type", "status"]),
            models.Index(fields=["call_sign"]),
            models.Index(fields=["imei"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["call_sign"],
                name="uniq_radio_call_sign",
                condition=models.Q(device_type__in=["hf", "vhf"], call_sign__gt="")
            ),
            models.UniqueConstraint(
                fields=["imei"],
                name="uniq_satphone_imei",
                condition=models.Q(device_type="satphone", imei__gt="")
            ),
        ]

    def clean(self):
        if self.device_type in ["hf", "vhf"] and not self.call_sign:
            raise ValidationError("Call sign is required for HF/VHF radios.")
        if self.device_type == "satphone" and not self.imei:
            raise ValidationError("IMEI is required for satellite phones.")

    def __str__(self):
        label = self.get_device_type_display()
        ident = self.call_sign or self.imei or self.serial_number or "Device"
        return f"{label} · {ident}"

    @property
    def is_radio(self):
        return self.device_type in ["hf", "vhf"]


class RadioCheckSession(models.Model):
    name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="radio_check_sessions"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[("ongoing", "Ongoing"), ("completed", "Completed")],
        default="ongoing",
    )

    def __str__(self):
        return self.name


class RadioCheckEntry(models.Model):
    session = models.ForeignKey(
        RadioCheckSession, related_name="entries",
        on_delete=models.CASCADE
    )
    device = models.ForeignKey(
        "CommunicationDevice", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="check_entries"
    )
    call_sign = models.CharField(max_length=100, blank=True)
    responded = models.BooleanField(null=True, blank=True)
    noted_issue = models.TextField(blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="radio_checks_done"
    )
    checked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.session.name} – {self.call_sign or 'Unknown'}"
