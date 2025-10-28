from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone
from django.http import HttpResponse
from .models import User, SecurityIncident, Agency  # <-- import Agency
import csv, secrets, string
from django.contrib import messages


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "user_count")
    search_fields = ("code", "name")
    ordering = ("code",)

    def user_count(self, obj):
        return obj.users.count()
    user_count.short_description = "Users"



def _gen_temp_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email", "phone", "employee_id")}),
        ("Roles & Permissions", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Password policy", {"fields": ("must_change_password", "temp_password_set_at")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("username", "email", "password1", "password2", "role", "phone", "employee_id")}),
    )
    list_display = ("username", "get_full_name", "email", "role", "is_active", "must_change_password")
    list_filter = ("role", "is_active", "must_change_password", "is_staff", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email", "employee_id", "phone")
    ordering = ("username",)
    actions = ["generate_temporary_passwords", "activate_users", "deactivate_users", "clear_must_change_flag"]

    def get_full_name(self, obj):
        return (f"{obj.first_name} {obj.last_name}").strip() or "-"
    get_full_name.short_description = "Full name"

    @admin.action(description="Generate temporary passwords (CSV) & require change")
    def generate_temporary_passwords(self, request, queryset):
        """
        Sets a random temporary password for selected users, marks them to change at next login,
        and returns a CSV with username/email/temp_password. (Safer than printing on screen.)
        """
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="temporary_passwords_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
        writer = csv.writer(resp)
        writer.writerow(["username", "email", "temporary_password", "must_change_password"])

        count = 0
        for user in queryset:
            temp = _gen_temp_password()
            user.set_password(temp)
            user.must_change_password = True
            user.temp_password_set_at = timezone.now()
            user.save(update_fields=["password", "must_change_password", "temp_password_set_at"])
            writer.writerow([user.username, user.email, temp, "yes"])
            count += 1

        messages.success(request, f"Temporary passwords generated for {count} user(s).")
        return resp

    @admin.action(description="Activate selected users")
    def activate_users(self, request, queryset):
        updated = queryset.update(is_active=True)
        messages.success(request, f"Activated {updated} user(s).")

    @admin.action(description="Deactivate selected users")
    def deactivate_users(self, request, queryset):
        updated = queryset.update(is_active=False)
        messages.success(request, f"Deactivated {updated} user(s).")

    @admin.action(description="Clear 'must change password' flag")
    def clear_must_change_flag(self, request, queryset):
        updated = queryset.update(must_change_password=False)
        messages.success(request, f"Cleared flag for {updated} user(s).")


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
