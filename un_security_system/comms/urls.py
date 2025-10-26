from django.urls import path
from . import views

app_name = "comms"

urlpatterns = [
    # Staff (not guards) - My Devices
    path("my/", views.MyDevicesView.as_view(), name="my_devices"),
    path("my/new/", views.DeviceCreateView.as_view(), name="device_create"),

    # Device detail / edit (LSA/SOC)
    path("devices/<int:pk>/", views.CommunicationDeviceDetailView.as_view(), name="device_detail"),
    path("devices/<int:pk>/edit/", views.CommunicationDeviceUpdateView.as_view(), name="device_edit"),
    path("devices/<int:pk>/mark-status/", views.device_mark_status, name="device_mark_status"),

    # LSA/SOC — Radios & Sat Phones Lists
    path("radios/", views.RadioListView.as_view(), name="radios"),
    path("satphones/", views.SatPhoneListView.as_view(), name="satphones"),
    path("radios/missing-users/", views.UsersWithoutRadiosView.as_view(), name="users_without_radios"),

    # Radio Status Updates (LSA/SOC)
    path("radios/<int:pk>/update-status/", views.radio_update_status, name="radio_update_status"),

    # Export Routes
    path("export/radios.csv", views.export_radios_csv, name="export_radios_csv"),
    path("export/radios.xlsx", views.export_radios_xlsx, name="export_radios_xlsx"),
    path("export/satphones.csv", views.export_satphones_csv, name="export_satphones_csv"),
    path("export/satphones.xlsx", views.export_satphones_xlsx, name="export_satphones_xlsx"),
    path("export/users-without-radios.csv", views.export_users_without_radios_csv, name="export_users_without_radios_csv"),
    path("export/users-without-radios.xlsx", views.export_users_without_radios_xlsx, name="export_users_without_radios_xlsx"),

    # Radio Check Sessions (SOC/LSA)
    path("checks/new/", views.RadioCheckStartView.as_view(), name="check_start"),
    path("checks/<int:pk>/", views.RadioCheckRunView.as_view(), name="check_run"),
    path("checks/<int:pk>/export.csv", views.export_check_csv, name="check_export_csv"),
    path("checks/<int:pk>/export.xlsx", views.export_check_xlsx, name="check_export_xlsx"),
]