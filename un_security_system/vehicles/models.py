from django.db import models
from django.conf import settings
from django.utils import timezone
import secrets
import hashlib, uuid
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
    expected_date = models.DateField(null=True, blank=True,)
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

class PackageFlowTemplate(models.Model):
    """
    A workflow template owned by one agency.
    Only packages destined for that agency can use this template.
    Only users in that agency (with the ict_focal role) can configure it.
    """
    DIRECTION = [
                ('incoming', 'Incoming Mail / Package'),
                ('outgoing', 'Outgoing Mail / Package'),
            ]
    direction = models.CharField(
        max_length=10, choices=DIRECTION, default='incoming',
        help_text="Whether this template governs incoming or outgoing items"
    )
    agency = models.ForeignKey(
        'accounts.Agency',  # already in accounts/models.py
        on_delete=models.CASCADE,
        related_name='package_flow_templates',
        help_text="Agency that owns and uses this workflow"
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='package_flow_templates_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['agency__name', 'name']
        unique_together = ('agency', 'name', 'direction')  # name unique within an agency

    def __str__(self):
        return f"[{self.agency.code}] {self.name}"

    @property
    def steps_ordered(self):
        return self.steps.order_by('order')

    @property
    def first_step(self):
        return self.steps.order_by('order').first()

    @property
    def step_count(self):
        return self.steps.count()


class PackageFlowStep(models.Model):
    """
    One step in a PackageFlowTemplate.

    Access control — ONE of two modes (or both):
      • allowed_roles  → any user in the agency whose role matches
      • allowed_users  → specific named users from the agency

    Notifications — ONE of two modes (or both):
      • notify_next_handler_roles → in-app to all agency users of those roles
      • notify_next_users         → in-app to specific named agency users
    """
    STEP_TYPES = [
        ('log', 'Initial Logging'),
        ('receive', 'Receive / Accept'),
        ('verify', 'Verify / Inspect'),
        ('scan', 'Scan Content'),
        ('stamp', 'Stamp / Sign'),
        ('route', 'Route to Unit / Project'),
        ('deliver', 'Deliver to Recipient'),
        ('return', 'Return to Sender'),
        ('custom', 'Custom Step'),
    ]

    ROLE_CHOICES = [
        ('requester', 'Requester (Staff)'),
        ('data_entry', 'Data Entry (Security Guard)'),
        ('lsa', 'Local Security Associate'),
        ('soc', 'Security Operations Center'),
        ('reception', 'Receptionist'),
        ('registry', 'Registry'),
        ('ict_focal', 'ICT Focal Point'),
        ('csm', 'Common Services Manager'),
        ('agency_hr', 'Agency HR'),
    ]

    template = models.ForeignKey(PackageFlowTemplate, on_delete=models.CASCADE, related_name='steps')
    order = models.PositiveIntegerField(default=1, help_text="Steps execute in ascending order")
    name = models.CharField(max_length=120)
    step_type = models.CharField(max_length=20, choices=STEP_TYPES, default='custom')
    status_code = models.SlugField(
        max_length=60,
        help_text="Machine-readable status slug written to Package.status"
    )
    description = models.CharField(max_length=255, blank=True)

    # ── Access control ─────────────────────────────────────────────────────────
    allowed_roles = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated roles allowed to act (e.g. reception,registry). "
                  "Only users in this agency with these roles will see the action."
    )
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name='package_steps_allowed',
        help_text="Specific users from this agency who can perform this step. "
                  "If set alongside roles, either match grants access."
    )

    # ── Required actions ──────────────────────────────────────────────────────
    requires_note = models.BooleanField(default=False)
    requires_scan = models.BooleanField(default=False)
    requires_stamp = models.BooleanField(default=False)
    requires_routing = models.BooleanField(default=False)
    requires_recipient_signature = models.BooleanField(default=False)

    # ── Notifications ─────────────────────────────────────────────────────────
    notify_requester = models.BooleanField(default=False, help_text="Notify the original package logger")
    notify_focal_email = models.BooleanField(default=True,
                                             help_text="Email the agency focal-point address on the package")
    notify_recipient = models.BooleanField(default=False, help_text="Email the named recipient on delivery")

    notify_next_handler_roles = models.CharField(
        max_length=300, blank=True,
        help_text="Comma-separated roles in this agency to notify (in-app) when the next step is ready"
    )
    notify_next_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name='package_steps_notify',
        help_text="Specific agency users to notify (in-app) when the next step is ready"
    )
    notify_sender = models.BooleanField(
        default=False,
        help_text="Email the sender when this step completes to confirm their "
                "package has been delivered / reached its destination"
    )

    # ── Flow control ──────────────────────────────────────────────────────────
    is_terminal = models.BooleanField(
        default=False,
        help_text="Tick if this is the last step. The workflow closes when this completes."
    )

    class Meta:
        ordering = ['template', 'order']
        unique_together = [
            ('template', 'order'),
            ('template', 'status_code'),
        ]

    def __str__(self):
        return f"{self.template} › Step {self.order}: {self.name}"

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def allowed_roles_list(self):
        return [r.strip() for r in self.allowed_roles.split(',') if r.strip()]

    @property
    def notify_next_roles_list(self):
        return [r.strip() for r in self.notify_next_handler_roles.split(',') if r.strip()]

    def next_step(self):
        return self.template.steps.filter(order__gt=self.order).order_by('order').first()

    @property
    def required_actions_display(self):
        acts = []
        if self.requires_note:                acts.append("Note")
        if self.requires_scan:                acts.append("Scan/Photo")
        if self.requires_stamp:               acts.append("Stamp")
        if self.requires_routing:             acts.append("Route to Unit")
        if self.requires_recipient_signature: acts.append("Recipient Signature")
        return acts or ["No special requirements"]

    def user_can_act(self, user) -> bool:
        """Return True if `user` is allowed to perform this step."""
        if user.is_superuser:
            return True
        # Must belong to same agency as the template
        if getattr(user, 'agency_id', None) != self.template.agency_id:
            return False
        # Role match
        if self.allowed_roles_list and getattr(user, 'role', None) in self.allowed_roles_list:
            return True
        # Named-user match
        if self.allowed_users.filter(pk=user.pk).exists():
            return True
        # No restrictions set → any agency member can act
        if not self.allowed_roles and not self.allowed_users.exists():
            return True
        return False


