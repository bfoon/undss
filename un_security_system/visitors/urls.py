from django.urls import path
from . import views

app_name = 'visitors'

urlpatterns = [
    # Visitor Management
    path('', views.VisitorListView.as_view(), name='visitor_list'),
    path('create/', views.VisitorCreateView.as_view(), name='visitor_create'),
    path('<int:pk>/', views.VisitorDetailView.as_view(), name='visitor_detail'),
    path('<int:pk>/edit/', views.VisitorUpdateView.as_view(), name='visitor_edit'),
    path('<int:visitor_id>/approve/', views.approve_visitor, name='approve_visitor'),
    path("export/", views.export_visitors, name="visitor_export"),
    path("reports/", views.VisitorReportView.as_view(), name="visitor_reports"),

    # actions from the detail page
    path('<int:pk>/request-clearance/', views.visitor_request_clearance, name='visitor_request_clearance'),
    path('<int:pk>/lsa-approve/', views.visitor_lsa_approve, name='visitor_lsa_approve'),
    path('<int:pk>/lsa-reject/', views.visitor_lsa_reject, name='visitor_lsa_reject'),
    path('<int:pk>/cancel-request/', views.visitor_cancel_request, name='visitor_cancel_request'),

    # Group Member Management
    path('<int:visitor_id>/member/<int:member_id>/delete/',
         views.delete_group_member,
         name='delete_group_member'),

    # Check-in/Check-out
    path('<int:visitor_id>/check-in/', views.check_in_visitor, name='check_in_visitor'),
    path('<int:visitor_id>/check-out/', views.check_out_visitor, name='check_out_visitor'),
    path('quick-check/', views.quick_check_page, name='quick_check_page'),

    # Filtered Views
    path('pending/', views.VisitorListView.as_view(), {'filter_status': 'pending'}, name='pending_approvals'),
    path('approved/', views.VisitorListView.as_view(), {'filter_status': 'approved'}, name='approved_visitors'),
    path('rejected/', views.VisitorListView.as_view(), {'filter_status': 'rejected'}, name='rejected_visitors'),
    path('active/', views.active_visitors_view, name='active_visitors'),

    # Visitor Logs
    path('logs/', views.VisitorLogListView.as_view(), name='visitor_logs'),
    path('<int:visitor_id>/logs/', views.visitor_logs_detail, name='visitor_logs_detail'),

    # API Endpoints
    path('api/quick-check/', views.quick_visitor_check, name='quick_check'),
    path('api/search/', views.visitor_search_api, name='visitor_search_api'),
    path('api/stats/', views.visitor_stats_api, name='visitor_stats_api'),
    path('api/<int:visitor_id>/status/', views.visitor_status_api, name='visitor_status_api'),

    # Bulk Operations
    path('bulk/approve/', views.bulk_approve_visitors, name='bulk_approve'),
    path('bulk/export/', views.export_visitors, name='export_visitors'),

    # Verify (Gate) — used by base.html
    path('gate/<int:pk>/', views.gate_check_view, name='gate_check'),
    path('verify/', views.visitor_verify_page, name='visitor_verify_page'),
    path('api/verify-lookup/', views.visitor_verify_lookup_api, name='visitor_verify_lookup_api'),

    path('cards/', views.visitor_card_list, name='visitor_card_list'),
    path('cards/create/', views.visitor_card_create, name='visitor_card_create'),
    path('cards/<int:pk>/', views.visitor_card_detail, name='visitor_card_detail'),
    path('api/cards/check/', views.visitor_card_check_api, name='visitor_card_check'),
]