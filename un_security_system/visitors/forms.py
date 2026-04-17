from django import forms
from django.utils import timezone
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column, HTML
from crispy_forms.bootstrap import Field, InlineRadios
from .models import Visitor, VisitorLog, GroupMember


# ---------------------------------------------------------------------------
# Helper: build the meeting choices queryset
# We lazy-import to avoid hard circular dependency.
# ---------------------------------------------------------------------------

def _upcoming_bookings_qs():
    """Return approved RoomBookings that are today or in the future."""
    try:
        from accounts.models import RoomBooking
        today = timezone.now().date()
        return (
            RoomBooking.objects
            .filter(status='approved', date__gte=today)
            .order_by('date', 'start_time')
            .select_related('room')
        )
    except Exception:
        return []


class MeetingModelChoiceField(forms.ModelChoiceField):
    """Renders a RoomBooking as a human-readable label in the dropdown."""

    def label_from_instance(self, obj):
        try:
            return f"{obj.date.strftime('%d %b %Y')} {obj.start_time.strftime('%H:%M')} — {obj.title} ({obj.room.name})"
        except Exception:
            return str(obj)


class VisitorForm(forms.ModelForm):

    # ── Meeting link (optional) ──────────────────────────────────────────────
    # We define this as a proper ModelChoiceField so it renders as a <select>.
    # The queryset is overridden in __init__ so it stays current.
    linked_booking = MeetingModelChoiceField(
        queryset=None,          # set in __init__
        required=False,
        label='Link to a meeting',
        empty_label='— No meeting link —',
        help_text=(
            'Optional. Select an upcoming approved meeting. Accepted registrants '
            'will be automatically added as group members and kept in sync.'
        ),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = Visitor
        fields = [
            'full_name', 'id_number', 'phone', 'email', 'organization',
            'visitor_type', 'purpose_of_visit', 'person_to_visit',
            'department_to_visit', 'expected_date', 'expected_time',
            'estimated_duration', 'has_vehicle', 'vehicle_plate',
            'vehicle_make', 'vehicle_model', 'vehicle_color',
            'linked_booking',
        ]
        widgets = {
            'expected_date': forms.DateInput(attrs={'type': 'date', 'min': timezone.now().date()}),
            'expected_time': forms.TimeInput(attrs={'type': 'time'}),
            'purpose_of_visit': forms.Textarea(attrs={'rows': 3}),
            'has_vehicle': forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Set the live queryset for upcoming meetings
        self.fields['linked_booking'].queryset = _upcoming_bookings_qs()

        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'form-horizontal'
        self.helper.label_class = 'col-lg-3'
        self.helper.field_class = 'col-lg-9'

        # Set minimum date to today
        self.fields['expected_date'].widget.attrs['min'] = timezone.now().date().isoformat()

        # Required field styling
        for field_name, field in self.fields.items():
            if field.required:
                field.widget.attrs['class'] = field.widget.attrs.get('class', '') + ' required'

        self.helper.layout = Layout(
            Fieldset(
                'Personal Information',
                Row(
                    Column('full_name', css_class='form-group col-md-6 mb-3'),
                    Column('id_number', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('phone', css_class='form-group col-md-6 mb-3'),
                    Column('email', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('organization', css_class='form-group col-md-6 mb-3'),
                    Column('visitor_type', css_class='form-group col-md-6 mb-3'),
                ),
            ),
            Fieldset(
                'Visit Details',
                'purpose_of_visit',
                Row(
                    Column('person_to_visit', css_class='form-group col-md-6 mb-3'),
                    Column('department_to_visit', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('expected_date', css_class='form-group col-md-4 mb-3'),
                    Column('expected_time', css_class='form-group col-md-4 mb-3'),
                    Column('estimated_duration', css_class='form-group col-md-4 mb-3'),
                ),
            ),
            Fieldset(
                'Vehicle Information (Optional)',
                'has_vehicle',
                Row(
                    Column('vehicle_plate', css_class='form-group col-md-6 mb-3'),
                    Column('vehicle_make', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('vehicle_model', css_class='form-group col-md-6 mb-3'),
                    Column('vehicle_color', css_class='form-group col-md-6 mb-3'),
                ),
                css_id='vehicle-section'
            ),
            Submit('submit', 'Register Visitor', css_class='btn btn-primary btn-lg')
        )

    def clean_expected_date(self):
        expected_date = self.cleaned_data.get('expected_date')
        if expected_date and expected_date < timezone.now().date():
            raise forms.ValidationError('Expected date cannot be in the past.')
        return expected_date

    def clean(self):
        cleaned_data = super().clean()
        has_vehicle = cleaned_data.get('has_vehicle')
        vehicle_plate = cleaned_data.get('vehicle_plate')

        if has_vehicle and not vehicle_plate:
            raise forms.ValidationError('Vehicle plate number is required when visitor has a vehicle.')

        # If a meeting is linked, visitor_type must be 'group' — enforce silently
        # so the group-member section is always shown for meeting-linked access requests.
        linked_booking = cleaned_data.get('linked_booking')
        if linked_booking:
            cleaned_data['visitor_type'] = 'group'

        return cleaned_data


class GroupMemberForm(forms.ModelForm):
    """Form for adding individual group members"""

    class Meta:
        model = GroupMember
        fields = ['full_name', 'contact_number', 'email', 'id_type', 'id_number',
                  'nationality', 'id_photo', 'notes']
        widgets = {
            'full_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Full name as on ID document'
            }),
            'contact_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '+123 456 789'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'member@example.com'
            }),
            'id_type': forms.Select(attrs={'class': 'form-control'}),
            'id_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'ID/Passport number'
            }),
            'nationality': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Country of citizenship'
            }),
            'id_photo': forms.FileInput(attrs={
                'class': 'form-control',
                'accept': 'image/*'
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'Additional notes (optional)'
            }),
        }

    def clean_id_photo(self):
        """Validate uploaded ID photo"""
        id_photo = self.cleaned_data.get('id_photo')
        if id_photo:
            if id_photo.size > 5 * 1024 * 1024:
                raise forms.ValidationError('Image file size must be less than 5MB.')

            allowed_types = ['image/jpeg', 'image/jpg', 'image/png', 'image/gif']
            if hasattr(id_photo, 'content_type'):
                if id_photo.content_type not in allowed_types:
                    raise forms.ValidationError('Only JPEG, PNG, and GIF images are allowed.')

        return id_photo