class PackageStepLog(models.Model):
    """Immutable audit record for every step performed on a Package."""
    package = models.ForeignKey('Package', on_delete=models.CASCADE, related_name='step_logs')
    step = models.ForeignKey(PackageFlowStep, on_delete=models.SET_NULL, null=True, blank=True)
    step_name = models.CharField(max_length=120)
    step_order = models.PositiveIntegerField(default=0)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='package_step_logs'
    )
    performed_at = models.DateTimeField(default=timezone.now)

    note = models.TextField(blank=True)
    scan_file = models.FileField(upload_to='package_scans/%Y/%m/', blank=True, null=True)
    stamped = models.BooleanField(default=False)
    routed_to = models.CharField(max_length=200, blank=True)
    recipient_name = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['performed_at']

    def __str__(self):
        return f"{self.package.tracking_code} › {self.step_name} ({self.performed_at:%Y-%m-%d %H:%M})"


class PackageNotification(models.Model):
    """In-app notification for a package workflow event."""
    package = models.ForeignKey('Package', on_delete=models.CASCADE, related_name='notifications')
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='package_notifications'
    )
    message = models.CharField(max_length=500)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"→ {self.recipient} | {self.package.tracking_code}: {self.message[:60]}"

class Package(models.Model):
    SENDER_TYPES = [
        ("gov", "Government / Law Enforcement"),
        ("private", "Private Sector"),
        ("ngo", "NGO"),
        ("nonprofit", "Non-Profit"),
        ("individual", "Individual"),
        ("other", "Other"),
    ]

    DIRECTION = [
        ('incoming', 'Incoming'),
        ('outgoing', 'Outgoing'),
    ]
    direction = models.CharField(
        max_length=10, choices=DIRECTION, default='incoming'
    )

    # basics
    tracking_code = models.CharField(max_length=32, unique=True)
    sender_name = models.CharField(max_length=120)
    sender_type = models.CharField(max_length=20, choices=SENDER_TYPES)
    sender_org = models.CharField(max_length=120, blank=True)
    sender_contact = models.CharField(max_length=120, blank=True)
    sender_email = models.EmailField(
                blank=True,
                help_text="Sender's email address for delivery confirmation (optional)"
            )

    # content / routing
    item_type = models.CharField(max_length=60, help_text="Package / Envelope / Box / Other")
    description = models.TextField(blank=True)
    destination_agency = models.CharField(max_length=120, help_text="UN Agency / Office (e.g., UNDP)")
    dest_focal_email = models.EmailField(blank=True, help_text="Agency focal point will be notified")
    for_recipient = models.CharField(max_length=120, blank=True)

    recipient_org = models.CharField(max_length=120, blank=True, help_text="External recipient organisation")
    recipient_address = models.TextField(blank=True, help_text="Delivery address for outgoing items")
    recipient_email = models.EmailField(blank=True, help_text="Recipient email — for outgoing delivery confirmation")

    # custody & workflow
    status = models.CharField(max_length=60, default="logged")
    flow_template = models.ForeignKey(
                  'PackageFlowTemplate', on_delete=models.PROTECT,
                  null=True, blank=True, related_name='packages',
                  help_text="Workflow template governing this package"
              )
    current_step = models.ForeignKey(
                  'PackageFlowStep', on_delete=models.SET_NULL,
                  null=True, blank=True, related_name='packages_at_step'
              )
    is_complete = models.BooleanField(default=False)
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
    package = models.ForeignKey(Package, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS)
    who = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-at"]


