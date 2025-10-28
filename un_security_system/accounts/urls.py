# accounts/urls.py
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views, views_ict

app_name = 'accounts'

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('change-password/', views.change_password_view, name='change_password'),

    # Password Reset (Django built-in views)
    path('password-reset/',
         auth_views.PasswordResetView.as_view(
             template_name='accounts/password_reset.html',
             email_template_name='accounts/password_reset_email.html',
             subject_template_name='accounts/password_reset_subject.txt',
             success_url='/accounts/password-reset/done/'
         ),
         name='password_reset'),
    path('password-reset/done/',
         auth_views.PasswordResetDoneView.as_view(
             template_name='accounts/password_reset_done.html'
         ),
         name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/',
         auth_views.PasswordResetConfirmView.as_view(
             template_name='accounts/password_reset_confirm.html',
             success_url='/accounts/password-reset-complete/'
         ),
         name='password_reset_confirm'),
    path('password-reset-complete/',
         auth_views.PasswordResetCompleteView.as_view(
             template_name='accounts/password_reset_complete.html'
         ),
         name='password_reset_complete'),

    # User Management (LSA)
    path('users/', views.UserListView.as_view(), name='user_list'),
    path('users/create/', views.UserCreateView.as_view(), name='user_create'),
    path('users/<int:pk>/edit/', views.UserUpdateView.as_view(), name='user_edit'),
    path('users/<int:pk>/toggle-status/', views.toggle_user_status, name='toggle_user_status'),

    # Activity Log
    path('activity/', views.user_activity_log, name='activity_log'),
    path('activity/<int:user_id>/', views.user_activity_log, name='user_activity_log'),

    # Security Incidents
    path('incidents/', views.SecurityIncidentListView.as_view(), name='incident_list'),
    path('incidents/create/', views.SecurityIncidentCreateView.as_view(), name='incident_create'),
    path('incidents/<int:pk>/', views.SecurityIncidentDetailView.as_view(), name='incident_detail'),
    path('incidents/<int:pk>/resolve/', views.resolve_incident, name='resolve_incident'),

    # Analytics (LSA)
    path('analytics/', views.AccountAnalyticsView.as_view(), name='analytics'),

    # JSON APIs
    path('api/user-search/', views.user_search_api, name='user_search_api'),
    path('api/dashboard-stats/', views.dashboard_stats_api, name='dashboard_stats_api'),

    # ICT Focal Point User Management
    path('ict/users/', views_ict.ICTUserListView.as_view(), name='ict_user_list'),
    path('ict/users/create/', views_ict.ICTUserCreateView.as_view(), name='ict_user_create'),
    path('ict/users/<int:pk>/', views_ict.ICTUserDetailView.as_view(), name='ict_user_detail'),
    path('ict/users/<int:pk>/edit/', views_ict.ICTUserUpdateView.as_view(), name='ict_user_edit'),
    path('ict/users/<int:pk>/set-password/', views_ict.ict_user_set_password, name='ict_user_set_password'),
    path('ict/users/<int:pk>/send-reset-link/', views_ict.ict_user_send_reset_link, name='ict_user_send_reset_link'),
    path('ict/users/<int:pk>/toggle-status/', views_ict.ict_user_toggle_status, name='ict_user_toggle_status'),
]