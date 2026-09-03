from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.core.exceptions import ValidationError

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column

from .models import (
    SecurityIncident, RegistrationInvite, RoomBooking, Room, RoomAmenity,
    RoomApprover, RoomBookingSeries, MeetingAttendee,
)

User = get_user_model()


class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(max_length=20, required=False)
    employee_id = forms.CharField(max_length=20, required=False)

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name', 'role', 'phone', 'employee_id')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Fieldset(
                'Account Information',
                Row(
                    Column('username', css_class='form-group col-md-6 mb-3'),
                    Column('email', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('first_name', css_class='form-group col-md-6 mb-3'),
                    Column('last_name', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('password1', css_class='form-group col-md-6 mb-3'),
                    Column('password2', css_class='form-group col-md-6 mb-3'),
                ),
            ),
            Fieldset(
                'Role & Contact Information',
                Row(
                    Column('role', css_class='form-group col-md-6 mb-3'),
                    Column('employee_id', css_class='form-group col-md-6 mb-3'),
                ),
                'phone',
            ),
            Submit('submit', 'Create User', css_class='btn btn-primary')
        )


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name', 'role', 'phone', 'employee_id')


class SecurityIncidentForm(forms.ModelForm):
    class Meta:
        model = SecurityIncident
        fields = ['title', 'description', 'severity', 'location']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'title': forms.TextInput(attrs={'placeholder': 'Brief incident title'}),
            'location': forms.TextInput(attrs={'placeholder': 'e.g., Front Gate, Building A, etc.'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            'title',
            'description',
            Row(
                Column('severity', css_class='form-group col-md-6 mb-3'),
                Column('location', css_class='form-group col-md-6 mb-3'),
            ),
            Submit('submit', 'Report Incident', css_class='btn btn-danger')
        )


class LoginForm(forms.Form):
    login = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Username or Email'
        })
    )

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Password'
        })
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            'login',
            'password',
            Submit('submit', 'Login', css_class='btn btn-primary btn-lg w-100')
        )

    def clean(self):
        cleaned_data = super().clean()
        login_value = (cleaned_data.get('login') or '').strip()
        password = cleaned_data.get('password')

        if not login_value or not password:
            return cleaned_data

        # 1) Try username
        user = authenticate(self.request, username=login_value, password=password)

        # 2) Try email (case-insensitive)
        if user is None and "@" in login_value:
            user_obj = User.objects.filter(email__iexact=login_value).first()
            if user_obj:
                user = authenticate(self.request, username=user_obj.username, password=password)

        if user is None:
            raise forms.ValidationError("Invalid username/email or password.")

        if not user.is_active:
            raise forms.ValidationError("This account is inactive.")

        self.user = user
        return cleaned_data


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Row(
                Column('first_name', css_class='form-group col-md-6 mb-3'),
                Column('last_name', css_class='form-group col-md-6 mb-3'),
            ),
            Row(
                Column('email', css_class='form-group col-md-6 mb-3'),
                Column('phone', css_class='form-group col-md-6 mb-3'),
            ),
            Submit('submit', 'Update Profile', css_class='btn btn-primary')
        )


# Which roles ICT is allowed to assign (adjust as needed)
ICT_ASSIGNABLE_ROLES = [
    ('requester', 'Requester (Staff)'),
    ('reception', 'Receptionist'),
    ('registry', 'Registry'),
    ('data_entry', 'Data Entry (Security Guard)'),
]


