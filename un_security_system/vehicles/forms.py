from django import forms
from django.utils import timezone
from django.forms import inlineformset_factory
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column, HTML
from django.contrib.auth import get_user_model

from .models import (
    Vehicle, VehicleMovement, ParkingCard, AssetExit,
    AssetExitItem, ParkingCardRequest, Key, KeyLog, Package
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


class PackageLogForm(forms.ModelForm):
    """
    Guard logs package at the gate.
    - item_type is rendered as a hard-coded select in the template
    - destination_agency, dest_focal_email, for_recipient are populated
      from Agency and User models but stored as plain strings on Package.
    """

    # Override model fields so we can provide dynamic choices
    destination_agency = forms.ChoiceField(
        label="Destination Agency",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select select2-agency"}),
    )

    dest_focal_email = forms.ChoiceField(
        label="Destination Focal Point",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select select2-user"}),
    )

    for_recipient = forms.ChoiceField(
        label="For (Recipient)",
        required=False,
        choices=[],
        widget=forms.Select(attrs={"class": "form-select select2-user"}),
    )

    class Meta:
        model = Package
        fields = [
            "sender_name", "sender_type", "sender_org", "sender_contact",
            "item_type", "description",
            "destination_agency", "dest_focal_email", "for_recipient",
            "notes",
        ]
        widgets = {
            # item_type will be overridden in the template (hard-coded list),
            # but we keep this as a default widget:
            "item_type": forms.Select(attrs={"class": "form-select"}),

            "description": forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
            "notes": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ---------- Agencies (for destination_agency) ----------
        agencies = Agency.objects.all().order_by("code", "name")
        agency_choices = [("", "Select destination agency…")]
        for a in agencies:
            label = f"{a.code} – {a.name}" if a.code else a.name
            # store agency code in the CharField (or adjust to name if you prefer)
            agency_choices.append((a.code, label))
        self.fields["destination_agency"].choices = agency_choices

        # ---------- Users (for focal point & recipient) ----------
        users = (
            User.objects.filter(is_active=True)
            .select_related("agency")
            .order_by("agency__code", "last_name", "first_name")
        )

        def user_label(u: User) -> str:
            full_name = (u.get_full_name() or "").strip()
            if not full_name:
                full_name = f"{u.first_name} {u.last_name}".strip() or u.username
            agency_code = getattr(getattr(u, "agency", None), "code", "") or ""
            if agency_code:
                return f"{agency_code} – {full_name} ({u.username})"
            return f"{full_name} ({u.username})"

        # Focal point: store email, show full name + agency
        focal_choices = [("", "Select destination focal point…")]
        for u in users:
            email_value = u.email or u.username  # in case some users have no email
            focal_choices.append((email_value, user_label(u)))
        self.fields["dest_focal_email"].choices = focal_choices

        # Recipient: store username, show full name + agency
        recipient_choices = [("", "Select package recipient…")]
        for u in users:
            recipient_choices.append((u.username, user_label(u)))
        self.fields["for_recipient"].choices = recipient_choices

class PackageReceptionForm(forms.Form):
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2})
    )


class PackageAgencyReceiveForm(forms.Form):
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2})
    )


class PackageDeliverForm(forms.Form):
    delivered_to = forms.CharField(max_length=120)
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2})
    )
