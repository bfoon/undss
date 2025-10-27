from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone
from django.http import HttpResponse
from .models import User, SecurityIncident, Agency  # <-- import Agency
import csv


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "user_count")
    search_fields = ("code", "name")
    ordering = ("code",)

    def user_count(self, obj):
        return obj.users.count()
    user_count.short_description = "Users"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    # add Agency + role to fieldsets
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {
            "fields": ("first_name", "last_name", "email", "phone", "employee_id", "agency")
        }),
        ("Roles & Permissions", {
            "fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")
        }),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "password1", "password2", "role", "phone", "employee_id", "agency"),
        }),
    )

    list_display = ("username", "full_name", "email", "role", "agency", "is_active", "is_staff")
    list_filter = ("role", "agency", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email", "employee_id", "phone")
    ordering = ("username",)

    def full_name(self, obj):
        return (f"{obj.first_name} {obj.last_name}").strip() or "-"
    full_name.short_description = "Full name"


@admin.register(SecurityIncident)
class SecurityIncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "location", "reported_by", "reported_at", "resolved", "resolved_by", "resolved_at")
    list_filter = ("severity", "resolved", "reported_at")
    search_fields = ("title", "description", "location", "reported_by__username")
    date_hierarchy = "reported_at"
    actions = ["export_csv", "mark_resolved", "mark_unresolved"]
    ordering = ("-reported_at",)

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="security_incidents.csv"'
        writer = csv.writer(resp)
        writer.writerow([
            "Title", "Severity", "Location",
            "Reported By", "Reported At (UTC)",
            "Resolved", "Resolved By", "Resolved At (UTC)"
        ])
        for i in queryset.select_related("reported_by", "resolved_by"):
            writer.writerow([
                i.title,
                i.severity,
                i.location,
                getattr(i.reported_by, "username", "") or "",
                timezone.localtime(i.reported_at).isoformat() if i.reported_at else "",
                "Yes" if i.resolved else "No",
                getattr(i.resolved_by, "username", "") or "",
                timezone.localtime(i.resolved_at).isoformat() if i.resolved_at else "",
            ])
        return resp

    @admin.action(description="Mark as resolved")
    def mark_resolved(self, request, queryset):
        queryset.update(resolved=True, resolved_by=request.user, resolved_at=timezone.now())

    @admin.action(description="Mark as unresolved")
    def mark_unresolved(self, request, queryset):
        queryset.update(resolved=False, resolved_by=None, resolved_at=None)
