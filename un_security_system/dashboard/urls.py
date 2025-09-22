from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    # Main Dashboard
    path('', views.DashboardView.as_view(), name='dashboard'),

    # Role-specific Dashboards
    path('data-entry/', views.DataEntryDashboardView.as_view(), name='data_entry_dashboard'),
    path('lsa/', views.LSADashboardView.as_view(), name='lsa_dashboard'),
    path('soc/', views.SOCDashboardView.as_view(), name='soc_dashboard'),

    # Analytics and Reports
    path('analytics/', views.AnalyticsDashboardView.as_view(), name='analytics'),
    path('reports/', views.ReportsView.as_view(), name='reports'),
    path('reports/daily/', views.daily_report_view, name='daily_report'),
    path('reports/weekly/', views.weekly_report_view, name='weekly_report'),
    path('reports/monthly/', views.monthly_report_view, name='monthly_report'),

    # API Endpoints for Real-time Updates
    path('api/', views.dashboard_api, name='dashboard_api'),
    path('api/stats/', views.dashboard_stats_api, name='dashboard_stats_api'),
    path('api/activities/', views.recent_activities_api, name='recent_activities_api'),
    path('api/alerts/', views.security_alerts_api, name='security_alerts_api'),
    path('api/live-feed/', views.live_feed_api, name='live_feed_api'),

    # Quick Actions
    path('quick-actions/', views.quick_actions_page, name='quick_actions'),
    path('search/', views.global_search_view, name='global_search'),

    # Settings and Configuration
    path('settings/', views.settings_view, name='settings'),
    path('help/', views.help_view, name='help'),

    # Export Functions
    path('export/daily-summary/', views.export_daily_summary, name='export_daily_summary'),
    path('export/security-report/', views.export_security_report, name='export_security_report'),
]