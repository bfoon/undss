from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.core.exceptions import ValidationError

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column

from .models import (
    SecurityIncident, RegistrationInvite, RoomBooking, Room, RoomAmenity,
    RoomApprover,
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
    # Usually don't allow ICT to create LSA/SOC/ICT Focal
]


class ICTUserCreateForm(forms.ModelForm):
    """Form for ICT focal points to create users in their agency."""

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone', 'employee_id', 'role']
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter username'
            }),
            'first_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter first name'
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter last name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'user@example.com'
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '+1234567890'
            }),
            'employee_id': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter employee ID'
            }),
            'role': forms.Select(attrs={
                'class': 'form-select'
            }),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)

        # Restrict role choices to ICT-assignable roles only
        self.fields['role'].choices = [('', '---------')] + ICT_ASSIGNABLE_ROLES

        # Make certain fields required
        self.fields['username'].required = True
        self.fields['role'].required = True

        # Add help text
        self.fields['username'].help_text = 'Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.'
        self.fields['email'].help_text = 'Optional. Used for password reset links.'
        self.fields['employee_id'].help_text = 'Optional. Internal employee identifier.'
        self.fields['role'].help_text = 'Select the role for this user within your agency.'

    def clean_username(self):
        """Validate that username is unique."""
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
        """Validate that employee_id is unique if provided."""
        employee_id = self.cleaned_data.get('employee_id')
        if employee_id:
            # Strip whitespace
            employee_id = employee_id.strip()
            if User.objects.filter(employee_id=employee_id).exists():
                raise ValidationError('A user with this employee ID already exists.')
        return employee_id

    def clean_role(self):
        """Validate that the role is one of the allowed roles for ICT."""
        role = self.cleaned_data.get('role')
        if role:
            allowed_roles = [r[0] for r in ICT_ASSIGNABLE_ROLES]
            if role not in allowed_roles:
                raise ValidationError('You are not allowed to assign this role.')
        return role

    def save(self, commit=True):
        """Save the user and assign to the ICT focal's agency."""
        user = super().save(commit=False)

        # Assign to the ICT focal's agency
        if self.request_user and self.request_user.agency_id:
            user.agency_id = self.request_user.agency_id

        # Set user as active but without a usable password initially
        user.is_active = True
        user.set_unusable_password()

        if commit:
            user.save()

        return user


class ICTUserUpdateForm(forms.ModelForm):
    """Form for ICT focal points to update users in their agency."""

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone', 'employee_id', 'role']
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter username'
            }),
            'first_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter first name'
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter last name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'user@example.com'
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '+1234567890'
            }),
            'employee_id': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter employee ID'
            }),
            'role': forms.Select(attrs={
                'class': 'form-select'
            }),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)

        # Restrict role choices to ICT-assignable roles only
        self.fields['role'].choices = [('', '---------')] + ICT_ASSIGNABLE_ROLES

        # Make certain fields required
        self.fields['username'].required = True
        self.fields['role'].required = True

        # Add help text
        self.fields['username'].help_text = 'Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.'
        self.fields['email'].help_text = 'Optional. Used for password reset links.'
        self.fields['employee_id'].help_text = 'Optional. Internal employee identifier.'
        self.fields['role'].help_text = 'Select the role for this user within your agency.'

    def clean_username(self):
        """Validate that username is unique (excluding current user)."""
        username = self.cleaned_data.get('username')
        if username and User.objects.filter(username=username).exclude(pk=self.instance.pk).exists():
            raise ValidationError('A user with this username already exists.')
        return username

    def clean_email(self):
        """Validate that email is unique if provided (excluding current user)."""
        email = self.cleaned_data.get('email')
        if email:
            # Strip whitespace
            email = email.strip()
            if User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
                raise ValidationError('A user with this email already exists.')
        return email

    def clean_employee_id(self):
        """Validate that employee_id is unique if provided (excluding current user)."""
        employee_id = self.cleaned_data.get('employee_id')
        if employee_id:
            # Strip whitespace
            employee_id = employee_id.strip()
            if User.objects.filter(employee_id=employee_id).exclude(pk=self.instance.pk).exists():
                raise ValidationError('A user with this employee ID already exists.')
        return employee_id

    def clean_role(self):
        """Validate that the role is one of the allowed roles for ICT."""
        role = self.cleaned_data.get('role')
        if role:
            allowed_roles = [r[0] for r in ICT_ASSIGNABLE_ROLES]
            if role not in allowed_roles:
                raise ValidationError('You are not allowed to assign this role.')
        return role

    def clean(self):
        """Additional validation to prevent ICT focal from changing agency."""
        cleaned_data = super().clean()

        # Ensure user stays in the same agency
        if self.instance.pk and self.request_user:
            if self.instance.agency_id != self.request_user.agency_id:
                raise ValidationError('You can only edit users in your own agency.')

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
            raise forms.ValidationError(
                "Validity must be less than 24 hours (max 23)."
            )
        return value

