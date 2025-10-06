from django import forms
from django.utils import timezone
from crispy_forms.helper import FormHelper
from django.forms import inlineformset_factory
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column, HTML
from .models import Vehicle, VehicleMovement, ParkingCard, AssetExit, AssetExitItem, ParkingCardRequest


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = ['plate_number', 'vehicle_type', 'make', 'model', 'color', 'un_agency', 'parking_card']
        widgets = {
            'plate_number': forms.TextInput(
                attrs={'placeholder': 'e.g., ABC-123', 'style': 'text-transform: uppercase;'}),
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
                    Column('plate_number', css_class='form-group col-md-6 mb-3'),
                    Column('vehicle_type', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('make', css_class='form-group col-md-4 mb-3'),
                    Column('model', css_class='form-group col-md-4 mb-3'),
                    Column('color', css_class='form-group col-md-4 mb-3'),
                ),
                'un_agency',
                'parking_card',
            ),
            Submit('submit', 'Register Vehicle', css_class='btn btn-primary')
        )

        # Filter active parking cards only
        self.fields['parking_card'].queryset = ParkingCard.objects.filter(is_active=True)

        # Make UN agency field conditional
        self.fields['un_agency'].required = False

    def clean_plate_number(self):
        plate_number = self.cleaned_data.get('plate_number')
        if plate_number:
            return plate_number.upper().strip()
        return plate_number

    def clean(self):
        cleaned_data = super().clean()
        vehicle_type = cleaned_data.get('vehicle_type')
        un_agency = cleaned_data.get('un_agency')
        parking_card = cleaned_data.get('parking_card')

        if vehicle_type == 'un_agency' and not un_agency:
            raise forms.ValidationError('UN Agency is required for UN agency vehicles.')

        if vehicle_type == 'staff' and not parking_card:
            raise forms.ValidationError('Parking card is required for staff vehicles.')

        return cleaned_data


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
            'card_number': forms.TextInput(attrs={'placeholder': 'e.g., PC-001'}),
            'owner_id': forms.TextInput(attrs={'placeholder': 'Employee ID or National ID'}),
            'vehicle_plate': forms.TextInput(attrs={'style': 'text-transform: uppercase;'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Fieldset(
                'Card Information',
                Row(
                    Column('card_number', css_class='form-group col-md-6 mb-3'),
                    Column('expiry_date', css_class='form-group col-md-6 mb-3'),
                ),
            ),
            Fieldset(
                'Owner Information',
                Row(
                    Column('owner_name', css_class='form-group col-md-6 mb-3'),
                    Column('owner_id', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('phone', css_class='form-group col-md-6 mb-3'),
                    Column('department', css_class='form-group col-md-6 mb-3'),
                ),
            ),
            Fieldset(
                'Vehicle Information',
                Row(
                    Column('vehicle_make', css_class='form-group col-md-6 mb-3'),
                    Column('vehicle_model', css_class='form-group col-md-6 mb-3'),
                ),
                Row(
                    Column('vehicle_plate', css_class='form-group col-md-6 mb-3'),
                    Column('vehicle_color', css_class='form-group col-md-6 mb-3'),
                ),
            ),
            Submit('submit', 'Create Parking Card', css_class='btn btn-primary')
        )

        # Set minimum expiry date to tomorrow
        tomorrow = timezone.now().date() + timezone.timedelta(days=1)
        self.fields['expiry_date'].widget.attrs['min'] = tomorrow.isoformat()

    def clean_expiry_date(self):
        expiry_date = self.cleaned_data.get('expiry_date')
        if expiry_date and expiry_date <= timezone.now().date():
            raise forms.ValidationError('Expiry date must be in the future.')
        return expiry_date

    def clean_vehicle_plate(self):
        vehicle_plate = self.cleaned_data.get('vehicle_plate')
        if vehicle_plate:
            return vehicle_plate.upper().strip()
        return vehicle_plate


class VehicleMovementForm(forms.ModelForm):
    plate_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': 'Enter plate number',
            'class': 'form-control form-control-lg',
            'style': 'text-transform: uppercase;'
        }),
        label='Vehicle Plate Number'
    )

    class Meta:
        model = VehicleMovement
        fields = ['plate_number', 'movement_type', 'gate', 'driver_name', 'purpose', 'notes']
        widgets = {
            'driver_name': forms.TextInput(attrs={'placeholder': 'Driver name (optional)'}),
            'purpose': forms.TextInput(attrs={'placeholder': 'Purpose of visit/trip (optional)'}),
            'notes': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Additional notes (optional)'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_class = 'vehicle-movement-form'
        self.helper.layout = Layout(
            'plate_number',
            Row(
                Column('movement_type', css_class='form-group col-md-6 mb-3'),
                Column('gate', css_class='form-group col-md-6 mb-3'),
            ),
            'driver_name',
            'purpose',
            'notes',
            Submit('submit', 'Record Movement', css_class='btn btn-success btn-lg')
        )

    def clean_plate_number(self):
        plate_number = self.cleaned_data.get('plate_number')
        if plate_number:
            plate_number = plate_number.upper().strip()

            # Try to find existing vehicle or create new one
            try:
                vehicle = Vehicle.objects.get(plate_number=plate_number)
                self.vehicle = vehicle
            except Vehicle.DoesNotExist:
                # For visitor vehicles, we'll create a temporary entry
                self.vehicle = None

            return plate_number
        return plate_number

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Get or create vehicle
        plate_number = self.cleaned_data['plate_number']
        vehicle, created = Vehicle.objects.get_or_create(
            plate_number=plate_number,
            defaults={
                'vehicle_type': 'visitor',
                'make': 'Unknown',
                'model': 'Unknown',
                'color': 'Unknown'
            }
        )

        instance.vehicle = vehicle

        if commit:
            instance.save()
        return instance


class QuickVehicleCheckForm(forms.Form):
    """Quick form for checking parking cards"""
    card_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': 'Scan or enter parking card number',
            'class': 'form-control form-control-lg'
        }),
        label='Parking Card Number'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'get'
        self.helper.layout = Layout(
            'card_number',
            Submit('check', 'Validate Card', css_class='btn btn-primary btn-lg')
        )

