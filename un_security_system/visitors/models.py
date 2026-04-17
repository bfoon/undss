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
    id_number = models.CharField(max_length=50, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    organization = models.CharField(max_length=200, blank=True)
    visitor_type = models.CharField(max_length=20, choices=VISITOR_TYPES)
    purpose_of_visit = models.TextField()
    person_to_visit = models.CharField(max_length=200)
    department_to_visit = models.CharField(max_length=200, blank=True)

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

    # Visit tracking — for meeting-linked visitors the PRIMARY record is not
    # individually checked in; check-in is per-member only.
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
    linked_booking = models.ForeignKey(
        'accounts.RoomBooking',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='visitor_access_requests',
        verbose_name='Linked meeting',
        help_text=(
            "If set, this access request is tied to a specific meeting. "
            "All fields are auto-populated from the meeting and members are "
            "synced from accepted registrants."
        ),
    )

    class Meta:
        ordering = ['-registered_at']

    def __str__(self):
        return f"{self.full_name} - {self.organization}"

    @property
    def is_meeting_linked(self):
        return self.linked_booking_id is not None

    @property
    def total_group_size(self):
        """Returns total number of people including main visitor and group members"""
        if self.visitor_type == 'group':
            return 1 + self.group_members.count()
        return 1

    @property
    def members_checked_in_count(self):
        return self.group_members.filter(checked_in=True, checked_out=False).count()

    @property
    def members_pending_count(self):
        return self.group_members.filter(checked_in=False).count()

    def clearance_is_active_today(self):
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
        Returns (created, updated) counts.
        """
        if not self.linked_booking or self.visitor_type != 'group':
            return 0, 0

        try:
            from accounts.models import MeetingAttendee
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
            existing = self.group_members.filter(meeting_attendee_id=attendee.pk).first()
            if not existing and email:
                existing = self.group_members.filter(
                    email__iexact=email,
                    meeting_attendee_id__isnull=False,
                ).first()

            name = (attendee.name or '').strip() or email
            phone = (getattr(attendee, 'phone', '') or '').strip()

            if existing:
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
                    id_number='',
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

    ATTENTION_STATUS = [
        ('ok', 'OK'),
        ('needs_attention', 'Needs Attention'),
        ('cleared', 'Cleared by Host'),
    ]

    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='group_members')
    full_name = models.CharField(max_length=200, help_text="Full name as shown on ID")
    contact_number = models.CharField(max_length=20, blank=True, help_text="Phone or mobile number")
    email = models.EmailField(blank=True, help_text="Email address (optional)")
    id_type = models.CharField(max_length=20, choices=ID_TYPES, help_text="Type of identification", default='other')
    id_number = models.CharField(max_length=100, blank=True, help_text="ID/Passport number (can be filled at gate)")
    nationality = models.CharField(max_length=100, blank=True)

    # Photo ID — can be face photo taken at gate
    id_photo = models.ImageField(upload_to='group_members/ids/%Y/%m/', blank=True, null=True,
                                 help_text="Photo of ID document or face photo taken at gate")

    added_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, help_text="Additional notes about this member")

    # ── Individual check-in tracking ────────────────────────────────────────
    checked_in = models.BooleanField(default=False)
    check_in_time = models.DateTimeField(null=True, blank=True)
    checked_out = models.BooleanField(default=False)
    check_out_time = models.DateTimeField(null=True, blank=True)

    # Visitor card issued to THIS member individually
    assigned_card = models.ForeignKey(
        'VisitorCard',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='member_holders',
    )

    # ── Gate attention flag ──────────────────────────────────────────────────
    # Gate officer can flag this person for host verification
    gate_attention = models.CharField(
        max_length=20, choices=ATTENTION_STATUS, default='ok',
    )
    gate_attention_note = models.TextField(
        blank=True,
        help_text="Reason for flagging this person for host attention.",
    )
    gate_attention_raised_at = models.DateTimeField(null=True, blank=True)
    gate_attention_cleared_at = models.DateTimeField(null=True, blank=True)

    # ── Meeting-sync tracking ────────────────────────────────────────────────
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

    @property
    def needs_attention(self):
        return self.gate_attention == 'needs_attention'

    def sync_to_meeting_attendee(self, fields_updated: dict):
        """
        Push gate-captured updates (id_number, phone, photo, notes) back to the
        linked MeetingAttendee so the meeting attendance list stays current.
        fields_updated is a dict like {'phone': '...', 'organization': '...'}
        """
        if not self.meeting_attendee_id:
            return
        try:
            from accounts.models import MeetingAttendee
            attendee = MeetingAttendee.objects.filter(pk=self.meeting_attendee_id).first()
            if not attendee:
                return
            changed = []
            field_map = {
                'phone': 'phone',
                'organization': 'organization',
                'full_name': 'name',
            }
            for local_field, attendee_field in field_map.items():
                if local_field in fields_updated:
                    setattr(attendee, attendee_field, fields_updated[local_field])
                    changed.append(attendee_field)
            if changed:
                attendee.save(update_fields=changed)
        except Exception:
            pass


class VisitorLog(models.Model):
    ACTION_TYPES = [
        ('check_in', 'Check In'),
        ('check_out', 'Check Out'),
        ('approval', 'Approved'),
        ('rejection', 'Rejected'),
        ('member_check_in', 'Member Check In'),
        ('member_check_out', 'Member Check Out'),
        ('gate_flag', 'Flagged for Attention'),
        ('gate_cleared', 'Attention Cleared'),
    ]

    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE)
    card = models.ForeignKey(
        'VisitorCard',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='logs'
    )
    action = models.CharField(max_length=20, choices=ACTION_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)
    performed_by = models.ForeignKey(User, on_delete=models.CASCADE)
    notes = models.TextField(blank=True)
    gate = models.CharField(max_length=10, blank=True)

    # Optional: link log entry to a specific group member
    group_member = models.ForeignKey(
        'GroupMember',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='logs',
    )

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