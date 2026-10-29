from django.contrib import admin

from .models import AccessAudit, AccessGroup, AccessRequest, AccessRole, RoleAssignment


@admin.register(AccessRole)
class AccessRoleAdmin(admin.ModelAdmin):
    list_display = ("name", "is_system", "legacy_code", "agency", "country_office", "requestable", "is_active")
    list_filter = ("is_system", "is_active", "agency")
    search_fields = ("name", "legacy_code")
    filter_horizontal = ("inherits",)


@admin.register(AccessGroup)
class AccessGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "agency", "country_office", "is_active")
    filter_horizontal = ("members", "roles")
    search_fields = ("name",)


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("role", "user", "group", "scope", "starts_at", "ends_at", "revoked_at", "delegated_by")
    list_filter = ("scope", "role")
    raw_id_fields = ("user", "group", "granted_by", "delegated_by", "revoked_by")


@admin.register(AccessRequest)
class AccessRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status", "created_at", "decided_by")
    list_filter = ("status",)


@admin.register(AccessAudit)
class AccessAuditAdmin(admin.ModelAdmin):
    list_display = ("at", "actor", "action", "summary")
    list_filter = ("action",)
    readonly_fields = [f.name for f in AccessAudit._meta.fields]
