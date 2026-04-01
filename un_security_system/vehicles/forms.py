from django import forms
from django.utils import timezone
from django.forms import inlineformset_factory
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column, HTML
from django.contrib.auth import get_user_model

from .models import (
    Vehicle, VehicleMovement, ParkingCard, AssetExit,
    AssetExitItem, ParkingCardRequest, Key, KeyLog, Package,
    PackageFlowTemplate, PackageFlowStep
)
from accounts.models import Agency

User = get_user_model()
# ---------------------------------------------------------
# VEHICLE REGISTRATION FORM
# ---------------------------------------------------------

class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = [
            'plate_number', 'vehicle_type', 'make', 'model',
            'color', 'un_agency', 'parking_card'
        ]
        widgets = {
            'plate_number': forms.TextInput(
                attrs={
                    'placeholder': 'e.g., ABC-123',
                    'style': 'text-transform: uppercase;'
                }
            ),
            'color': forms.TextInput(attrs={'placeholder': 'e.g., White, Blue, etc.'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_method = 'post'

        self.helper.layout = Layout(
            Fieldset(
                'Vehicle Information',
                Row(
                    Column('plate_number', css_class='col-md-6'),
                    Column('vehicle_type', css_class='col-md-6'),
                ),
                Row(
                    Column('make', css_class='col-md-4'),
                    Column('model', css_class='col-md-4'),
                    Column('color', css_class='col-md-4'),
                ),
                'un_agency',
                'parking_card',
            ),
            Submit('submit', 'Register Vehicle', css_class='btn btn-primary')
        )

        # Filter active parking cards only
        self.fields['parking_card'].queryset = ParkingCard.objects.filter(is_active=True)

        # Optional field
        self.fields['un_agency'].required = False

    def clean_plate_number(self):
        p = self.cleaned_data.get('plate_number')
        return p.upper().strip() if p else p

    def clean(self):
        cleaned = super().clean()
        vtype = cleaned.get('vehicle_type')
        un_agency = cleaned.get('un_agency')
        card = cleaned.get('parking_card')

        if vtype == 'un_agency' and not un_agency:
            self.add_error('un_agency', "UN Agency is required for UN vehicles.")

        if vtype == 'staff' and not card:
            self.add_error('parking_card', "Staff vehicles require a parking card.")

        return cleaned


# ---------------------------------------------------------
# PARKING CARD FORM
# ---------------------------------------------------------

class ParkingCardForm(forms.ModelForm):
    class Meta:
        model = ParkingCard
        fields = [
            'card_number', 'owner_name', 'owner_id', 'phone', 'department',
            'vehicle_make', 'vehicle_model', 'vehicle_plate', 'vehicle_color',
            'expiry_date'
        ]
        widgets = {
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
            'vehicle_plate': forms.TextInput(attrs={'style': 'text-transform: uppercase;'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        tomorrow = timezone.now().date() + timezone.timedelta(days=1)
        self.fields['expiry_date'].widget.attrs['min'] = tomorrow.isoformat()

        self.helper = FormHelper()
        self.helper.form_method = 'post'

        self.helper.layout = Layout(
            Fieldset(
                'Card Information',
                Row(
                    Column('card_number', css_class='col-md-6'),
                    Column('expiry_date', css_class='col-md-6'),
                ),
            ),
            Fieldset(
                'Owner Information',
                Row(
                    Column('owner_name', css_class='col-md-6'),
                    Column('owner_id', css_class='col-md-6'),
                ),
                Row(
                    Column('phone', css_class='col-md-6'),
                    Column('department', css_class='col-md-6'),
                ),
            ),
            Fieldset(
                'Vehicle Information',
                Row(
                    Column('vehicle_make', css_class='col-md-6'),
                    Column('vehicle_model', css_class='col-md-6'),
                ),
                Row(
                    Column('vehicle_plate', css_class='col-md-6'),
                    Column('vehicle_color', css_class='col-md-6'),
                ),
            ),
            Submit('submit', 'Create Parking Card', css_class='btn btn-primary')
        )

    def clean_expiry_date(self):
        d = self.cleaned_data.get('expiry_date')
        if d and d <= timezone.now().date():
            raise forms.ValidationError("Expiry date must be in the future.")
        return d

    def clean_vehicle_plate(self):
        p = self.cleaned_data.get('vehicle_plate')
        return p.upper().strip() if p else p


# ---------------------------------------------------------
# VEHICLE MOVEMENT FORM
# ---------------------------------------------------------

class VehicleMovementForm(forms.ModelForm):
    plate_number = forms.CharField(
        max_length=20,
        label="Vehicle Plate Number",
        widget=forms.TextInput(attrs={
            'placeholder': 'Enter plate number',
            'class': 'form-control form-control-lg',
            'style': 'text-transform: uppercase;',
        })
    )

    class Meta:
        model = VehicleMovement
        fields = ['plate_number', 'movement_type', 'gate',
                  'driver_name', 'purpose', 'notes']
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_method = 'post'

        self.helper.layout = Layout(
            'plate_number',
            Row(
                Column('movement_type', css_class='col-md-6'),
                Column('gate', css_class='col-md-6'),
            ),
            'driver_name',
            'purpose',
            'notes',
            Submit('submit', 'Record Movement', css_class='btn btn-success btn-lg')
        )

    def clean_plate_number(self):
        plate = self.cleaned_data.get('plate_number', '').upper().strip()
        self.cleaned_data['plate_number'] = plate
        return plate

    def save(self, commit=True):
        instance = super().save(commit=False)
        plate_number = self.cleaned_data['plate_number']

        vehicle, created = Vehicle.objects.get_or_create(
            plate_number=plate_number,
            defaults={
                'vehicle_type': 'visitor',
                'make': 'Unknown',
                'model': 'Unknown',
                'color': 'Unknown',
            }
        )
        instance.vehicle = vehicle

        if commit:
            instance.save()

        return instance


# ---------------------------------------------------------
# QUICK VEHICLE CHECK
# ---------------------------------------------------------

class QuickVehicleCheckForm(forms.Form):
    card_number = forms.CharField(
        max_length=20,
        label="Parking Card Number",
        widget=forms.TextInput(attrs={
            'placeholder': 'Scan or enter parking card number',
            'class': 'form-control form-control-lg'
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_method = 'get'
        self.helper.layout = Layout(
            'card_number',
            Submit('check', 'Validate Card', css_class='btn btn-primary btn-lg')
        )


# ---------------------------------------------------------
# ASSET EXIT
# ---------------------------------------------------------

class AssetExitForm(forms.ModelForm):
    class Meta:
        model = AssetExit
        fields = [
            'agency_name', 'reason', 'destination',
            'expected_date', 'escort_required', 'notes'
        ]
        widgets = {
            'expected_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # expected_date is optional
        self.fields['expected_date'].required = False

        self.helper = FormHelper()
        self.helper.form_method = 'post'

        self.helper.layout = Layout(
            Fieldset(
                "Request Details",
                'agency_name',
                'reason',
                Row(
                    Column('destination', css_class='col-md-6'),
                    Column('expected_date', css_class='col-md-6'),
                ),
                'escort_required',
                'notes',
            ),
            Submit('submit', 'Submit Request', css_class='btn btn-primary')
        )


# ---------------------------------------------------------
# ASSET EXIT ITEMS — FORMSET
# ---------------------------------------------------------

class AssetExitItemForm(forms.ModelForm):
    class Meta:
        model = AssetExitItem
        fields = ['description', 'category', 'quantity', 'serial_or_tag']


AssetExitItemFormSet = inlineformset_factory(
    AssetExit,
    AssetExitItem,
    form=AssetExitItemForm,
    extra=2,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


# ---------------------------------------------------------
# PARKING CARD REQUEST
# ---------------------------------------------------------

class ParkingCardRequestForm(forms.ModelForm):
    class Meta:
        model = ParkingCardRequest
        fields = [
            'owner_name', 'owner_id', 'phone', 'department',
            'vehicle_make', 'vehicle_model', 'vehicle_plate',
            'vehicle_color', 'requested_expiry'
        ]
        widgets = {
            'requested_expiry': forms.DateInput(attrs={'type': 'date'}),
            'vehicle_plate': forms.TextInput(attrs={'style': 'text-transform: uppercase;'}),
        }

    def clean_vehicle_plate(self):
        p = self.cleaned_data.get('vehicle_plate')
        return p.upper().strip() if p else p

    def clean_requested_expiry(self):
        d = self.cleaned_data.get('requested_expiry')
        if d and d <= timezone.now().date():
            raise forms.ValidationError("Expiry must be in the future.")
        return d


# ---------------------------------------------------------
# KEY MANAGEMENT
# ---------------------------------------------------------

class KeyForm(forms.ModelForm):
    class Meta:
        model = Key
        fields = ['code', 'label', 'key_type', 'vehicle', 'location', 'is_active', 'notes']


class KeyIssueForm(forms.ModelForm):
    class Meta:
        model = KeyLog
        fields = [
            'issued_to_name', 'issued_to_agency',
            'issued_to_badge_id', 'purpose', 'due_back',
            'condition_out'
        ]
        widgets = {
            'due_back': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }


class KeyReturnForm(forms.ModelForm):
    class Meta:
        model = KeyLog
        fields = ['condition_in']


# ---------------------------------------------------------
# PACKAGE LOGGING
# ---------------------------------------------------------

# ── Bootstrap helper ──────────────────────────────────────────────────────────
def _bc(extra=''):
    return f'form-control {extra}'.strip()


# ── Agency-aware user queryset helper ─────────────────────────────────────────

def _agency_users(agency):
    """Return users belonging to `agency`, ordered by full name / username."""
    if agency is None:
        return User.objects.none()
    return User.objects.filter(agency=agency, is_active=True).order_by('first_name', 'last_name', 'username')


# ── Package log form ──────────────────────────────────────────────
class PackageLogForm(forms.ModelForm):
    class Meta:
        model = Package
        fields = [
            'sender_name', 'sender_type', 'sender_org', 'sender_contact','sender_email',
            'item_type', 'description',
            'destination_agency', 'dest_focal_email', 'for_recipient',
            'notes',
        ]
        widgets = {
            'sender_name': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Full name or organisation'}),
            'sender_type': forms.Select(attrs={'class': 'form-select'}),
            'sender_org': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Organisation (if applicable)'}),
            'sender_contact': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Phone or email'}),
            'sender_email': forms.EmailInput(attrs={
                          'class': 'form-control',
                          'placeholder': 'sender@example.com (for delivery confirmation)',
                      }),
            'item_type': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Package / Envelope / Box …'}),
            'description': forms.Textarea(attrs={'class': _bc(), 'rows': 3}),
            'destination_agency': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'e.g. UNDP, WHO, UNICEF'}),
            'dest_focal_email': forms.EmailInput(attrs={'class': _bc(), 'placeholder': 'focal@agency.un.org'}),
            'for_recipient': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Recipient name (if known)'}),
            'notes': forms.Textarea(attrs={'class': _bc(), 'rows': 2}),
        }


class PackageOutgoingLogForm(forms.ModelForm):
    """
    Used when an agency staff member registers an OUTGOING package/mail.

    Field mapping for outgoing direction:
      sender_name    → internal originator's name  (auto-filled from request.user)
      sender_org     → originator's unit / project
      sender_contact → originator's phone
      sender_email   → originator's email (for status updates back to them)
      sender_type    → always 'individual' for outgoing (hidden, set in view)

      for_recipient      → external recipient name
      recipient_org      → external recipient organisation
      recipient_address  → delivery address
      recipient_email    → external recipient email (delivery confirmation)
      destination_agency → leave as the external agency/organisation name
      dest_focal_email   → leave blank or use recipient_email (handled in view)
    """

    class Meta:
        model = Package
        fields = [
            # Originator (internal sender)
            'sender_name', 'sender_org', 'sender_contact', 'sender_email',
            # Item
            'item_type', 'description',
            # External recipient
            'for_recipient', 'recipient_org', 'recipient_address', 'recipient_email',
            # Routing label
            'destination_agency',
            # Notes
            'notes',
        ]
        labels = {
            'sender_name': 'Your Name / Originator',
            'sender_org': 'Unit / Project',
            'sender_contact': 'Your Phone',
            'sender_email': 'Your Email (status updates)',
            'for_recipient': 'Recipient Name',
            'destination_agency': 'Recipient Organisation / Agency',
            'recipient_org': 'Recipient Department',
            'recipient_address': 'Delivery Address',
            'recipient_email': 'Recipient Email (delivery confirmation)',
        }
        widgets = {
            'sender_name': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Your full name'}),
            'sender_org': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'e.g. Finance Unit, Project X'}),
            'sender_contact': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Phone number'}),
            'sender_email': forms.EmailInput(attrs={'class': _bc(), 'placeholder': 'you@agency.un.org'}),
            'item_type': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Letter / Package / Parcel / Box …'}),
            'description': forms.Textarea(
                attrs={'class': _bc(), 'rows': 3, 'placeholder': 'Brief description of contents'}),
            'for_recipient': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Recipient full name'}),
            'destination_agency': forms.TextInput(
                attrs={'class': _bc(), 'placeholder': 'e.g. Ministry of Finance, UNDP Kenya'}),
            'recipient_org': forms.TextInput(
                attrs={'class': _bc(), 'placeholder': 'Department or office within organisation'}),
            'recipient_address': forms.Textarea(
                attrs={'class': _bc(), 'rows': 2, 'placeholder': 'Full postal / delivery address'}),
            'recipient_email': forms.EmailInput(attrs={'class': _bc(), 'placeholder': 'recipient@example.com'}),
            'notes': forms.Textarea(
                attrs={'class': _bc(), 'rows': 2, 'placeholder': 'Special handling instructions, urgency, etc.'}),
        }

# ── Flow template form ────────────────────────────────────────────────────────

_DIRECTION_CHOICES = [
    ('incoming', 'Incoming Mail / Package'),
    ('outgoing', 'Outgoing Mail / Package'),
]

class PackageFlowTemplateForm(forms.ModelForm):
    """
    ICT focal points create/edit templates for THEIR OWN agency only.
    The `agency` field is hidden and set automatically in the view.
    """

    class Meta:
        model = PackageFlowTemplate
        fields = ['name', 'description', 'direction', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'e.g. Outgoing Courier Flow'}),
            'description': forms.Textarea(attrs={'class': _bc(), 'rows': 3}),
            'direction': forms.Select(
                choices=_DIRECTION_CHOICES,
                attrs={'class': 'form-select'}
            ),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# ── Flow step form ────────────────────────────────────────────────────────────

class PackageFlowStepForm(forms.ModelForm):
    """
    Must be instantiated with `agency=<Agency>` so the M2M user pickers
    are filtered to that agency only.

        form = PackageFlowStepForm(request.POST or None, agency=tmpl.agency)
    """

    def __init__(self, *args, agency=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._agency = agency
        qs = _agency_users(agency)

        # Scope both M2M pickers to the agency
        self.fields['allowed_users'].queryset = qs
        self.fields['notify_next_users'].queryset = qs

        # Human-readable labels for the checkboxes in the widget
        self.fields['allowed_users'].label = 'Allowed Users (this agency)'
        self.fields['notify_next_users'].label = 'Notify Specific Users (this agency)'

    class Meta:
        model = PackageFlowStep
        fields = [
            'order', 'name', 'step_type', 'status_code', 'description',
            # Access
            'allowed_roles', 'allowed_users',
            # Required actions
            'requires_note', 'requires_scan', 'requires_stamp',
            'requires_routing', 'requires_recipient_signature',
            # Notifications
            'notify_requester', 'notify_focal_email', 'notify_recipient',
            'notify_next_handler_roles', 'notify_next_users',
            # Flow control
            'is_terminal',
        ]
        widgets = {
            'order': forms.NumberInput(attrs={'class': _bc(), 'min': 1}),
            'name': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'e.g. Reception Check'}),
            'step_type': forms.Select(attrs={'class': 'form-select'}),
            'status_code': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'e.g. at_reception'}),
            'description': forms.TextInput(attrs={'class': _bc(), 'placeholder': 'Short description shown to handler'}),

            # Role text fields
            'allowed_roles': forms.TextInput(attrs={
                'class': _bc(),
                'placeholder': 'e.g. reception,registry  (leave blank to rely on allowed_users)'
            }),
            'notify_next_handler_roles': forms.TextInput(attrs={
                'class': _bc(),
                'placeholder': 'e.g. registry,agency_fp'
            }),

            # M2M user pickers — multi-select listbox
            'allowed_users': forms.SelectMultiple(attrs={
                'class': 'form-select',
                'size': 6,
            }),
            'notify_next_users': forms.SelectMultiple(attrs={
                'class': 'form-select',
                'size': 6,
            }),

            # Checkboxes
            'requires_note': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'requires_scan': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'requires_stamp': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'requires_routing': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'requires_recipient_signature': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_requester': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_focal_email': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_recipient': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_terminal': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def clean_status_code(self):
        code = self.cleaned_data.get('status_code', '').strip()
        if ' ' in code:
            raise forms.ValidationError("Status code must be a slug — no spaces.")
        return code

    def clean(self):
        cd = super().clean()
        roles = cd.get('allowed_roles', '').strip()
        users = cd.get('allowed_users')
        # Warn (not error) if nothing is set — any agency member will be able to act
        return cd


# ── Dynamic step action form  ────────────────────────────────

class PackageStepActionForm(forms.Form):
    """
    Dynamically-built form whose fields depend on what a PackageFlowStep requires.
    Pass `step=<PackageFlowStep>` when constructing.
    """

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, **kwargs)

        if step is None:
            return

        self.fields['note'] = forms.CharField(
            label='Note / Remarks',
            widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                         'placeholder': 'Enter any relevant observations …'}),
            required=step.requires_note,
            help_text='Required for this step.' if step.requires_note else 'Optional.',
        )

        if step.requires_scan:
            self.fields['scan_file'] = forms.FileField(
                label='Upload Scan / Photo',
                required=True,
                help_text='Upload a scan or clear photograph of the item or its contents.',
                widget=forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/*,.pdf'}),
            )

        if step.requires_stamp:
            self.fields['stamped'] = forms.BooleanField(
                label='I confirm this item has been stamped / signed',
                required=True,
                widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            )

        if step.requires_routing:
            self.fields['routed_to'] = forms.CharField(
                label='Route To (Unit / Project)',
                max_length=200,
                required=True,
                widget=forms.TextInput(attrs={'class': 'form-control',
                                              'placeholder': 'e.g. Finance Unit, Project XYZ'}),
            )

        if step.requires_recipient_signature:
            self.fields['recipient_name'] = forms.CharField(
                label='Recipient Name',
                max_length=120,
                required=True,
                widget=forms.TextInput(attrs={'class': 'form-control',
                                              'placeholder': 'Full name of person receiving and signing'}),
            )