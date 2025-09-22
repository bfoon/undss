from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password_view, name='change_password'),

    # User Management (LSA only)
    path('users/', views.UserListView.as_view(), name='user_list'),
    path('users/create/', views.UserCreateView.as_view(), name='user_create'),
    path('users/<int:pk>/edit/', views.UserUpdateView.as_view(), name='user_edit'),
    path('users/<int:pk>/toggle-status/', views.toggle_user_status, name='toggle_user_status'),
    path('users/<int:user_id>/activity/', views.user_activity_log, name='user_activity_log'),
    path('activity/', views.user_activity_log, name='my_activity_log'),

    # Security Incidents
    path('incidents/', views.SecurityIncidentListView.as_view(), name='incident_list'),
    path('incidents/create/', views.SecurityIncidentCreateView.as_view(), name='incident_create'),
    path('incidents/<int:pk>/', views.SecurityIncidentDetailView.as_view(), name='incident_detail'),
    path('incidents/<int:pk>/resolve/', views.resolve_incident, name='resolve_incident'),

    # Analytics and Reports (LSA only)
    path('analytics/', views.AccountAnalyticsView.as_view(), name='analytics'),

    # API Endpoints
    path('api/users/search/', views.user_search_api, name='user_search_api'),
    path('api/dashboard-stats/', views.dashboard_stats_api, name='dashboard_stats_api'),
]