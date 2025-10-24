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
    """One check session (e.g. daily @ 09:00)"""
    name        = models.CharField(max_length=120, help_text="e.g. Morning Net 09:00")
    started_at  = models.DateTimeField(default=timezone.now)
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="radio_checks_created")

    def __str__(self):
        return f"{self.name} ({self.started_at:%Y-%m-%d %H:%M})"


class RadioCheckEntry(models.Model):
    session     = models.ForeignKey(RadioCheckSession, on_delete=models.CASCADE, related_name="entries")
    device      = models.ForeignKey(CommunicationDevice, on_delete=models.CASCADE, related_name="check_entries")
    call_sign   = models.CharField(max_length=40)  # cached for historical integrity
    responded   = models.BooleanField(null=True)   # True/False/None(not attempted)
    noted_issue = models.CharField(max_length=200, blank=True)
    checked_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="radio_checks_done")
    checked_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("session", "device")
