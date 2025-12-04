from django.db import models
from django.conf import settings
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
    estimated_duration = models.CharField(max_length=50)  # e.g., "2 hours"

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
    card_issued_at = models.DateTimeField(null=True, blank=True)
    card_returned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-registered_at']

    def __str__(self):
        return f"{self.full_name} - {self.organization}"


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
    gate = models.CharField(max_length=10, blank=True)  # front/back

    class Meta:
        ordering = ['-timestamp']

class VisitorCard(models.Model):
    number = models.CharField(max_length=20, unique=True)
    is_active = models.BooleanField(default=True)       # card exists/usable
    in_use = models.BooleanField(default=False)         # currently issued
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