from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL

class EmployeeIDCardRequest(models.Model):
    STATUS_CHOICES = [
        ("submitted", "Submitted"),
        ("photo_pending", "Pending Photo Capture"),
        ("printed", "Printed"),
        ("issued", "Issued"),
        ("rejected", "Rejected"),
    ]

    REQUEST_TYPE_CHOICES = [
        ("new", "New ID Card"),
        ("replacement", "Replacement"),
        ("renewal", "Renewal"),
    ]

    for_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="idcard_requests_for"
    )
    requested_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="idcard_requests_made"
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="idcard_requests_approved"
    )

    request_type = models.CharField(max_length=20, choices=REQUEST_TYPE_CHOICES, default="new")
    reason = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="submitted")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    approved_at = models.DateTimeField(null=True, blank=True)
    printed_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(null=True, blank=True)

    printed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="idcard_requests_printed"
    )
    issued_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="idcard_requests_issued"
    )

    def __str__(self):
        return f"{self.for_user} - {self.get_request_type_display()} [{self.get_status_display()}]"

    # ---- Workflow helpers ----

    def mark_call_for_photo(self, user):
        """
        Step 1 after submission:
        LSA / SOC / HR says 'come for photo'.
        """
        self.status = "photo_pending"
        self.approver = user
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approver", "approved_at", "updated_at"])

    def mark_printed(self, user):
        """
        Step 2: card has been physically printed.
        """
        self.status = "printed"
        self.printed_by = user
        self.printed_at = timezone.now()
        self.save(update_fields=["status", "printed_by", "printed_at", "updated_at"])

    def mark_issued(self, user):
        """
        Step 3: card has been issued to the staff member.
        """
        self.status = "issued"
        self.issued_by = user
        self.issued_at = timezone.now()
        self.save(update_fields=["status", "issued_by", "issued_at", "updated_at"])

    def mark_rejected(self, user):
        self.status = "rejected"
        self.approver = user
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approver", "approved_at", "updated_at"])