from django.db import models
from django.conf import settings
from django.utils import timezone
import secrets
from django.contrib.auth import get_user_model

User = get_user_model()


class ParkingCard(models.Model):
    card_number = models.CharField(max_length=20, unique=True)
    owner_name = models.CharField(max_length=100)
    owner_id = models.CharField(max_length=50)
    phone = models.CharField(max_length=20)
    department = models.CharField(max_length=100)
    vehicle_make = models.CharField(max_length=50)
    vehicle_model = models.CharField(max_length=50)
    vehicle_plate = models.CharField(max_length=20)
    vehicle_color = models.CharField(max_length=30)
    issued_date = models.DateField(auto_now_add=True)
    expiry_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.card_number} - {self.owner_name}"


class Vehicle(models.Model):
    VEHICLE_TYPES = [
        ('un_agency', 'UN Agency Vehicle'),
        ('staff', 'Staff Vehicle'),
        ('visitor', 'Visitor Vehicle'),
    ]

    plate_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=10, choices=VEHICLE_TYPES)
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    color = models.CharField(max_length=30)
    un_agency = models.CharField(max_length=100, blank=True)  # For UN vehicles
    parking_card = models.ForeignKey(ParkingCard, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.plate_number} ({self.get_vehicle_type_display()})"


class VehicleMovement(models.Model):
    MOVEMENT_TYPES = [
        ('entry', 'Entry'),
        ('exit', 'Exit'),
    ]

    GATE_CHOICES = [
        ('front', 'Front Gate'),
        ('back', 'Back Gate'),
    ]

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    movement_type = models.CharField(max_length=5, choices=MOVEMENT_TYPES)
    gate = models.CharField(max_length=5, choices=GATE_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    driver_name = models.CharField(max_length=100, blank=True)
    purpose = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.vehicle.plate_number} - {self.movement_type} at {self.timestamp}"


def _gen_ax():
    return f"AX-{secrets.token_hex(4).upper()}"

class AgencyApprover(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agency_approver_for')
    agency_name = models.CharField(max_length=120)

    class Meta:
        unique_together = ('user', 'agency_name')

    def __str__(self):
        return f"{self.user.username} -> {self.agency_name}"


class AssetExit(models.Model):
    STATUS = [
        ('pending', 'Pending (Waiting Agency Approval)'),
        ('agency_approved', 'Agency Approved (Waiting LSA Clearance)'),
        ('lsa_cleared', 'LSA Cleared'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='asset_exits'
    )
    agency_name = models.CharField(max_length=120)
    reason = models.CharField(max_length=255, help_text="Why the assets are exiting (e.g., repair, transfer)")
    destination = models.CharField(max_length=200, help_text="Where assets are going")
    expected_date = models.DateField()
    escort_required = models.BooleanField(default=False, help_text="Tick if security escort is required")
    status = models.CharField(max_length=20, choices=STATUS, default='pending')

    # NEW: agency decision fields
    agency_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='asset_exits_agency_approved'
    )
    agency_approved_at = models.DateTimeField(null=True, blank=True)

    # LSA decision (as you already had)
    lsa_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cleared_asset_exits'
    )
    lsa_decided_at = models.DateTimeField(null=True, blank=True)
    # Guard sign-out / sign-in (optional, for audit at gate)
    signed_out_at = models.DateTimeField(null=True, blank=True)
    signed_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='asset_exits_signed_out'
    )
    signed_in_at = models.DateTimeField(null=True, blank=True)
    signed_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='asset_exits_signed_in'
    )

    # Tracking / meta
    code = models.CharField(max_length=32, unique=True, default=_gen_ax)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    # methods
    def approve_by_agency(self, user):
        self.agency_approver = user
        self.agency_approved_at = timezone.now()
        self.status = 'agency_approved'
        self.save(update_fields=['agency_approver', 'agency_approved_at', 'status'])

    def clear_by_lsa(self, user):
        self.lsa_user = user
        self.lsa_decided_at = timezone.now()
        self.status = 'lsa_cleared'
        self.save(update_fields=['lsa_user', 'lsa_decided_at', 'status'])

    def reject_by_lsa(self, user):
        self.lsa_user = user
        self.lsa_decided_at = timezone.now()
        self.status = 'rejected'
        self.save(update_fields=['lsa_user', 'lsa_decided_at', 'status'])

    def mark_signed_out(self, user):
        self.signed_out_by = user
        self.signed_out_at = timezone.now()
        self.save(update_fields=['signed_out_by', 'signed_out_at'])

    def mark_signed_in(self, user):
        self.signed_in_by = user
        self.signed_in_at = timezone.now()
        self.save(update_fields=['signed_in_by', 'signed_in_at'])

    def __str__(self):
        return f"Asset Exit {self.code} ({self.agency_name})"

class AssetExitItem(models.Model):
    asset_exit = models.ForeignKey(AssetExit, on_delete=models.CASCADE, related_name='items')
    description = models.CharField(max_length=255)
    category = models.CharField(max_length=100, blank=True)   # e.g., Equipment, Furniture
    quantity = models.PositiveIntegerField(default=1)
    serial_or_tag = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return f"{self.description} x{self.quantity}"


class ParkingCardRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]

    # who is requesting (usually a staff member)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='parking_card_requests'
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')

    # decision
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='parking_card_request_decisions'
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_notes = models.TextField(blank=True)

    # card holder details (often same as requester, but editable)
    owner_name = models.CharField(max_length=150)
    owner_id = models.CharField(max_length=50, help_text="Employee ID or National ID")
    phone = models.CharField(max_length=30, blank=True)
    department = models.CharField(max_length=100, blank=True)

    # vehicle details
    vehicle_make = models.CharField(max_length=100, blank=True)
    vehicle_model = models.CharField(max_length=100, blank=True)
    vehicle_plate = models.CharField(max_length=30)
    vehicle_color = models.CharField(max_length=50, blank=True)

    # desired expiry
    requested_expiry = models.DateField()

    def __str__(self):
        return f"PC Request #{self.id} - {self.owner_name} ({self.vehicle_plate}) - {self.get_status_display()}"

# --- KEY CONTROL ------------------------------------------------------------
from django.conf import settings
from django.db import models
from django.utils import timezone

class Key(models.Model):
    KEY_TYPES = (
        ('office', 'Office Key'),
        ('vehicle', 'Vehicle Key'),
    )

    code = models.CharField(max_length=50, unique=True, help_text="Unique key code/number engraved on tag")
    label = models.CharField(max_length=150, help_text="Human-friendly name e.g. 'Room 2A – Store'")
    key_type = models.CharField(max_length=10, choices=KEY_TYPES, default='office')
    vehicle = models.ForeignKey('vehicles.Vehicle', null=True, blank=True,
                                on_delete=models.SET_NULL,
                                help_text="Link for vehicle keys (optional)")
    location = models.CharField(max_length=120, blank=True, help_text="Rack/hook position")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['key_type', 'code']

    def __str__(self):
        return f"{self.get_key_type_display()} | {self.code} – {self.label}"

    @property
    def is_out(self):
        """True if there is an open KeyLog (not returned)"""
        return self.keylog_set.filter(returned_at__isnull=True).exists()

    @property
    def current_log(self):
        return self.keylog_set.filter(returned_at__isnull=True).order_by('-issued_at').first()


class KeyLog(models.Model):
    key = models.ForeignKey(Key, on_delete=models.CASCADE)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='keys_issued')
    issued_to_name = models.CharField(max_length=120)
    issued_to_agency = models.CharField(max_length=120, blank=True)
    issued_to_badge_id = models.CharField(max_length=60, blank=True)
    purpose = models.CharField(max_length=200, blank=True)

    issued_at = models.DateTimeField(default=timezone.now)
    due_back = models.DateTimeField(null=True, blank=True)

    returned_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='keys_received')

    condition_out = models.CharField(max_length=120, blank=True)
    condition_in = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['-issued_at']

    def __str__(self):
        status = "OUT" if self.returned_at is None else "IN"
        return f"{self.key.code} to {self.issued_to_name} at {self.issued_at:%Y-%m-%d %H:%M} [{status}]"

# --- Packages & Mailroom ---

from django.conf import settings
from django.db import models
from django.utils import timezone

class Package(models.Model):
    SENDER_TYPES = [
        ("gov", "Government / Law Enforcement"),
        ("private", "Private Sector"),
        ("ngo", "NGO"),
        ("nonprofit", "Non-Profit"),
        ("individual", "Individual"),
        ("other", "Other"),
    ]

    STATUS = [
        ("logged", "Logged at Gate"),
        ("to_reception", "Sent to Reception"),
        ("at_reception", "Received by Reception"),
        ("to_agency", "Sent to Agency/Registry"),
        ("with_agency", "Received by Agency/Registry"),
        ("delivered", "Delivered to Recipient"),
        ("returned", "Returned to Sender"),
        ("cancelled", "Cancelled"),
    ]

    # basics
    tracking_code = models.CharField(max_length=32, unique=True)
    sender_name = models.CharField(max_length=120)
    sender_type = models.CharField(max_length=20, choices=SENDER_TYPES)
    sender_org = models.CharField(max_length=120, blank=True)
    sender_contact = models.CharField(max_length=120, blank=True)

    # content / routing
    item_type = models.CharField(max_length=60, help_text="Package / Envelope / Box / Other")
    description = models.TextField(blank=True)
    destination_agency = models.CharField(max_length=120, help_text="UN Agency / Office (e.g., UNDP)")
    dest_focal_email = models.EmailField(blank=True, help_text="Agency focal point will be notified")
    for_recipient = models.CharField(max_length=120, blank=True)

    # custody & workflow
    status = models.CharField(max_length=20, choices=STATUS, default="logged")
    logged_at = models.DateTimeField(default=timezone.now)
    logged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="packages_logged")

    reception_received_at = models.DateTimeField(null=True, blank=True)
    reception_received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="packages_reception")

    agency_received_at = models.DateTimeField(null=True, blank=True)
    agency_received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="packages_agency")

    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_to = models.CharField(max_length=120, blank=True)
    delivered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="packages_delivered")

    # audit
    last_update = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-logged_at"]

    def __str__(self):
        return f"{self.tracking_code} · {self.item_type} → {self.destination_agency}"

class PackageEvent(models.Model):
    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Package.STATUS)
    who = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-at"]
