from django.contrib import admin
from .models import (
    IncidentReport,
    IncidentUpdate,
    CommonServiceRequest,
    CommonServiceConfig,
    CommonServiceApprover
)


# ============================================
# INCIDENT REPORT ADMIN
# ============================================

class IncidentUpdateInline(admin.TabularInline):
    """Inline editor for incident updates"""
    model = IncidentUpdate
    extra = 0
    fields = ('author', 'note', 'is_internal', 'created_at')
    readonly_fields = ('created_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(IncidentReport)
class IncidentReportAdmin(admin.ModelAdmin):
    """Admin interface for Security Incident Reports"""

    list_display = (
        'id',
        'title',
        'severity',
        'status',
        'category',
        'reported_by',
        'assigned_to',
        'created_at',
    )

    list_filter = (
        'status',
        'severity',
        'category',
        'created_at',
    )

    search_fields = (
        'title',
        'description',
        'location',
        'reported_by__username',
        'reported_by__first_name',
        'reported_by__last_name',
    )

    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Incident Information', {
            'fields': (
                'title',
                'description',
                'category',
                'location',
                'occurred_at',
            )
        }),
        ('Classification', {
            'fields': (
                'severity',
                'status',
            )
        }),
        ('Assignment', {
            'fields': (
                'reported_by',
                'assigned_to',
            )
        }),
        ('Attachment', {
            'fields': ('attachment',),
        }),
        ('Metadata', {
            'fields': (
                'created_at',
                'updated_at',
            ),
        }),
    )

    inlines = [IncidentUpdateInline]

    list_per_page = 25
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


# ============================================
# INCIDENT UPDATE ADMIN
# ============================================

@admin.register(IncidentUpdate)
class IncidentUpdateAdmin(admin.ModelAdmin):
    """Admin interface for Incident Updates"""

    list_display = (
        'id',
        'incident',
        'author',
        'is_internal',
        'created_at',
    )

    list_filter = (
        'is_internal',
        'created_at',
    )

    search_fields = (
        'note',
        'author__username',
        'incident__title',
    )

    readonly_fields = ('created_at',)

    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


# ============================================
# COMMON SERVICE REQUEST ADMIN
# ============================================

@admin.register(CommonServiceRequest)
class CommonServiceRequestAdmin(admin.ModelAdmin):
    """Admin interface for Common Service Requests"""

    list_display = (
        'id',
        'title',
        'category',
        'status',
        'priority',
        'agency',
        'requested_by',
        'assigned_to',
        'created_at',
        'is_notice',
    )

    list_filter = (
        'status',
        'priority',
        'category',
        'is_notice',
        'agency',
        'created_at',
    )

    search_fields = (
        'title',
        'description',
        'location',
        'requested_by__username',
        'requested_by__first_name',
        'requested_by__last_name',
    )

    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        ('Request Information', {
            'fields': (
                'title',
                'description',
                'category',
                'location',
                'priority',
            )
        }),
        ('Status & Assignment', {
            'fields': (
                'status',
                'requested_by',
                'assigned_to',
                'agency',
            )
        }),
        ('Facility Notice', {
            'fields': (
                'is_notice',
                'disruption_start',
                'disruption_end',
            ),
        }),
        ('Escalation', {
            'fields': (
                'escalated_to',
                'escalated_to_user',
                'escalated_at',
            ),
        }),
        ('Approval Workflow', {
            'fields': (
                'requires_approval',
                'current_level',
                'approved_at',
                'approved_by',
            ),
        }),
        ('Linked Incident', {
            'fields': ('incident',),
        }),
        ('Attachment', {
            'fields': ('attachment',),
        }),
        ('Metadata', {
            'fields': (
                'created_at',
                'updated_at',
            ),
        }),
    )

    list_per_page = 25
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


# ============================================
# COMMON SERVICE CONFIG ADMIN
# ============================================

class CommonServiceApproverInline(admin.TabularInline):
    """Inline editor for approvers"""
    model = CommonServiceApprover
    extra = 1
    fields = ('level', 'user', 'is_primary', 'is_active')


@admin.register(CommonServiceConfig)
class CommonServiceConfigAdmin(admin.ModelAdmin):
    """Admin interface for Common Service Configuration"""

    list_display = (
        'agency',
        'approval_levels',
        'level_1_manager',
        'operations_manager',
        'is_active',
    )

    list_filter = (
        'is_active',
        'approval_levels',
    )

    search_fields = (
        'agency__name',
        'agency__code',
        'level_1_manager__username',
    )

    inlines = [CommonServiceApproverInline]

    list_per_page = 25


# ============================================
# COMMON SERVICE APPROVER ADMIN
# ============================================

@admin.register(CommonServiceApprover)
class CommonServiceApproverAdmin(admin.ModelAdmin):
    """Admin interface for Common Service Approvers"""

    list_display = (
        'agency',
        'level',
        'user',
        'is_primary',
        'is_active',
    )

    list_filter = (
        'level',
        'is_primary',
        'is_active',
        'agency',
    )

    search_fields = (
        'agency__name',
        'agency__code',
        'user__username',
        'user__first_name',
        'user__last_name',
    )

    list_per_page = 50
    ordering = ('agency', 'level', '-is_primary')