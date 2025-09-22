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
]