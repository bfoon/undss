from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from .models import User, SecurityIncident
from django.utils.html import format_html
from django.http import HttpResponse
import csv

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email", "phone", "employee_id")}),
        ("Roles & Permissions", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "email", "password1", "password2", "role", "phone", "employee_id")}),
    )
    list_display = ("username", "full_name", "email", "role", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email", "employee_id", "phone")
    ordering = ("username",)

    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or "-"

@admin.register(SecurityIncident)
class SecurityIncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "location", "reported_by", "reported_at", "resolved")
    list_filter = ("severity", "resolved", "reported_at")
    search_fields = ("title", "description", "location", "reported_by__username")
    date_hierarchy = "reported_at"
    actions = ["export_csv", "mark_resolved"]

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="security_incidents.csv"'
        writer = csv.writer(resp)
        writer.writerow(["Title", "Severity", "Location", "Reported By", "Reported At", "Resolved"])
        for i in queryset:
            writer.writerow([i.title, i.severity, i.location, i.reported_by.username, i.reported_at, "Yes" if i.resolved else "No"])
        return resp

    @admin.action(description="Mark as resolved")
    def mark_resolved(self, request, queryset):
        queryset.update(resolved=True)
