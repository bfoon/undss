from django import forms
from .models import CommunicationDevice, RadioCheckSession, RadioCheckEntry

class CommunicationDeviceForm(forms.ModelForm):
    class Meta:
        model  = CommunicationDevice
        fields = ["device_type", "call_sign", "imei", "serial_number", "notes"]

    def clean(self):
        cleaned = super().clean()
        # model.clean() already enforces conditional reqs
        return cleaned


class AdminDeviceForm(forms.ModelForm):
    class Meta:
        model  = CommunicationDevice
        fields = ["device_type", "call_sign", "imei", "serial_number",
                  "assigned_to", "status", "notes"]


class RadioCheckSessionForm(forms.ModelForm):
    class Meta:
        model  = RadioCheckSession
        fields = ["name"]


class RadioCheckEntryForm(forms.ModelForm):
    class Meta:
        model  = RadioCheckEntry
        fields = ["responded", "noted_issue"]