def _scope_office_field(form, request_user, allow_blank=False):
    """
    Limit a `country_office` field to the offices this person may assign.

    Superusers get every active office. Anyone else gets their own, plus
    whatever the record already has — so editing a user who sits in another
    office shows that office rather than silently dropping it.

    Called from both ICT user forms and shared with the room form's equivalent
    in accounts/room_access.py.
    """
    field = form.fields.get("country_office")
    if field is None:
        return

    try:
        from tenancy.models import CountryOffice
    except ImportError:
        return

    offices = CountryOffice.objects.filter(is_active=True).select_related("agency")

    if not getattr(request_user, "is_superuser", False):
        own = getattr(request_user, "country_office_id", None)
        current = getattr(form.instance, "country_office_id", None)
        keep = [pk for pk in (own, current) if pk]
        offices = offices.filter(pk__in=keep) if keep else offices.none()

    field.queryset = offices.order_by("agency__code", "name")
    field.required = not allow_blank

    if not form.instance.pk and getattr(request_user, "country_office_id", None):
        field.initial = request_user.country_office_id

    # One choice and no authority to change it: show it, but do not pretend
    # it is a decision. The view re-checks on save either way.
    if field.queryset.count() == 1 and not getattr(request_user, "is_superuser", False):
        field.empty_label = None
        field.widget.attrs["disabled"] = "disabled"
        field.required = False


class ICTUserCreateForm(forms.ModelForm):
    """Form for ICT focal points to create users in their agency."""

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone',
                  'employee_id', 'role', 'country_office']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter username'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter first name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter last name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'user@example.com'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+1234567890'}),
            'employee_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter employee ID'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'country_office': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)
        self.fields['role'].choices = [('', '---------')] + ICT_ASSIGNABLE_ROLES
        self.fields['username'].required = True
        self.fields['role'].required = True
        self.fields['username'].help_text = 'Required. 150 characters or fewer.'
        self.fields['email'].help_text = 'Optional. Used for password reset links.'
        self.fields['employee_id'].help_text = 'Optional. Internal employee identifier.'
        self.fields['role'].help_text = 'Select the role for this user.'
        if 'country_office' in self.fields:
            self.fields['country_office'].label = 'Country office'
            self.fields['country_office'].help_text = (
                'Which office this user belongs to. Decides which records they see.'
            )
            _scope_office_field(self, self.request_user)

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if username and User.objects.filter(username=username).exists():
            raise ValidationError('A user with this username already exists.')
        return username

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip()
        if email and User.objects.filter(email__iexact=email).exists():
            raise ValidationError('A user with this email already exists.')
        return email

    def clean_employee_id(self):
        employee_id = self.cleaned_data.get('employee_id')
        if employee_id:
            employee_id = employee_id.strip()
            if User.objects.filter(employee_id=employee_id).exists():
                raise ValidationError('A user with this employee ID already exists.')
        return employee_id

    def clean_role(self):
        role = self.cleaned_data.get('role')
        if role:
            allowed_roles = [r[0] for r in ICT_ASSIGNABLE_ROLES]
            if role not in allowed_roles:
                raise ValidationError('You are not allowed to assign this role.')
        return role

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.request_user and self.request_user.agency_id:
            user.agency_id = self.request_user.agency_id

        # A disabled select is not submitted, so fall back to the creator's own
        # office. Without this the account is created with no office and shows
        # up in nobody's list.
        if not getattr(user, 'country_office_id', None) and self.request_user:
            own_office_id = getattr(self.request_user, 'country_office_id', None)
            if own_office_id:
                user.country_office_id = own_office_id

        # Keep agency and office consistent — the office knows its agency, and
        # a mismatch between the two produces a user nobody can find.
        office = getattr(user, 'country_office', None)
        if office is not None and getattr(office, 'agency_id', None):
            user.agency_id = office.agency_id

        user.is_active = True
        user.set_unusable_password()
        if commit:
            user.save()
        return user


