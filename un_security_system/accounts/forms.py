from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth import authenticate
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column
from .models import User, SecurityIncident, RegistrationInvite
from django.core.exceptions import ValidationError


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
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Username'
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
            'username',
            'password',
            Submit('submit', 'Login', css_class='btn btn-primary btn-lg w-100')
        )

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')

        if username and password:
            user = authenticate(self.request, username=username, password=password)
            if user is None:
                raise forms.ValidationError('Invalid username or password.')
            if not user.is_active:
                raise forms.ValidationError('This account is inactive.')

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


# accounts/forms.py (ICT-related forms to add to your existing forms.py)

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()

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
        """Validate that email is unique if provided."""
        email = self.cleaned_data.get('email')
        if email:
            # Strip whitespace
            email = email.strip()
            if User.objects.filter(email=email).exists():
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