class AssetExitForm(forms.ModelForm):
    class Meta:
        model = AssetExit
        fields = [
            'agency_name', 'reason', 'destination', 'expected_date',
            'escort_required', 'notes'
        ]
        widgets = {
            'expected_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.layout = Layout(
            Fieldset('Request Details',
                'agency_name', 'reason',
                Row(
                    Column('destination', css_class='col-md-6'),
                    Column('expected_date', css_class='col-md-6'),
                ),
                Row(
                    Column('escort_required', css_class='col-md-4'),
                ),
                'notes',
            ),
        )

class AssetExitItemForm(forms.ModelForm):
    class Meta:
        model = AssetExitItem
        fields = ['description', 'category', 'quantity', 'serial_or_tag']

AssetExitItemFormSet = inlineformset_factory(
    parent_model=AssetExit,
    model=AssetExitItem,
    form=AssetExitItemForm,
    fields=['description','category','quantity','serial_or_tag'],
    extra=2,
    can_delete=True,
    min_num=1,
    validate_min=True,
)

class ParkingCardRequestForm(forms.ModelForm):
    class Meta:
        model = ParkingCardRequest
        fields = [
            'owner_name', 'owner_id', 'phone', 'department',
            'vehicle_make', 'vehicle_model', 'vehicle_plate', 'vehicle_color',
            'requested_expiry'
        ]
        widgets = {
            'requested_expiry': forms.DateInput(attrs={'type': 'date'}),
            'vehicle_plate': forms.TextInput(attrs={'style': 'text-transform: uppercase;'}),
        }

    def clean_vehicle_plate(self):
        p = self.cleaned_data.get('vehicle_plate', '')
        return p.upper().strip()

    def clean_requested_expiry(self):
        d = self.cleaned_data.get('requested_expiry')
        if d and d <= timezone.now().date():
            raise forms.ValidationError("Expiry must be a future date.")
        return d