class UserSignature(models.Model):
    """
    A saved signature profile for a user.
    One of three types: uploaded image, font-rendered text, or drawn (canvas PNG).
    Only one signature can be active per user at a time.
    """
    SIG_TYPES = [
        ('upload', 'Uploaded Image'),
        ('font', 'Font / Typed'),
        ('draw', 'Drawn On-Screen'),
    ]
    FONT_CHOICES = [
        ('dancing', 'Dancing Script'),
        ('pacifico', 'Pacifico'),
        ('satisfy', 'Satisfy'),
        ('great_vibes', 'Great Vibes'),
        ('allura', 'Allura'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='signatures')
    sig_type = models.CharField(max_length=10, choices=SIG_TYPES)
    is_active = models.BooleanField(default=True,
                                    help_text="This is the user's current default signature")

    # upload
    image_file = models.ImageField(upload_to='signatures/uploads/%Y/', blank=True, null=True)

    # font
    font_name = models.CharField(max_length=20, choices=FONT_CHOICES, blank=True)
    font_text = models.CharField(max_length=120, blank=True,
                                 help_text="Text to render as signature (defaults to full name)")

    # drawn — stored as base64 PNG data-URL in the DB (small canvas image)
    drawn_data = models.TextField(blank=True,
                                  help_text="Base64 PNG from signature pad canvas")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} — {self.get_sig_type_display()}"

    def save(self, *args, **kwargs):
        # Ensure only one active signature per user
        if self.is_active:
            UserSignature.objects.filter(
                user=self.user, is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)


class PackageDocument(models.Model):
    """
    A scanned document attached to a PackageStepLog.
    Holds the file and tracks its signing lifecycle.
    """
    STATUS = [
        ('uploaded', 'Uploaded — Awaiting Annotation'),
        ('annotation_ready', 'Signature Fields Placed'),
        ('pending_signature', 'Sent for Signature'),
        ('signed', 'Fully Signed'),
    ]

    step_log = models.ForeignKey('PackageStepLog', on_delete=models.CASCADE,
                                 related_name='documents')
    file = models.FileField(upload_to='package_docs/%Y/%m/')
    filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, related_name='package_docs_uploaded')
    uploaded_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS, default='uploaded')

    # SHA-256 of the original file — set on upload, used to detect tampering
    file_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.filename} ({self.get_status_display()})"

    def compute_hash(self):
        """Compute and store SHA-256 of the uploaded file."""
        h = hashlib.sha256()
        self.file.seek(0)
        for chunk in iter(lambda: self.file.read(8192), b''):
            h.update(chunk)
        self.file.seek(0)
        return h.hexdigest()


class SignatureField(models.Model):
    """
    A signature placeholder placed on a document page by the handler.
    Stores position as percentages of the page so it works at any render size.
    """
    document = models.ForeignKey(PackageDocument, on_delete=models.CASCADE,
                                 related_name='signature_fields')
    page_number = models.PositiveIntegerField(default=1)

    # Position as % of rendered page width/height (0–100)
    pos_x_pct = models.FloatField(help_text="Left edge % from page left")
    pos_y_pct = models.FloatField(help_text="Top edge % from page top")
    width_pct = models.FloatField(default=20.0)
    height_pct = models.FloatField(default=6.0)

    label = models.CharField(max_length=100, blank=True,
                             help_text="e.g. 'Agency Focal Point', 'Authorising Officer'")

    # Who should sign this field
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True,
                                    related_name='signature_fields_assigned')

    order = models.PositiveIntegerField(default=1,
                                        help_text="Signing order if multiple signers")
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ['page_number', 'order']

    def __str__(self):
        return f"Field {self.order} on p.{self.page_number} — {self.label}"

    @property
    def is_signed(self):
        return hasattr(self, 'signature_record') and self.signature_record is not None


class SignatureRecord(models.Model):
    """
    Immutable record of a signature being applied to a SignatureField.
    Stores the rendered signature image + a hash chain for audit.
    """
    field = models.OneToOneField(SignatureField, on_delete=models.CASCADE,
                                 related_name='signature_record')
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  related_name='signature_records')
    sig_profile = models.ForeignKey(UserSignature, on_delete=models.PROTECT,
                                    null=True, blank=True)

    # Rendered signature (PNG) stored as base64 or file
    rendered_image = models.TextField(blank=True,
                                      help_text="Base64 PNG of the rendered signature")

    signed_at = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    # Audit hash: SHA-256(document_file_hash + field_id + user_id + timestamp)
    audit_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ['signed_at']

    def __str__(self):
        return f"Signed by {self.signed_by.username} at {self.signed_at:%Y-%m-%d %H:%M}"

    @staticmethod
    def compute_audit_hash(doc_hash, field_id, user_id, timestamp_iso):
        raw = f"{doc_hash}|{field_id}|{user_id}|{timestamp_iso}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def verify(self):
        """Re-compute and compare the stored audit hash."""
        expected = self.compute_audit_hash(
            self.field.document.file_hash,
            self.field.pk,
            self.signed_by_id,
            self.signed_at.isoformat(),
        )
        return expected == self.audit_hash