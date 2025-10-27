from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth import authenticate
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column
from .models import User, SecurityIncident


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

# Which roles ICT is allowed to assign (adjust as needed)
ICT_ASSIGNABLE_ROLES = [
    ('requester', 'Requester (Staff)'),
    ('reception', 'Receptionist'),
    ('registry', 'Registry'),
    ('data_entry', 'Data Entry (Security Guard)'),
    # Usually don’t allow ICT to create LSA/SOC
]

class ICTUserCreateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=ICT_ASSIGNABLE_ROLES)

    class Meta:
        model = User
        fields = [
            'username', 'first_name', 'last_name', 'email',
            'employee_id', 'phone', 'role'
        ]

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop('request_user', None)
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        user = super().save(commit=False)
        # Force the agency to ICT focal's agency
        if self.request_user and self.request_user.agency_id:
            user.agency_id = self.request_user.agency_id
        # You can set a temporary password or unusable password
        user.set_unusable_password()
        if commit:
            user.save()
        return user