from django.db import models
from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model

User = get_user_model()


class Visitor(models.Model):
    APPROVAL_STATUS = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    VISITOR_TYPES = [
        ('individual', 'Individual'),
        ('group', 'Group'),
        ('official', 'Official Visit'),
        ('contractor', 'Contractor'),
    ]

    full_name = models.CharField(max_length=200)
    id_number = models.CharField(max_length=50)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    organization = models.CharField(max_length=200, blank=True)
    visitor_type = models.CharField(max_length=20, choices=VISITOR_TYPES)
    purpose_of_visit = models.TextField()
    person_to_visit = models.CharField(max_length=200)
    department_to_visit = models.CharField(max_length=200)

    # Vehicle information (if applicable)
    has_vehicle = models.BooleanField(default=False)
    vehicle_plate = models.CharField(max_length=20, blank=True)
    vehicle_make = models.CharField(max_length=50, blank=True)
    vehicle_model = models.CharField(max_length=50, blank=True)
    vehicle_color = models.CharField(max_length=30, blank=True)

    # Visit details
    expected_date = models.DateField()
    expected_time = models.TimeField()
    estimated_duration = models.CharField(max_length=50)

    # Approval workflow
    status = models.CharField(max_length=10, choices=APPROVAL_STATUS, default='pending')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='approved_visitors')
    approval_date = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    # Registration details
    registered_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registered_visitors')
    registered_at = models.DateTimeField(auto_now_add=True)

    # Visit tracking
    checked_in = models.BooleanField(default=False)
    check_in_time = models.DateTimeField(null=True, blank=True)
    checked_out = models.BooleanField(default=False)
    check_out_time = models.DateTimeField(null=True, blank=True)

    visitor_card = models.ForeignKey('VisitorCard', null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name='current_holder')

    clearance_valid_from = models.DateField(null=True, blank=True)
    clearance_valid_until = models.DateField(null=True, blank=True)

    card_issued_at = models.DateTimeField(null=True, blank=True)
    card_returned_at = models.DateTimeField(null=True, blank=True)

    # ─── Meeting link ─────────────────────────────────────────────────────────
    # Optional FK to the room-booking (meeting) this access request is tied to.
    # Uses a string reference so this app does not hard-depend on the accounts app.
    linked_booking = models.ForeignKey(
        'accounts.RoomBooking',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='visitor_access_requests',
        verbose_name='Linked meeting',
        help_text=(
            "If set, group members are automatically populated from the meeting's "
            "accepted registrants and kept in sync as new people register."
        ),
    )

    class Meta:
        ordering = ['-registered_at']

    def __str__(self):
        return f"{self.full_name} - {self.organization}"

    @property
    def total_group_size(self):
        """Returns total number of people including main visitor and group members"""
        if self.visitor_type == 'group':
            return 1 + self.group_members.count()
        return 1

    def clearance_is_active_today(self):
        """
        True if:
          - approved
          - and (no validity dates set) OR today is inside the window
        """
        if self.status != "approved":
            return False

        today = timezone.now().date()

        if not self.clearance_valid_from and not self.clearance_valid_until:
            return True

        start = self.clearance_valid_from or today
        end = self.clearance_valid_until or today
        return start <= today <= end

    def sync_members_from_booking(self):
        """
        Pull accepted MeetingAttendee records from linked_booking and create/update
        corresponding GroupMember records.

        Rules:
        - Only runs when linked_booking is set and visitor_type == 'group'.
        - Matches on email (case-insensitive). If a GroupMember with the same email
          already exists (and was sourced from the meeting), it is updated rather than
          duplicated.
        - Members that were manually added (meeting_attendee_id is None) are left alone.
        - Returns (created, updated) counts.
        """
        if not self.linked_booking or self.visitor_type != 'group':
            return 0, 0

        try:
            from accounts.models import MeetingAttendee  # lazy import to avoid circular
        except ImportError:
            return 0, 0

        accepted_qs = MeetingAttendee.objects.filter(
            booking=self.linked_booking,
            is_accepted=True,
        )

        created = 0
        updated = 0

        for attendee in accepted_qs:
            email = (attendee.email or '').strip().lower()

            # Try to find an existing GroupMember linked to this exact attendee
            existing = self.group_members.filter(meeting_attendee_id=attendee.pk).first()

            if not existing and email:
                # Also try to match by email in case it was created before attendee_id was stored
                existing = self.group_members.filter(
                    email__iexact=email,
                    meeting_attendee_id__isnull=False,
                ).first()

            name = (attendee.name or '').strip() or email
            phone = (getattr(attendee, 'phone', '') or '').strip()
            org = (getattr(attendee, 'organization', '') or '').strip()

            if existing:
                # Update mutable fields in case the attendee updated their profile
                existing.full_name = name or existing.full_name
                existing.contact_number = phone or existing.contact_number
                existing.notes = f"Synced from meeting: {self.linked_booking.title}"
                existing.meeting_attendee_id = attendee.pk
                existing.save(update_fields=[
                    'full_name', 'contact_number', 'notes', 'meeting_attendee_id',
                ])
                updated += 1
            else:
                GroupMember.objects.create(
                    visitor=self,
                    full_name=name or 'Unknown',
                    contact_number=phone,
                    email=email,
                    id_type='other',
                    id_number='',        # gate officer fills this in on arrival
                    nationality='',
                    notes=f"Synced from meeting: {self.linked_booking.title}",
                    meeting_attendee_id=attendee.pk,
                )
                created += 1

        return created, updated