class ICTUserUpdateForm(forms.ModelForm):
    """Form for ICT focal points to update users in their agency."""

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone',
                  'employee_id', 'role', 'country_office']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter username'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter first name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter last name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'user@example.com'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+1234567890'}),
            'employee_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter employee ID'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'country_office': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)
        self.fields['role'].choices = [('', '---------')] + ICT_ASSIGNABLE_ROLES
        self.fields['username'].required = True
        self.fields['role'].required = True
        if 'country_office' in self.fields:
            self.fields['country_office'].label = 'Country office'
            self.fields['country_office'].help_text = (
                'Moving someone to another office changes what they can see.'
            )
            _scope_office_field(self, self.request_user)

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if username and User.objects.filter(username=username).exclude(pk=self.instance.pk).exists():
            raise ValidationError('A user with this username already exists.')
        return username

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if email:
            email = email.strip()
            if User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
                raise ValidationError('A user with this email already exists.')
        return email

    def clean_employee_id(self):
        employee_id = self.cleaned_data.get('employee_id')
        if employee_id:
            employee_id = employee_id.strip()
            if User.objects.filter(employee_id=employee_id).exclude(pk=self.instance.pk).exists():
                raise ValidationError('A user with this employee ID already exists.')
        return employee_id

    def clean_role(self):
        role = self.cleaned_data.get('role')
        if role:
            allowed_roles = [r[0] for r in ICT_ASSIGNABLE_ROLES]
            if role not in allowed_roles:
                raise ValidationError('You are not allowed to assign this role.')
        return role

    def clean(self):
        """
        Confirm the editor may act on this user.

        Two things were wrong with the previous version:

        1. It compared agencies. With several country offices per agency that
           is too wide — a Gambia focal point passed the check while editing a
           Senegal account. The view blocks it now, and this should agree with
           the view rather than contradict it.

        2. It had no superuser bypass, so a superuser editing anyone outside
           their own agency was refused with "You can only edit users in your
           own agency." A superuser has no agency in the sense this test meant.
        """
        cleaned_data = super().clean()

        if not self.instance.pk or not self.request_user:
            return cleaned_data

        if getattr(self.request_user, 'is_superuser', False):
            return cleaned_data

        try:
            from tenancy.services import can_manage_user
        except ImportError:
            # No tenancy app — fall back to the old agency comparison.
            if self.instance.agency_id != self.request_user.agency_id:
                raise ValidationError('You can only edit users in your own agency.')
            return cleaned_data

        if not can_manage_user(self.request_user, self.instance):
            raise ValidationError('You can only edit users in your own country office.')

        # Do not let someone move a user into an office they cannot assign.
        chosen = cleaned_data.get('country_office')
        if chosen is not None:
            allowed = set(self.fields['country_office'].queryset.values_list('pk', flat=True))
            if chosen.pk not in allowed:
                raise ValidationError('You cannot move a user to that country office.')

        return cleaned_data


class CustomUserRegistrationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    phone = forms.CharField(required=False)

    class Meta:
        model = User
        fields = ["username", "email", "phone", "password1", "password2"]


class RegistrationInviteForm(forms.ModelForm):
    class Meta:
        model = RegistrationInvite
        fields = ["max_uses", "valid_for_hours"]
        widgets = {
            "max_uses": forms.NumberInput(attrs={"min": 1}),
            "valid_for_hours": forms.NumberInput(attrs={"min": 1, "max": 23}),
        }

    def clean_valid_for_hours(self):
        value = self.cleaned_data.get("valid_for_hours") or 12
        if value <= 0:
            raise forms.ValidationError("Validity must be at least 1 hour.")
        if value >= 24:
            raise forms.ValidationError("Validity must be less than 24 hours (max 23).")
        return value