class RoomBookingForm(forms.ModelForm):

    FREQUENCY_CHOICES = (
        ("", "Does not repeat"),
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
        ("yearly", "Yearly"),
    )

    # Hidden flag set by JS when the recurring toggle is ON.
    # This is the authoritative server-side gate — never relies on
    # whether radio/interval inputs happened to be submitted.
    is_recurring = forms.BooleanField(
        required=False,
        widget=forms.HiddenInput(),
    )

    frequency = forms.ChoiceField(
        choices=FREQUENCY_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select"})
    )

    interval = forms.IntegerField(
        required=False,
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={
            "class": "form-control",
            "min": 1
        }),
        help_text="Repeat every N units (e.g. every 2 weeks)"
    )

    until = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        help_text="End date for recurrence"
    )

    weekdays = forms.MultipleChoiceField(
        required=False,
        choices=[
            (0, "Mon"), (1, "Tue"), (2, "Wed"),
            (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun"),
        ],
        widget=forms.CheckboxSelectMultiple,
        help_text="Select weekdays (for weekly recurrence)"
    )

    # ── Monthly recurrence: nth-weekday pattern ──────────────────────────
    # e.g. "last Thursday" = monthly_type=weekday, monthly_week=-1, monthly_weekday=3
    MONTHLY_TYPE_CHOICES = (
        ("day", "Same day of month"),
        ("weekday", "Specific weekday of month"),
    )
    monthly_type = forms.ChoiceField(
        choices=MONTHLY_TYPE_CHOICES,
        required=False,
        initial="day",
        widget=forms.RadioSelect,
        help_text="For monthly recurrence: repeat on the same date, or on a specific weekday?",
    )

    WEEK_POSITION_CHOICES = (
        (1,  "1st"),
        (2,  "2nd"),
        (3,  "3rd"),
        (4,  "4th"),
        (-1, "Last"),
    )
    monthly_week = forms.TypedChoiceField(
        choices=WEEK_POSITION_CHOICES,
        coerce=int,
        required=False,
        widget=forms.RadioSelect,
        help_text="Which occurrence within the month (1st, 2nd … last)",
    )

    WEEKDAY_CHOICES = (
        (0, "Mon"), (1, "Tue"), (2, "Wed"),
        (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun"),
    )
    monthly_weekday = forms.TypedChoiceField(
        choices=WEEKDAY_CHOICES,
        coerce=int,
        required=False,
        widget=forms.RadioSelect,
        help_text="Which day of the week (Mon–Sun)",
    )

    class Meta:
        model = RoomBooking
        fields = [
            "room", "title", "description",
            "date", "start_time", "end_time"
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "start_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
            "end_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
        }

    def clean(self):
        cleaned = super().clean()

        # Only validate recurring fields when the user explicitly
        # enabled the recurring toggle (is_recurring=True).
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

        # Validate monthly-weekday mode fields
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



class RoomBookingApprovalForm(forms.Form):
    ACTION_CHOICES = (
        ("approve", "Approve"),
        ("reject", "Reject"),
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.RadioSelect)
    reason = forms.CharField(
        label="Reason (optional for approval, required for rejection)",
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )

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


class RoomForm(forms.ModelForm):
    """
    Professional Room create/update form.

    - Allows selecting amenities (RoomAmenity)
    - Allows selecting approvers (Users)
    - Adds approval_mode (manual/auto/mixed) to control workflow
    - Keeps RoomApprover links in sync with selected approvers
      because your booking approval views use RoomApprover links.
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
            "name",
            "code",
            "room_type",
            "location",
            "capacity",
            "description",
            "approval_mode",  # ✅ add this field in Room model
            "is_active",
            "amenities",
            "approvers",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Conference Room A"}
            ),
            "code": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. CR-A, LIB-1"}
            ),
            "room_type": forms.Select(attrs={"class": "form-select"}),
            "location": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. UN House 1st Floor"}
            ),
            "capacity": forms.NumberInput(
                attrs={"class": "form-control", "placeholder": "Number of people", "min": 1}
            ),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 4, "placeholder": "Describe the room and its purpose"}
            ),
            "approval_mode": forms.Select(attrs={"class": "form-select"}),  # ✅
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # If editing, prefill selections
        if self.instance.pk:
            # Amenities already M2M on Room
            self.fields["amenities"].initial = self.instance.amenities.filter(is_active=True)

            # Approvers:
            # Prefer RoomApprover links (since your workflow uses them), fall back to Room.approvers.
            linked_users = User.objects.filter(
                room_approver_roles__room=self.instance,
                room_approver_roles__is_active=True,
            ).distinct()

            if linked_users.exists():
                self.fields["approvers"].initial = linked_users
            else:
                self.fields["approvers"].initial = self.instance.approvers.filter(is_active=True)

    def clean_code(self):
        """Validate that code is unique (excluding current instance if editing)."""
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
        """Validate that name is unique (excluding current instance if editing)."""
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
        """
        Save room + sync:
        - Room.amenities M2M
        - Room.approvers M2M (optional/legacy)
        - RoomApprover links (ACTIVE) to match selected approvers (this is what your workflow uses)
        """
        room = super().save(commit=commit)

        # M2M saving
        if commit:
            self.save_m2m()

        selected_amenities = self.cleaned_data.get("amenities")
        selected_approvers = self.cleaned_data.get("approvers")

        # Ensure amenities match selection (defensive)
        if selected_amenities is not None:
            room.amenities.set(selected_amenities)

        # Keep Room.approvers updated too (since you have this M2M on the model)
        if selected_approvers is not None:
            room.approvers.set(selected_approvers)

            # ---- Sync RoomApprover links (THIS is what your views use) ----
            selected_ids = set(selected_approvers.values_list("id", flat=True))

            # Deactivate links not selected
            RoomApprover.objects.filter(room=room).exclude(user_id__in=selected_ids).update(is_active=False)

            # Activate/create selected links
            existing = set(RoomApprover.objects.filter(room=room, user_id__in=selected_ids).values_list("user_id", flat=True))

            to_create = [RoomApprover(room=room, user_id=uid, is_active=True) for uid in (selected_ids - existing)]
            if to_create:
                RoomApprover.objects.bulk_create(to_create)

            RoomApprover.objects.filter(room=room, user_id__in=selected_ids).update(is_active=True)

        return room