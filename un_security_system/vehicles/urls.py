from django.urls import path
from . import views

app_name = 'vehicles'

urlpatterns = [
    # Vehicle Management
    path('', views.VehicleListView.as_view(), name='vehicle_list'),
    path('create/', views.VehicleCreateView.as_view(), name='vehicle_create'),
    path('<int:pk>/', views.VehicleDetailView.as_view(), name='vehicle_detail'),
    path('<int:pk>/edit/', views.VehicleUpdateView.as_view(), name='vehicle_edit'),
    path('<int:pk>/delete/', views.VehicleDeleteView.as_view(), name='vehicle_delete'),

    # Vehicle Movements
    path('movements/', views.VehicleMovementListView.as_view(), name='movement_list'),
    path('movements/<int:pk>/', views.VehicleMovementDetailView.as_view(), name='movement_detail'),
    path('record-movement/', views.record_vehicle_movement, name='record_movement'),
    path('quick-movement/', views.quick_movement_page, name='quick_movement'),

    # Parking Cards
    path('parking-cards/', views.ParkingCardListView.as_view(), name='parking_card_list'),
    path('parking-cards/create/', views.ParkingCardCreateView.as_view(), name='parking_card_create'),
    path('parking-cards/<int:pk>/', views.ParkingCardDetailView.as_view(), name='parking_card_detail'),
    path('parking-cards/<int:pk>/edit/', views.ParkingCardUpdateView.as_view(), name='parking_card_edit'),
    path('parking-cards/<int:pk>/deactivate/', views.deactivate_parking_card, name='parking_card_deactivate'),
    path('parking-cards/<int:pk>/reactivate/', views.reactivate_parking_card, name='parking_card_reactivate'),

    # Reports and Analytics
    path('reports/', views.vehicle_reports_view, name='reports'),
    path('reports/movements/', views.movement_reports_view, name='movement_reports'),
    path('reports/parking-cards/', views.parking_card_reports_view, name='parking_card_reports'),

    # API Endpoints
    path('api/validate-parking-card/', views.validate_parking_card, name='validate_parking_card'),
    path('api/vehicle-lookup/', views.vehicle_lookup, name='vehicle_lookup'),
    path('api/movements/recent/', views.recent_movements_api, name='recent_movements_api'),
    path('api/stats/', views.vehicle_stats_api, name='vehicle_stats_api'),
    path('api/compound-status/', views.compound_status_api, name='compound_status_api'),

    # Bulk Operations
    path('bulk/export-movements/', views.export_movements, name='export_movements'),
    path('bulk/export-parking-cards/', views.export_parking_cards, name='export_parking_cards'),

    # Asset Exit Clearance
    path('asset-exit/new/', views.asset_exit_new, name='asset_exit_new'),
    path('asset-exit/my/', views.my_asset_exits, name='my_asset_exits'),
    path('asset-exit/<int:pk>/', views.asset_exit_detail, name='asset_exit_detail'),
    path('asset-exit/<int:pk>/lsa-clear/', views.asset_exit_lsa_clear, name='asset_exit_lsa_clear'),
    path('asset-exit/<int:pk>/lsa-reject/', views.asset_exit_lsa_reject, name='asset_exit_lsa_reject'),
    path('asset-exit/<int:pk>/cancel/', views.asset_exit_cancel, name='asset_exit_cancel'),
    path('asset-exit/<int:pk>/agency-approve/', views.asset_exit_agency_approve, name='asset_exit_agency_approve'),
    path('asset-exit/<int:pk>/edit/', views.asset_exit_edit, name='asset_exit_edit'),
    path('asset-exit/verify/page', views.asset_exit_verify_page, name='asset_exit_verify_page'),
    # Review/Queue for LSA & SOC
    path('asset-exit/queue/', views.AssetExitQueueView.as_view(), name='asset_exit_queue'),

    # Guard list of approved exits
    path('asset-exit/approved/', views.GuardApprovedAssetExitsView.as_view(), name='asset_exit_approved_list'),

    # add these if you want print/duplicate actions
    path('asset-exit/<int:pk>/print/', views.asset_exit_print, name='asset_exit_print'),
    path('asset-exit/<int:pk>/duplicate/', views.asset_exit_duplicate, name='asset_exit_duplicate'),

    # Parking card actions
    path('parking-cards/<int:pk>/print/', views.parking_card_print, name='parking_card_print'),
    path('parking-cards/<int:pk>/duplicate/', views.parking_card_duplicate, name='parking_card_duplicate'),
    path('parking-cards/<int:pk>/delete/', views.parking_card_delete, name='parking_card_delete'),

    # Parking Card Requests (staff → LSA)
    path('parking-cards/request/new/', views.pc_request_new, name='pc_request_new'),
    path('parking-cards/request/my/', views.my_pc_requests, name='my_pc_requests'),
    path('parking-cards/requests/pending/', views.pc_requests_pending, name='pc_requests_pending'),   # LSA
    path('parking-cards/request/<int:pk>/approve/', views.pc_request_approve, name='pc_request_approve'),  # LSA
    path('parking-cards/request/<int:pk>/reject/', views.pc_request_reject, name='pc_request_reject'),     # LSA
    path('parking-cards/request/<int:pk>/cancel/', views.pc_request_cancel, name='pc_request_cancel'),

    # Guard verify + sign in/out
    path('asset-exit/verify/', views.asset_exit_verify_page, name='asset_exit_verify_page'),
    path('api/asset-exit/lookup/', views.asset_exit_lookup_api, name='asset_exit_lookup_api'),
    path('asset-exit/<int:pk>/sign-out/', views.asset_exit_mark_signed_out, name='asset_exit_mark_signed_out'),
    path('asset-exit/<int:pk>/sign-in/', views.asset_exit_mark_signed_in, name='asset_exit_mark_signed_in'),

    # --- KEY CONTROL ------------------------------------------------------------
    path('keys/', views.KeyListView.as_view(), name='key_list'),
    path('keys/create/', views.KeyCreateView.as_view(), name='key_create'),
    path('keys/<int:pk>/', views.KeyDetailView.as_view(), name='key_detail'),
    path('keys/<int:pk>/edit/', views.KeyUpdateView.as_view(), name='key_edit'),
    path("keys/<int:pk>/toggle-active/", views.key_toggle_active, name="key_toggle_active"),

    path('keys/<int:pk>/issue/', views.key_issue, name='key_issue'),
    path('keys/<int:pk>/return/', views.key_return, name='key_return'),

    path('keys/logs/', views.KeyLogListView.as_view(), name='key_logs'),
    path('keys/quick/', views.quick_key_page, name='quick_key'),
    path('api/keys/lookup/', views.key_lookup_api, name='key_lookup_api'),

    # Packages & Mailroom
    path('packages/', views.package_list, name='package_list'),
    path('packages/new/', views.package_log_new, name='package_log_new'),
    path('packages/<int:pk>/', views.package_detail, name='package_detail'),
    path('packages/<int:pk>/reception/receive/', views.package_mark_reception_received, name='package_reception_receive'),
    path('packages/<int:pk>/reception/send-to-agency/', views.package_send_to_agency, name='package_send_to_agency'),
    path('packages/<int:pk>/agency/receive/', views.package_mark_agency_received, name='package_agency_receive'),
    path('packages/<int:pk>/deliver/', views.package_mark_delivered, name='package_deliver'),
    path('api/package/track/', views.package_track_api, name='package_track_api'),

    path('asset-exit/<int:pk>/qr/', views.asset_exit_qr_code, name='asset_exit_qr_code'),

]