from django.urls import path
from django.contrib.auth import views as auth_views
from . import views, views_ict, view_asset_management
from .hr import views_hr
from .views_room_booking import (
    RoomListView, RoomDetailView, RoomCreateView, RoomUpdateView,
    MyRoomBookingsView, RoomBookingCreateView, MyRoomApprovalsView,
    room_booking_approve_view, room_delete_view
)
from .views_asset_verify import asset_verify, asset_verification_history

app_name = 'accounts'

urlpatterns = [
    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    path('login/', views.login_view, name='login'),
    path("login/otp/", views.otp_verify_view, name="otp_verify"),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password_view, name='change_password'),

    # ------------------------------------------------------------------
    # Password Reset
    # ------------------------------------------------------------------
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='accounts/password_reset.html',
            email_template_name='accounts/password_reset_email.html',
            subject_template_name='accounts/password_reset_subject.txt',
            success_url='/accounts/password-reset/done/'
        ),
        name='password_reset'
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='accounts/password_reset_done.html'
        ),
        name='password_reset_done'
    ),
    path(
        'password-reset-confirm/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='accounts/password_reset_confirm.html',
            success_url='/accounts/password-reset-complete/'
        ),
        name='password_reset_confirm'
    ),
    path(
        'password-reset-complete/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='accounts/password_reset_complete.html'
        ),
        name='password_reset_complete'
    ),

    # ------------------------------------------------------------------
    # User Management (LSA)
    # ------------------------------------------------------------------
    path('users/', views.UserListView.as_view(), name='user_list'),
    path('users/create/', views.UserCreateView.as_view(), name='user_create'),
    path('users/<int:pk>/edit/', views.UserUpdateView.as_view(), name='user_edit'),
    path('users/<int:pk>/toggle-status/', views.toggle_user_status, name='toggle_user_status'),

    # ------------------------------------------------------------------
    # HR / Employee ID features (using view_hr)
    # LSA / SOC / agency_hr roles
    # ------------------------------------------------------------------
    path(
        'hr/ids/expiring/',
        views_hr.ExpiringIDListView.as_view(),
        name='expiring_ids',
    ),
    path(
        'hr/idcard/my/',
        views_hr.my_idcard_request,
        name='my_idcard_requests',
    ),
    path(
        "my-id-card-requests/",
        views_hr.my_id_card_requests,
        name="my_id_card_requests"
    ),
    path(
            "my-id-requests/<int:pk>/",
        views_hr.my_id_card_request_detail,
        name="my_id_card_request_detail",
    ),
    path(
        'hr/idcard/admin/',
        views_hr.idcard_request_for_user,
        name='idcard_request_for_user',
    ),
    path(
        'hr/idcard/requests/',
        views_hr.idcard_request_list,
        name='idcard_request_list',
    ),
    path(
        "idcard/requests/<int:pk>/download-form/",
        views_hr.idcard_request_download_form,
         name="idcard_request_download_form"
    ),
    path(
        "hr/idcard/requests/<int:pk>/edit/",
        views_hr.idcard_request_edit,
        name="idcard_request_edit",
    ),
    path(
            "hr/idcard/requests/<int:pk>/",
            views_hr.idcard_request_detail,
            name="idcard_request_detail",
    ),
    path(
        'hr/idcard/requests/<int:pk>/approve/',
        views_hr.idcard_request_approve,
        name='idcard_request_approve',
    ),
    path(
        'hr/idcard/requests/<int:pk>/reject/',
        views_hr.idcard_request_reject,
        name='idcard_request_reject',
    ),
    path(
        'hr/idcard/requests/<int:pk>/printed/',
        views_hr.idcard_request_mark_printed,
        name='idcard_request_mark_printed',
    ),
