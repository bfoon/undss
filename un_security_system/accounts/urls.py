from django.urls import path

from . import api_mobile
from django.contrib.auth import views as auth_views
from . import views, views_ict, view_asset_management, views_batch, view_asset_reports
from .hr import views_hr
from .views_room_booking import (
    RoomListView, RoomDetailView, RoomCreateView, RoomUpdateView,
    MyRoomBookingsView, RoomBookingCreateView, MyRoomApprovalsView,
    room_booking_approve_view, room_delete_view, room_series_approve_view,
    cancel_booking, cancel_booking_series, cancel_series_occurrence, room_bookings_calendar,
    room_bookings_events, booking_detail_view, meeting_registration_view, meeting_qr_code_view,
    booking_attendee_count_api, meeting_registration_success_view, room_detail_api, attendance_checkin_lookup,
    walkin_decision_view, check_availability_api, meeting_qr_code_download_view,
    booking_attendance_export_csv, booking_attendance_export_excel, toggle_booking_option_view,
    agenda_document_qr_view, attendance_page_view, accept_registration_view,
    series_detail_view, reschedule_booking,
)
from .views_asset_verify import asset_verify, asset_verification_history
from . import views_esign
from . import views_esign_markup
from . import views_esign_self
from . import views_esign_studio
from . import views_esign_workflow
from . import views_esign_forms

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
            "register/<str:code>/qr.png",
            views_ict.invite_qr_download,
            name="invite_qr_download",
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
    path("rooms/calendar/", room_bookings_calendar, name="rooms_calendar"),
    path("rooms/calendar/events/", room_bookings_events, name="rooms_calendar_events"),


    # Approvals
    path("rooms/approvals/", MyRoomApprovalsView.as_view(), name="room_approvals"),
    path("rooms/bookings/<int:pk>/approve/", room_booking_approve_view, name="booking_approve"),
    path("rooms/series/<int:pk>/approve/", room_series_approve_view, name="series_approve"),

    # Room Booking - Cancel Actions
    path("rooms/bookings/<int:pk>/cancel/", cancel_booking, name="cancel_booking"),
    path("rooms/bookings/<int:pk>/reschedule/", reschedule_booking, name="reschedule_booking"),
    path("rooms/series/<int:pk>/cancel/", cancel_booking_series, name="cancel_series"),
    path("rooms/series/<int:pk>/detail/", series_detail_view, name="series_detail"),
    path("rooms/occurrences/<int:pk>/cancel/", cancel_series_occurrence, name="cancel_occurrence"),
    # ------------------------------------------------------------------
    # Asset Management (Agency Service)
    # ------------------------------------------------------------------

    # Main portal (Dashboard + Tabs + Actions)
    path("assets/", view_asset_management.view_asset_management, name="asset_management"),
    path("assets/<int:asset_id>/", view_asset_management.asset_detail, name="asset_detail"),
    path('assets/supply/<int:item_id>/', view_asset_management.consumable_item_detail, name='consumable_item_detail'),
    path("assets/report/", view_asset_management.asset_report, name="asset_report"),
    path("assets/labels.pdf", view_asset_management.asset_labels_pdf, name="asset_labels_pdf"),
    path("assets/verify/", asset_verify, name="asset_verify"),
    path("assets/verification-history/", asset_verification_history, name="asset_verification_history"),
    path("exit/", view_asset_management.exit_organization, name="exit_organization"),
    path("assets/consumable/<int:item_id>/", view_asset_management.consumable_item_detail, name="consumable_item_detail"),
    path("assets/supplies/export/",          view_asset_management.consumables_export,      name="consumables_export"),

    # -------- Batch upload -----------
    path("batch/template/<str:kind>/", views_batch.download_csv_template, name="batch_template"),
    path("batch/upload/<str:kind>/", views_batch.batch_upload_csv, name="batch_upload"),

    # ------------------------------------------------------------------
    # eSign — DocuSign-style electronic signature (ICT & Assets)
    # ------------------------------------------------------------------

    # Sender side (login required)
    path("esign/", views_esign.esign_dashboard, name="esign_dashboard"),
    path("esign/new/", views_esign.esign_new, name="esign_new"),

    # Sign something yourself — no recipients, no email. Creates a one-recipient
    # envelope so the conversion, stamping, certificate and audit trail are all
    # the same code as a normal send.
    path("esign/self/", views_esign_self.esign_self_new, name="esign_self_new"),
    path("esign/<int:pk>/self/sign/", views_esign_self.esign_self_finish, name="esign_self_finish"),
    path("esign/<int:pk>/", views_esign.esign_envelope_detail, name="esign_envelope_detail"),
    path("esign/<int:pk>/prepare/", views_esign.esign_prepare, name="esign_prepare"),
    path("esign/<int:pk>/fields/", views_esign.esign_fields_save, name="esign_fields_save"),
    path("esign/<int:pk>/recipients/add/", views_esign.esign_recipient_add, name="esign_recipient_add"),
    path("esign/<int:pk>/recipients/order/", views_esign.esign_recipients_reorder, name="esign_recipients_reorder"),
    path("esign/<int:pk>/recipients/<int:recipient_id>/remove/", views_esign.esign_recipient_remove, name="esign_recipient_remove"),
    path("esign/<int:pk>/send/", views_esign.esign_send, name="esign_send"),
    path("esign/<int:pk>/remind/", views_esign.esign_remind, name="esign_remind"),
    path("esign/<int:pk>/void/", views_esign.esign_void, name="esign_void"),
    path("esign/<int:pk>/rework/", views_esign.esign_rework, name="esign_rework"),
    path("esign/<int:pk>/duplicate/", views_esign.esign_duplicate, name="esign_duplicate"),
    path("esign/<int:pk>/comment/", views_esign.esign_comment_internal, name="esign_comment_internal"),
    path("esign/<int:pk>/delete/", views_esign.esign_envelope_delete, name="esign_envelope_delete"),
    path("esign/<int:pk>/resend/<int:recipient_id>/", views_esign.esign_resend, name="esign_resend"),
    path("esign/<int:pk>/preview/", views_esign.esign_preview, name="esign_preview"),
    path("esign/<int:pk>/document/<int:doc_id>/", views_esign.esign_document_file, name="esign_document_file"),
    path("esign/<int:pk>/document/<int:doc_id>/pages/", views_esign.esign_document_pages, name="esign_document_pages"),
    path("esign/<int:pk>/document/<int:doc_id>/remove/", views_esign.esign_document_remove, name="esign_document_remove"),
    path("esign/<int:pk>/documents/add/", views_esign.esign_document_add, name="esign_document_add"),
    path("esign/<int:pk>/download/<str:kind>/", views_esign.esign_download, name="esign_download"),

    # Markup & comments — sender / ICT / Ops side (login required)
    path("esign/<int:pk>/markup/", views_esign_markup.esign_markup_list_internal, name="esign_markup_list_internal"),
    path("esign/<int:pk>/markup/add/", views_esign_markup.esign_markup_add_internal, name="esign_markup_add_internal"),
    path("esign/<int:pk>/markup/<int:ann_id>/reply/", views_esign_markup.esign_markup_reply_internal, name="esign_markup_reply_internal"),
    path("esign/<int:pk>/markup/<int:ann_id>/resolve/", views_esign_markup.esign_markup_resolve_internal, name="esign_markup_resolve_internal"),
    path("esign/<int:pk>/markup/<int:ann_id>/delete/", views_esign_markup.esign_markup_delete_internal, name="esign_markup_delete_internal"),
    path("esign/<int:pk>/markup/resolve-all/", views_esign_markup.esign_markup_resolve_all_internal, name="esign_markup_resolve_all_internal"),
    path("esign/<int:pk>/rework-review/", views_esign_markup.esign_rework_review, name="esign_rework_review"),
    path("esign/<int:pk>/markup/download/", views_esign_markup.esign_markup_pdf, name="esign_markup_pdf"),

    # Saved signatures ("My signature" studio)
    path("esign/signatures/", views_esign.esign_signatures, name="esign_signatures"),
    path("esign/signatures/save/", views_esign.esign_signature_save, name="esign_signature_save"),
    path("esign/signatures/<int:pk>/delete/", views_esign.esign_signature_delete, name="esign_signature_delete"),
    path("esign/signatures/<int:pk>/default/", views_esign.esign_signature_default, name="esign_signature_default"),

    # ------------------------------------------------------------------
    # eSign Studio — PDF workbench
    # ------------------------------------------------------------------
    path("esign/studio/", views_esign_studio.esign_studio, name="esign_studio"),
    path("esign/studio/upload/", views_esign_studio.esign_studio_upload, name="esign_studio_upload"),
    path("esign/studio/tools/<slug:tool>/", views_esign_studio.esign_studio_tool, name="esign_studio_tool"),
    path("esign/studio/files/", views_esign_studio.esign_studio_files, name="esign_studio_files"),
    path("esign/studio/files/bulk/", views_esign_studio.esign_studio_files_bulk, name="esign_studio_files_bulk"),
    path("esign/studio/files/<int:pk>/", views_esign_studio.esign_studio_file, name="esign_studio_file"),
    path("esign/studio/files/<int:pk>/pdf/", views_esign_studio.esign_studio_file_pdf, name="esign_studio_file_pdf"),
    path("esign/studio/files/<int:pk>/rename/", views_esign_studio.esign_studio_file_rename, name="esign_studio_file_rename"),
    path("esign/studio/files/<int:pk>/delete/", views_esign_studio.esign_studio_file_delete, name="esign_studio_file_delete"),
    path("esign/studio/files/<int:pk>/revert/<int:number>/", views_esign_studio.esign_studio_file_revert, name="esign_studio_file_revert"),
    path("esign/studio/files/<int:pk>/organize/", views_esign_studio.esign_studio_organize, name="esign_studio_organize"),
    path("esign/studio/files/<int:pk>/organize/save/", views_esign_studio.esign_studio_organize_save, name="esign_studio_organize_save"),
    path("esign/studio/files/<int:pk>/edit/", views_esign_studio.esign_studio_edit, name="esign_studio_edit"),
    path("esign/studio/files/<int:pk>/edit/save/", views_esign_studio.esign_studio_edit_save, name="esign_studio_edit_save"),
    path("esign/studio/files/<int:pk>/sign/", views_esign_studio.esign_studio_to_sign, name="esign_studio_to_sign"),

    # eSign Studio — workflows
    path("esign/workflows/", views_esign_workflow.esign_workflows, name="esign_workflows"),
    path("esign/workflows/new/", views_esign_workflow.esign_workflow_new, name="esign_workflow_new"),
    path("esign/workflows/import/", views_esign_workflow.esign_workflow_import, name="esign_workflow_import"),
    path("esign/workflows/<int:pk>/design/", views_esign_workflow.esign_workflow_designer, name="esign_workflow_designer"),
    path("esign/workflows/<int:pk>/save/", views_esign_workflow.esign_workflow_save, name="esign_workflow_save"),
    path("esign/workflows/<int:pk>/export/", views_esign_workflow.esign_workflow_export, name="esign_workflow_export"),
    path("esign/workflows/<int:pk>/duplicate/", views_esign_workflow.esign_workflow_duplicate, name="esign_workflow_duplicate"),
    path("esign/workflows/<int:pk>/delete/", views_esign_workflow.esign_workflow_delete, name="esign_workflow_delete"),
    path("esign/workflows/<int:pk>/start/", views_esign_workflow.esign_workflow_launch, name="esign_workflow_launch"),
    path("esign/workflows/<int:pk>/slots/", views_esign_forms.esign_flow_slots, name="esign_flow_slots"),
    path("esign/runs/<int:pk>/", views_esign_workflow.esign_run_detail, name="esign_run_detail"),
    path("esign/runs/<int:pk>/pdf/<str:kind>/", views_esign_workflow.esign_run_pdf, name="esign_run_pdf"),
    path("esign/runs/<int:pk>/cancel/", views_esign_workflow.esign_run_cancel, name="esign_run_cancel"),
    path("esign/runs/<int:pk>/retry/", views_esign_workflow.esign_run_retry, name="esign_run_retry"),
    path("esign/runs/<int:pk>/tasks/<int:task_id>/reassign/", views_esign_workflow.esign_run_reassign, name="esign_run_reassign"),
    # Task links (tokenized, no login — like signing links)
    path("esign/wf/t/<str:token>/", views_esign_workflow.esign_wf_task, name="esign_wf_task"),
    path("esign/wf/t/<str:token>/pdf/", views_esign_workflow.esign_wf_task_pdf, name="esign_wf_task_pdf"),

    # eSign Studio — forms
    path("esign/forms/", views_esign_forms.esign_forms, name="esign_forms"),
    path("esign/forms/new/", views_esign_forms.esign_form_new, name="esign_form_new"),
    path("esign/forms/import-word/", views_esign_forms.esign_form_import_word, name="esign_form_import_word"),
    path("esign/forms/<int:pk>/design/", views_esign_forms.esign_form_designer, name="esign_form_designer"),
    path("esign/forms/<int:pk>/save/", views_esign_forms.esign_form_save, name="esign_form_save"),
    path("esign/forms/<int:pk>/preview.pdf", views_esign_forms.esign_form_preview_pdf, name="esign_form_preview_pdf"),
    path("esign/forms/<int:pk>/section-step/", views_esign_forms.esign_form_section_step, name="esign_form_section_step"),

    # ── the phone app ───────────────────────────────────────────────────────
    path("api/m/ping/", api_mobile.ping, name="api_m_ping"),
    path("api/m/auth/login/", api_mobile.auth_login, name="api_m_login"),
    path("api/m/auth/verify/", api_mobile.auth_verify, name="api_m_verify"),
    path("api/m/auth/resend/", api_mobile.auth_resend, name="api_m_resend"),
    path("api/m/auth/logout/", api_mobile.auth_logout, name="api_m_logout"),
    path("api/m/me/", api_mobile.me, name="api_m_me"),
    path("api/m/inbox/", api_mobile.inbox, name="api_m_inbox"),
    path("api/m/tasks/<str:token>/", api_mobile.task_detail, name="api_m_task"),
    path("api/m/tasks/<str:token>/decide/", api_mobile.task_decide, name="api_m_task_decide"),
    path("api/m/assets/lookup/", api_mobile.asset_lookup, name="api_m_asset_lookup"),
    path("api/m/assets/search/", api_mobile.asset_search, name="api_m_asset_search"),
    path("api/m/assets/mine/", api_mobile.my_assets, name="api_m_my_assets"),
    path("api/m/envelopes/", api_mobile.envelopes, name="api_m_envelopes"),
    path("api/m/envelopes/<int:pk>/", api_mobile.envelope_detail, name="api_m_envelope"),
    path("api/m/sign/<str:token>/", api_mobile.sign_sheet, name="api_m_sign_sheet"),
    path("api/m/sign/<str:token>/submit/", api_mobile.envelope_sign, name="api_m_sign"),
    path("api/m/sign/<str:token>/decline/", api_mobile.envelope_decline, name="api_m_decline"),
    path("api/m/forms/", api_mobile.forms, name="api_m_forms"),
    path("api/m/forms/<int:pk>/", api_mobile.form_schema, name="api_m_form"),
    path("api/m/forms/<int:pk>/submit/", api_mobile.form_submit, name="api_m_form_submit"),
    path("api/m/submissions/<int:pk>/", api_mobile.submission_detail, name="api_m_submission"),
    path("api/m/flows/", api_mobile.flows, name="api_m_flows"),
    path("api/m/runs/", api_mobile.runs, name="api_m_runs"),
    path("api/m/runs/<int:pk>/", api_mobile.run_detail, name="api_m_run"),
    path("api/m/runs/<int:pk>/cancel/", api_mobile.run_cancel, name="api_m_run_cancel"),
    path("esign/forms/<int:pk>/duplicate/", views_esign_forms.esign_form_duplicate, name="esign_form_duplicate"),
    path("esign/forms/<int:pk>/delete/", views_esign_forms.esign_form_delete, name="esign_form_delete"),
    path("esign/forms/<int:pk>/fill/", views_esign_forms.esign_form_fill, name="esign_form_fill"),
    path("esign/forms/<int:pk>/submissions/", views_esign_forms.esign_form_submissions, name="esign_form_submissions"),
    path("esign/submissions/<int:pk>/", views_esign_forms.esign_submission_detail, name="esign_submission_detail"),
    path("esign/submissions/<int:pk>/pdf/", views_esign_forms.esign_submission_pdf, name="esign_submission_pdf"),
    path("esign/submissions/<int:pk>/action/", views_esign_forms.esign_submission_action, name="esign_submission_action"),
    # Public form link (tokenized, no login)
    path("esign/f/<str:token>/", views_esign_forms.esign_form_public, name="esign_form_public"),

    # Recipient side (tokenized, no login — signers, CC/BCC and viewers)
    path("esign/s/<str:token>/", views_esign.esign_sign, name="esign_sign"),
    path("esign/s/<str:token>/decline/", views_esign.esign_decline, name="esign_decline"),
    path("esign/s/<str:token>/return/", views_esign.esign_return, name="esign_return"),
    path("esign/s/<str:token>/comment/", views_esign.esign_comment, name="esign_comment"),
    path("esign/s/<str:token>/review/", views_esign.esign_review, name="esign_review"),
    path("esign/s/<str:token>/document/<int:doc_id>/", views_esign.esign_token_document, name="esign_token_document"),
    path("esign/s/<str:token>/download/<str:kind>/", views_esign.esign_token_download, name="esign_token_download"),

    # Markup & comments — recipient side (tokenized, no login)
    path("esign/s/<str:token>/markup/", views_esign_markup.esign_markup_list, name="esign_markup_list"),
    path("esign/s/<str:token>/markup/add/", views_esign_markup.esign_markup_add, name="esign_markup_add"),
    path("esign/s/<str:token>/markup/<int:pk>/reply/", views_esign_markup.esign_markup_reply, name="esign_markup_reply"),
    path("esign/s/<str:token>/markup/<int:pk>/resolve/", views_esign_markup.esign_markup_resolve, name="esign_markup_resolve"),
    path("esign/s/<str:token>/markup/<int:pk>/delete/", views_esign_markup.esign_markup_delete, name="esign_markup_delete"),

    # --- NEW URLs ---
    path('booking/<int:pk>/', booking_detail_view, name='booking_detail'),
    path('booking/<int:pk>/agenda-qr/', agenda_document_qr_view, name='booking_agenda_qr'),
    path('booking/<int:pk>/attendees/count/', booking_attendee_count_api, name='booking_attendee_count'),
    path('meeting/register/<uuid:registration_code>/', meeting_registration_view,
         name='meeting_registration'),
    path('meeting/register/qr/<uuid:registration_code>/', meeting_qr_code_view,
         name='meeting_qr_code'),
    path('meeting/register/success/', meeting_registration_success_view, name='meeting_registration_success'),
    path('registration/<int:pk>/<str:action>/', accept_registration_view, name='accept_registration'),
    path('meeting/attendance/<uuid:registration_code>/', attendance_page_view, name='meeting_attendance_page'),
    path('api/room-details/<int:pk>/', room_detail_api, name='room_detail_api'),
    path('meeting/register/<uuid:registration_code>/check/', attendance_checkin_lookup, name='attendance_checkin_lookup'),
    path('attendance/<int:pk>/<str:action>/', walkin_decision_view, name='walkin_decision'),
    path('api/check-availability/', check_availability_api, name='check_availability'),
    path("meeting/<uuid:registration_code>/qr/download/", meeting_qr_code_download_view, name="meeting_qr_code_download"),
    path("booking/<int:pk>/attendance/export/csv/", booking_attendance_export_csv, name="booking_attendance_export_csv"),
    path("booking/<int:pk>/attendance/export/excel/", booking_attendance_export_excel, name="booking_attendance_export_excel"),
    path("booking/<int:pk>/toggle-option/", toggle_booking_option_view, name="toggle_booking_option"),

    path("asset-reports/", view_asset_reports.asset_reports, name="asset_reports"),
    path("asset-reports/download/excel/", view_asset_reports.asset_reports_excel, name="asset_reports_excel"),
    path("asset-reports/download/word/", view_asset_reports.asset_reports_word, name="asset_reports_word"),
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