class GroupMember(models.Model):
    """Individual member of a group visit"""
    ID_TYPES = [
        ('passport', 'Passport'),
        ('national_id', 'National ID Card'),
        ('driving_license', 'Driving License'),
        ('other', 'Other Photo ID'),
    ]

    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='group_members')
    full_name = models.CharField(max_length=200, help_text="Full name as shown on ID")
    contact_number = models.CharField(max_length=20, blank=True, help_text="Phone or mobile number")
    email = models.EmailField(blank=True, help_text="Email address (optional)")
    id_type = models.CharField(max_length=20, choices=ID_TYPES, help_text="Type of identification")
    id_number = models.CharField(max_length=100, blank=True, help_text="ID/Passport number")
    nationality = models.CharField(max_length=100, blank=True)

    # Photo ID upload (optional but recommended)
    id_photo = models.ImageField(upload_to='group_members/ids/%Y/%m/', blank=True, null=True,
                                 help_text="Upload a photo of the ID document")

    added_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, help_text="Additional notes about this member")

    # ─── Meeting-sync tracking ────────────────────────────────────────────────
    # When this member was auto-created from a MeetingAttendee record, we store
    # the PK of that attendee so we can avoid duplicating on re-sync.
    meeting_attendee_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="PK of the MeetingAttendee this record was synced from (if any).",
    )

    class Meta:
        ordering = ['full_name']
        verbose_name = 'Group Member'
        verbose_name_plural = 'Group Members'

    def __str__(self):
        return f"{self.full_name} ({self.get_id_type_display()}: {self.id_number or 'ID pending'})"

    @property
    def from_meeting(self):
        """True if this member was auto-synced from a meeting registration."""
        return self.meeting_attendee_id is not None


class VisitorLog(models.Model):
    ACTION_TYPES = [
        ('check_in', 'Check In'),
        ('check_out', 'Check Out'),
        ('approval', 'Approved'),
        ('rejection', 'Rejected'),
    ]

    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE)
    card = models.ForeignKey(
        'VisitorCard',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='logs'
    )
    action = models.CharField(max_length=10, choices=ACTION_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)
    performed_by = models.ForeignKey(User, on_delete=models.CASCADE)
    notes = models.TextField(blank=True)
    gate = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ['-timestamp']


class VisitorCard(models.Model):
    number = models.CharField(max_length=20, unique=True)
    is_active = models.BooleanField(default=True)
    in_use = models.BooleanField(default=False)
    issued_to = models.ForeignKey('Visitor', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='issued_card_history')
    issued_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='visitor_cards_issued')
    returned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='visitor_cards_returned')

    class Meta:
        ordering = ['number']

    def __str__(self):
        return self.number