class RoomBookingForm(forms.ModelForm):
    FREQUENCY_CHOICES = (
        ("", "Does not repeat"),
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
        ("yearly", "Yearly"),
    )

    is_recurring = forms.BooleanField(required=False, widget=forms.HiddenInput())
    frequency = forms.ChoiceField(choices=FREQUENCY_CHOICES, required=False,
                                  widget=forms.Select(attrs={"class": "form-select"}))
    interval = forms.IntegerField(required=False, min_value=1, initial=1,
                                  widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}))
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    weekdays = forms.MultipleChoiceField(required=False,
                                         choices=[(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"),
                                                  (5, "Sat"), (6, "Sun")], widget=forms.CheckboxSelectMultiple)

    MONTHLY_TYPE_CHOICES = (("day", "Same day of month"), ("weekday", "Specific weekday of month"))
    monthly_type = forms.ChoiceField(choices=MONTHLY_TYPE_CHOICES, required=False, initial="day",
                                     widget=forms.RadioSelect)

    WEEK_POSITION_CHOICES = ((1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (-1, "Last"))
    monthly_week = forms.TypedChoiceField(choices=WEEK_POSITION_CHOICES, coerce=int, required=False,
                                          widget=forms.RadioSelect)

    WEEKDAY_CHOICES = ((0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun"))
    monthly_weekday = forms.TypedChoiceField(choices=WEEKDAY_CHOICES, coerce=int, required=False,
                                             widget=forms.RadioSelect)

    ICT_SUPPORT_CHOICES = (
        ("none", "No ICT support needed"),
        ("setup", "Before meeting — Setup / AV configuration"),
        ("during", "During meeting — Live technical support"),
    )
    ict_support = forms.ChoiceField(choices=ICT_SUPPORT_CHOICES, required=False, initial="none",
                                    widget=forms.RadioSelect(attrs={"class": "form-check-input"}), label="ICT Support")

    selected_amenities = forms.ModelMultipleChoiceField(queryset=RoomAmenity.objects.none(),
                                                        widget=forms.CheckboxSelectMultiple, required=False,
                                                        label="Optional Amenities")

    requested_amenities = forms.ModelMultipleChoiceField(
        queryset=RoomAmenity.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Request Optional Amenities"
    )
    agenda_document = forms.FileField(required=False, label="Upload Agenda (PDF, DOCX, etc.)")

    attendee_emails = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'e.g., colleague1@example.com, colleague2@example.com'}),
        required=False,
        label="Invite Guests (optional)",
        help_text="Enter comma-separated email addresses. Each will receive a calendar invite."
    )
    virtual_meeting_link = forms.URLField(
        widget=forms.URLInput(attrs={'placeholder': 'https://teams.microsoft.com/...'}),
        required=False,
        label="Virtual Meeting Link (optional)"
    )

    class Meta:
        model = RoomBooking
        fields = [
            "room", "title", "description", "agenda_document", "date", "start_time", "end_time",
            "requested_amenities", "attendee_emails", "virtual_meeting_link", "enable_attendance",
            "enable_invite_link"
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'start_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'end_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        room = kwargs.pop('room', None)
        super().__init__(*args, **kwargs)
        selected_room = room or (self.instance.room if self.instance and self.instance.pk else None)
        if selected_room:
            self.fields['requested_amenities'].queryset = selected_room.amenities.filter(is_active=True)
            self.fields['enable_attendance'].widget = forms.CheckboxInput(attrs={'id': 'id_enable_attendance'})
            self.fields['enable_invite_link'].widget = forms.CheckboxInput(attrs={'id': 'id_enable_invite_link'})
        else:
            self.fields['requested_amenities'].queryset = RoomAmenity.objects.none()

    def clean(self):
        cleaned = super().clean()
        is_recurring = cleaned.get("is_recurring")
        if not is_recurring:
            return cleaned

        frequency = cleaned.get("frequency")
        until = cleaned.get("until")
        interval = cleaned.get("interval")

        if not frequency:
            raise ValidationError("Please select a repeat frequency.")
        if not interval:
            raise ValidationError("Please specify the repeat interval (e.g. every 1 week).")
        if not until:
            raise ValidationError("Please specify an end date for the recurring booking.")
        if until and cleaned.get("date") and until < cleaned.get("date"):
            raise ValidationError("End date cannot be before the start date.")

        if frequency == "monthly":
            monthly_type = cleaned.get("monthly_type") or "day"
            if monthly_type == "weekday":
                monthly_week = cleaned.get("monthly_week")
                monthly_weekday = cleaned.get("monthly_weekday")
                if monthly_week is None or monthly_week == "":
                    raise ValidationError(
                        "Please select which occurrence (1st, 2nd, 3rd, 4th, or last) for the monthly recurrence."
                    )
                if monthly_weekday is None or monthly_weekday == "":
                    raise ValidationError(
                        "Please select which day of the week for the monthly recurrence."
                    )
        return cleaned


class RoomBookingApprovalForm(forms.ModelForm):
    """
    Form for an approver to:
    1. Confirm which amenities (from those requested) are actually available.
    2. Provide a rejection reason if declining.

    FIX: approved_amenities queryset is populated from the booking's
         requested_amenities; initial is pre-ticked with all requested ones
         so the approver can uncheck any unavailable items.
    """

    approved_amenities = forms.ModelMultipleChoiceField(
        queryset=RoomAmenity.objects.none(),   # set in __init__
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Confirm Available Amenities",
        help_text="Uncheck any amenities that are NOT available for this booking."
    )

    rejection_reason = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control',
                                     'placeholder': 'Provide a reason for rejection...'}),
        required=False,
        label="Reason for Rejection (required if rejecting)"
    )

    class Meta:
        model = RoomBooking
        # Only expose read-only booking details + the two decision fields.
        # We intentionally exclude editable booking fields so approvers
        # cannot accidentally change the room/date/time.
        fields = [
            "approved_amenities",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        booking = self.instance  # always passed via instance=booking in the view

        if booking and booking.pk:
            # Populate the amenity queryset with what the requester asked for.
            requested_qs = booking.requested_amenities.filter(is_active=True)
            self.fields['approved_amenities'].queryset = requested_qs

            # Pre-tick all requested amenities — approver unchecks unavailable ones.
            self.fields['approved_amenities'].initial = requested_qs

    def clean_rejection_reason(self):
        """
        Only validate the rejection_reason in the view (it checks the action button),
        but strip whitespace here for convenience.
        """
        return (self.cleaned_data.get('rejection_reason') or '').strip()


class RoomSeriesApprovalForm(forms.Form):
    """
    Form for approving/rejecting an entire recurring booking series.
    """
    ACTION_CHOICES = (
        ("approve", "Approve entire series"),
        ("reject", "Reject entire series"),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.RadioSelect)
    reason = forms.CharField(
        label="Reason (optional for approval, required for rejection)",
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        required=False,
        help_text="Provide a reason for rejection. This will be sent to the requester."
    )


class MeetingAttendeeForm(forms.ModelForm):
    """
    Form for external attendees to register for a meeting via the public link.
    """
    class Meta:
        model = MeetingAttendee
        fields = ['name', 'email', 'organization']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Your Full Name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Your Email Address'}),
            'organization': forms.TextInput(
                attrs={'class': 'form-control', 'placeholder': 'Your Organization (Optional)'}),
        }


