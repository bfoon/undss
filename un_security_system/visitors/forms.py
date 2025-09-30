from django import forms
from django.utils import timezone
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset, Submit, Row, Column, HTML
from crispy_forms.bootstrap import Field, InlineRadios
from .models import Visitor, VisitorLog


class VisitorForm(forms.ModelForm):
    class Meta:
        model = Visitor
        fields = [
            'full_name', 'id_number', 'phone', 'email', 'organization',
            'visitor_type', 'purpose_of_visit', 'person_to_visit',
            'department_to_visit', 'expected_date', 'expected_time',
            'estimated_duration', 'has_vehicle', 'vehicle_plate',
            'vehicle_make', 'vehicle_model', 'vehicle_color'
        ]
        widgets = {
            'expected_date': forms.DateInput(attrs={'type': 'date', 'min': timezone.now().date()}),
            'expected_time': forms.TimeInput(attrs={'type': 'time'}),
            'purpose_of_visit': forms.Textarea(attrs={'rows': 3}),
            'has_vehicle': forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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

        return cleaned_data


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
    action = forms.ChoiceField(choices=GATE_ACTIONS)
    gate = forms.ChoiceField(choices=(('front', 'Front Gate'), ('back', 'Back Gate')), initial='front')
    id_number = forms.CharField(max_length=50, required=False,
                                help_text="Required if missing for check-in.")
    card_number = forms.CharField(max_length=20, required=False,
                                  help_text="Required for check-in to issue a visitor card.")

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get('action')
        id_number = cleaned.get('id_number')
        card_number = cleaned.get('card_number')
        if action == 'check_in':
            # require a card and ID
            if not card_number:
                self.add_error('card_number', 'Card number is required for check-in.')
            if not id_number:
                self.add_error('id_number', 'Visitor ID is required for check-in.')
        return cleaned