path(
    'hr/idcard/requests/<int:pk>/issued/',
    views_hr.idcard_request_mark_issued,
    name='idcard_request_mark_issued'
),

    # ------------------------------------------------------------------
    # Activity Log
    # ------------------------------------------------------------------
    path('activity/', views.user_activity_log, name='activity_log'),
    path('activity/<int:user_id>/', views.user_activity_log, name='user_activity_log'),

    # ------------------------------------------------------------------
    # Security Incidents
    # ------------------------------------------------------------------
    path('incidents/', views.SecurityIncidentListView.as_view(), name='incident_list'),
    path('incidents/create/', views.SecurityIncidentCreateView.as_view(), name='incident_create'),
    path('incidents/<int:pk>/', views.SecurityIncidentDetailView.as_view(), name='incident_detail'),
    path('incidents/<int:pk>/resolve/', views.resolve_incident, name='resolve_incident'),

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------
    path('analytics/', views.AccountAnalyticsView.as_view(), name='analytics'),

    # ------------------------------------------------------------------
    # JSON APIs
    # ------------------------------------------------------------------
    path('api/user-search/', views.user_search_api, name='user_search_api'),
    path('api/dashboard-stats/', views.dashboard_stats_api, name='dashboard_stats_api'),

    # ------------------------------------------------------------------
    # ICT Focal Point User Management
    # ------------------------------------------------------------------
    path('ict/users/', views_ict.ICTUserListView.as_view(), name='ict_user_list'),
    path('ict/users/create/', views_ict.ICTUserCreateView.as_view(), name='ict_user_create'),
    path('ict/users/<int:pk>/', views_ict.ICTUserDetailView.as_view(), name='ict_user_detail'),
    path('ict/users/<int:pk>/edit/', views_ict.ICTUserUpdateView.as_view(), name='ict_user_edit'),
    path('ict/users/<int:pk>/set-password/', views_ict.ict_user_set_password, name='ict_user_set_password'),
    path('ict/users/<int:pk>/send-reset-link/', views_ict.ict_user_send_reset_link, name='ict_user_send_reset_link'),
    path('ict/users/<int:pk>/toggle-status/', views_ict.ict_user_toggle_status, name='ict_user_toggle_status'),
    path(
        "invites/create/",
        views_ict.create_registration_link,
        name="create_registration_link",
    ),
    path(
        "register/<str:code>/",
        views_ict.register_with_invite,
        name="register_with_invite",
    ),
    path(
        "invites/",
        views_ict.registration_links_list,
        name="registration_links_list",
    ),
    path(
        "invites/<int:pk>/",
        views_ict.registration_link_detail,
        name="registration_link_detail",
    ),
    path(
        "invites/<int:pk>/toggle/",
        views_ict.registration_link_toggle_active,
        name="registration_link_toggle_active",
    ),

    # Room Management (superuser only)
    path("rooms/add/", RoomCreateView.as_view(), name="room_add"),
    path("rooms/<int:pk>/edit/", RoomUpdateView.as_view(), name="room_edit"),
    path("rooms/<int:pk>/delete/", room_delete_view, name="room_delete"),

    # Room Listing & Detail
    path("rooms/", RoomListView.as_view(), name="room_list"),
    path("rooms/<int:pk>/", RoomDetailView.as_view(), name="room_detail"),

    # Room Booking
    path("rooms/book/", RoomBookingCreateView.as_view(), name="room_book"),
    path("rooms/my-bookings/", MyRoomBookingsView.as_view(), name="my_bookings"),

    # Approvals
    path("rooms/approvals/", MyRoomApprovalsView.as_view(), name="room_approvals"),
    path("rooms/bookings/<int:pk>/approve/", room_booking_approve_view, name="booking_approve"),
    # ------------------------------------------------------------------
    # Asset Management (Agency Service)
    # ------------------------------------------------------------------

    # Main portal (Dashboard + Tabs + Actions)
    path("assets/", view_asset_management.view_asset_management, name="asset_management"),
    path("assets/<int:asset_id>/", view_asset_management.asset_detail, name="asset_detail"),
    path("assets/report/", view_asset_management.asset_report, name="asset_report"),
    path("assets/labels.pdf", view_asset_management.asset_labels_pdf, name="asset_labels_pdf"),
path("assets/verify/", asset_verify, name="asset_verify"),
    path("assets/verification-history/", asset_verification_history, name="asset_verification_history"),
    # # ----- Requests workflow -----
    # path(
    #     "assets/requests/",
    #     view_asset_management.asset_request_list_view,
    #     name="asset_request_list",
    # ),
    # path(
    #     "assets/requests/<int:pk>/",
    #     view_asset_management.asset_request_detail_view,
    #     name="asset_request_detail",
    # ),
    #
    # # Approval step (Unit head / Asset manager / Ops manager)
    # path(
    #     "assets/requests/<int:pk>/approve/",
    #     view_asset_management.asset_request_approve_view,
    #     name="asset_request_approve",
    # ),
    # path(
    #     "assets/requests/<int:pk>/reject/",
    #     view_asset_management.asset_request_reject_view,
    #     name="asset_request_reject",
    # ),
    #
    # # ICT custodian assignment step
    # path(
    #     "assets/requests/<int:pk>/assign/",
    #     view_asset_management.asset_request_assign_view,
    #     name="asset_request_assign",
    # ),
    #
    # # Requester verifies receipt
    # path(
    #     "assets/requests/<int:pk>/verify/",
    #     view_asset_management.asset_request_verify_receipt_view,
    #     name="asset_request_verify_receipt",
    # ),
    #
    # # ----- Assets registry -----
    # path(
    #     "assets/registry/",
    #     view_asset_management.asset_registry_list_view,
    #     name="asset_registry",
    # ),
    # path(
    #     "assets/registry/<int:pk>/",
    #     view_asset_management.asset_detail_view,
    #     name="asset_detail",
    # ),
    # path(
    #     "assets/registry/<int:pk>/update/",
    #     view_asset_management.asset_update_view,
    #     name="asset_update",
    # ),
    # path(
    #     "assets/registry/<int:pk>/retire/",
    #     view_asset_management.asset_retire_view,
    #     name="asset_retire",
    # ),
    #
    # # ----- Setup (categories / units) -----
    # path(
    #     "assets/categories/",
    #     view_asset_management.asset_category_list_view,
    #     name="asset_category_list",
    # ),
    # path(
    #     "assets/categories/new/",
    #     view_asset_management.asset_category_create_view,
    #     name="asset_category_create",
    # ),
    # path(
    #     "assets/categories/<int:pk>/edit/",
    #     view_asset_management.asset_category_update_view,
    #     name="asset_category_update",
    # ),
    #
    # path(
    #     "assets/units/",
    #     view_asset_management.unit_list_view,
    #     name="asset_unit_list",
    # ),
    # path(
    #     "assets/units/new/",
    #     view_asset_management.unit_create_view,
    #     name="asset_unit_create",
    # ),
    # path(
    #     "assets/units/<int:pk>/edit/",
    #     view_asset_management.unit_update_view,
    #     name="asset_unit_update",
    # ),

    # ----- Optional: small JSON APIs (nice for dynamic UI later) -----
    # path(
    #     "assets/api/available-assets/",
    #     view_asset_management.asset_available_list_api,
    #     name="asset_available_list_api",
    # ),
    # path(
    #     "assets/api/unit-managers/",
    #     view_asset_management.unit_managers_api,
    #     name="unit_managers_api",
    # ),


]