class RoomForm(forms.ModelForm):
    """
    Professional Room create/update form.
    """
    amenities = forms.ModelMultipleChoiceField(
        queryset=RoomAmenity.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Select all amenities available in this room",
    )

    approvers = forms.ModelMultipleChoiceField(
        queryset=User.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Select users who can approve bookings for this room",
    )

    class Meta:
        model = Room
        fields = [
            "name", "code", "room_type", "location", "capacity", "description",
            "approval_mode", "is_active", "amenities", "approvers",
            # Who can see and book it. The views narrow owner_office to the
            # offices the editor may assign; see accounts/room_access.py.
            "owner_office", "visibility", "shared_note",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Conference Room A"}),
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. CR-A, LIB-1"}),
            "room_type": forms.Select(attrs={"class": "form-select"}),
            "location": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. UN House 1st Floor"}),
            "capacity": forms.NumberInput(attrs={"class": "form-control", "placeholder": "Number of people", "min": 1}),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 4, "placeholder": "Describe the room and its purpose"}),
            "approval_mode": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "owner_office": forms.Select(attrs={"class": "form-select"}),
            "visibility": forms.Select(attrs={"class": "form-select"}),
            "shared_note": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Anything another agency needs to know before booking",
            }),
        }

    def __init__(self, *args, **kwargs):
        # The room views pass request_user so the office list can be narrowed.
        # Accepted optionally, so any existing caller that does not pass it
        # keeps working.
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        if "owner_office" in self.fields:
            self.fields["owner_office"].required = False
            self.fields["owner_office"].label = "Owning country office"
            self.fields["owner_office"].empty_label = "— no owner (visible to everyone) —"
            self.fields["owner_office"].help_text = (
                "The office responsible for the room. Leave blank to keep it "
                "visible to everyone, as before."
            )
        if "visibility" in self.fields:
            self.fields["visibility"].required = False
            self.fields["visibility"].help_text = (
                "Who may see this room in their list and book it."
            )
            # If Room.visibility was added without `choices=`, Django builds a
            # plain CharField and the Select renders with no options at all.
            # Put the choices on regardless, from the single definition in
            # room_access so the two can never disagree.
            try:
                from .room_access import VISIBILITY_CHOICES
            except ImportError:
                VISIBILITY_CHOICES = (
                    ("office", "This country office only"),
                    ("agency", "All offices of this agency"),
                    ("country", "All agencies in this country (shared compound)"),
                )
            if not getattr(self.fields["visibility"], "choices", None):
                self.fields["visibility"] = forms.ChoiceField(
                    choices=VISIBILITY_CHOICES,
                    required=False,
                    label="Visibility",
                    initial=getattr(self.instance, "visibility", None) or "office",
                    widget=forms.Select(attrs={"class": "form-select"}),
                )
        if "shared_note" in self.fields:
            self.fields["shared_note"].required = False
            self.fields["shared_note"].label = "Note for other agencies"

        if self.instance.pk:
            self.fields["amenities"].initial = self.instance.amenities.filter(is_active=True)
            linked_users = User.objects.filter(
                room_approver_roles__room=self.instance,
                room_approver_roles__is_active=True,
            ).distinct()
            if linked_users.exists():
                self.fields["approvers"].initial = linked_users
            else:
                self.fields["approvers"].initial = self.instance.approvers.filter(is_active=True)

    def clean_code(self):
        code = self.cleaned_data.get("code")
        if code:
            code = code.strip().upper()
            qs = Room.objects.filter(code=code)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("A room with this code already exists.")
        return code

    def clean_name(self):
        name = self.cleaned_data.get("name")
        if name:
            name = name.strip()
            qs = Room.objects.filter(name=name)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("A room with this name already exists.")
        return name

    def save(self, commit=True):
        room = super().save(commit=commit)

        # `self.save_m2m()` used to be called here when commit=True. Django
        # only defines save_m2m on the *commit=False* path — when commit=True
        # it has already run _save_m2m() itself — so that line raised
        #
        #     AttributeError: 'RoomForm' object has no attribute 'save_m2m'
        #
        # on every save, before any of the field values below were written.
        # That is why nothing on this form appeared to post.
        if not commit:
            # Nothing further can be done until the caller saves the instance;
            # amenities and approvers need a primary key to attach to.
            return room

        selected_amenities = self.cleaned_data.get("amenities")
        selected_approvers = self.cleaned_data.get("approvers")

        if selected_amenities is not None:
            room.amenities.set(selected_amenities)

        if selected_approvers is not None:
            room.approvers.set(selected_approvers)
            selected_ids = set(selected_approvers.values_list("id", flat=True))
            RoomApprover.objects.filter(room=room).exclude(user_id__in=selected_ids).update(is_active=False)
            existing = set(
                RoomApprover.objects.filter(room=room, user_id__in=selected_ids).values_list("user_id", flat=True))
            to_create = [RoomApprover(room=room, user_id=uid, is_active=True) for uid in (selected_ids - existing)]
            if to_create:
                RoomApprover.objects.bulk_create(to_create)
            RoomApprover.objects.filter(room=room, user_id__in=selected_ids).update(is_active=True)

        return room