class VisitorApprovalForm(forms.Form):
    ACTION_CHOICES = [
        ('approve', 'Approve'),
        ('reject', 'Reject'),
    ]

    action = forms.ChoiceField(
        choices=ACTION_CHOICES,
        widget=forms.RadioSelect,
        required=True
    )
    notes = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Optional notes...'}),
        required=False,
        label='Notes'
    )
    rejection_reason = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Reason for rejection...'}),
        required=False,
        label='Rejection Reason'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            InlineRadios('action'),
            'notes',
            'rejection_reason',
            Submit('submit', 'Submit Decision', css_class='btn btn-primary')
        )

    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get('action')
        rejection_reason = cleaned_data.get('rejection_reason')

        if action == 'reject' and not rejection_reason:
            raise forms.ValidationError('Rejection reason is required when rejecting a visitor.')

        return cleaned_data


class QuickVisitorCheckForm(forms.Form):
    """Quick form for checking in/out visitors"""
    visitor_id = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={
            'placeholder': 'Enter Visitor ID or scan badge',
            'class': 'form-control form-control-lg'
        }),
        label='Visitor ID'
    )
    gate = forms.ChoiceField(
        choices=[('front', 'Front Gate'), ('back', 'Back Gate')],
        widget=forms.Select(attrs={'class': 'form-control'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            'visitor_id',
            'gate',
            Submit('check_in', 'Check In', css_class='btn btn-success me-2'),
            Submit('check_out', 'Check Out', css_class='btn btn-warning')
        )


GATE_ACTIONS = (
    ('check_in', 'Check In'),
    ('check_out', 'Check Out'),
)


class GateCheckForm(forms.Form):
    action = forms.ChoiceField(
        choices=GATE_ACTIONS,
        widget=forms.Select(attrs={"class": "form-select"})
    )
    gate = forms.ChoiceField(
        choices=(
            ("front", "Front Gate"),
            ("back", "Back Gate"),
        ),
        initial="front",
        widget=forms.Select(attrs={"class": "form-select"})
    )
    id_number = forms.CharField(
        max_length=50,
        required=False,
        help_text="Required if missing for check-in.",
        widget=forms.TextInput(attrs={"class": "form-control"})
    )
    card_number = forms.CharField(
        max_length=20,
        required=False,
        help_text="Required for check-in to issue a visitor card.",
        widget=forms.TextInput(attrs={"class": "form-control"})
    )

    def __init__(self, *args, visitor=None, **kwargs):
        self.visitor = visitor
        super().__init__(*args, **kwargs)

        if self.visitor and getattr(self.visitor, "id_number", None):
            current_id = self.visitor.id_number
            self.fields["id_number"].help_text = (
                f"Current ID on file: {current_id}. "
                "Fill only if you need to update it at the gate."
            )
        else:
            self.fields["id_number"].help_text = "Required for first check-in."

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")
        id_number = cleaned.get("id_number")
        card_number = cleaned.get("card_number")

        if action:
            action = str(action)

        if action == "check_in":
            if not card_number:
                self.add_error("card_number", "Card number is required for check-in.")

            visitor_has_id = bool(getattr(self.visitor, "id_number", None)) if self.visitor else False
            if not id_number and not visitor_has_id:
                self.add_error("id_number", "Visitor ID is required for check-in.")

        return cleaned