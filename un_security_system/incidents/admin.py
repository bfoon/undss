from django.contrib import admin

from .models import (
    IncidentReport,
    IncidentUpdate,
    CommonServiceRequest,
    CommonServiceConfig,
    CommonServiceApprover,
    CommonServiceRequestType,
)


class IncidentUpdateInline(admin.TabularInline):
    model = IncidentUpdate
    extra = 0
    fields = ("author", "note", "is_internal", "created_at")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(IncidentReport)
class IncidentReportAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "severity", "status", "category",
        "reported_by", "assigned_to", "created_at",
    )
    list_filter = ("status", "severity", "category", "created_at")
    search_fields = (
        "title", "description", "location",
        "reported_by__username", "reported_by__first_name", "reported_by__last_name",
    )
    readonly_fields = ("created_at", "updated_at")
    inlines = [IncidentUpdateInline]
    list_per_page = 25
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(IncidentUpdate)
class IncidentUpdateAdmin(admin.ModelAdmin):
    list_display = ("id", "incident", "author", "is_internal", "created_at")
    list_filter = ("is_internal", "created_at")
    search_fields = ("note", "author__username", "incident__title")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)


@admin.register(CommonServiceRequestType)
class CommonServiceRequestTypeAdmin(admin.ModelAdmin):
    list_display = (
        "name", "code", "is_active", "requires_disruption_window",
        "sort_order", "updated_at",
    )
    list_filter = ("is_active", "requires_disruption_window")
    search_fields = ("name", "code", "description")
    ordering = ("sort_order", "name")
    readonly_fields = ("code", "created_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_add_permission(self, request):
        return bool(request.user and request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user and request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CommonServiceRequest)
class CommonServiceRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "category_label", "status", "priority", "agency",
        "requested_by", "assigned_to", "created_at", "is_notice",
    )
    list_filter = (
        "status", "priority", "category", "is_notice", "agency", "created_at",
    )
    search_fields = (
        "title", "description", "location",
        "requested_by__username", "requested_by__first_name", "requested_by__last_name",
    )
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 25
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    @admin.display(description="Request type", ordering="category")
    def category_label(self, obj):
        return obj.get_category_display()


class CommonServiceApproverInline(admin.TabularInline):
    model = CommonServiceApprover
    extra = 1
    fields = ("level", "user", "is_primary", "is_active")


@admin.register(CommonServiceConfig)
class CommonServiceConfigAdmin(admin.ModelAdmin):
    list_display = (
        "agency", "approval_levels", "level_1_manager",
        "operations_manager", "is_active",
    )
    list_filter = ("is_active", "approval_levels")
    search_fields = ("agency__name", "agency__code", "level_1_manager__username")
    inlines = [CommonServiceApproverInline]
    list_per_page = 25


@admin.register(CommonServiceApprover)
class CommonServiceApproverAdmin(admin.ModelAdmin):
    list_display = ("agency", "level", "user", "is_primary", "is_active")
    list_filter = ("level", "is_primary", "is_active", "agency")
    search_fields = (
        "agency__name", "agency__code",
        "user__username", "user__first_name", "user__last_name",
    )
    list_per_page = 50
    ordering = ("agency", "level", "-is_primary")
