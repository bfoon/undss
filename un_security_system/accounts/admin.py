from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone
from django.http import HttpResponse
from django.contrib import messages

from .models import (
    User,
    SecurityIncident,
    Agency,
    EmployeeIDCardRequest,
    Room,
    RoomBooking,
    RoomApprover,
    RoomAmenity,
    AgencyServiceConfig, AgencyAssetRoles, Unit,
    AssetCategory, Asset, AssetRequest, AssetChangeRequest,
    ExitRequest, MobileLine, CellServiceFocalPoint
)

import csv
import secrets
import string


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
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _gen_otp_code(length=6):
    """Generate a numeric OTP code (e.g. 6 digits)."""
    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(length))


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal info",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "employee_id",  # Staff ID
                    "agency",
                )
            },
        ),
        (
            "Roles & Permissions",
            {
                "fields": (
                    "role",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Password policy",
            {
                "fields": (
                    "must_change_password",
                    "temp_password_set_at",
                )
            },
        ),
        (
            "OTP / Device login",
            {
                "fields": (
                    "otp_code",
                    "otp_expires_at",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "password1",
                    "password2",
                    "role",
                    "phone",
                    "employee_id",  # Staff ID on creation
                    "agency",
                ),
            },
        ),
    )

    # helper so label shows "Staff ID" instead of "employee_id"
    def staff_id(self, obj):
        return obj.employee_id or ""

    staff_id.short_description = "Staff ID"

    list_display = (
        "username",
        "get_full_name",
        "staff_id",  # <-- Staff ID column
        "email",
        "agency",
        "role",
        "is_active",
        "must_change_password",
    )
    list_filter = (
        "role",
        "agency",
        "is_active",
        "must_change_password",
        "is_staff",
        "is_superuser",
    )
    search_fields = (
        "username",
        "first_name",
        "last_name",
        "email",
        "employee_id",
        "phone",
        "agency__name",
        "agency__code",
    )
    ordering = ("username",)

    readonly_fields = (
        "temp_password_set_at",
        "otp_code",
        "otp_expires_at",
    )

    actions = [
        "generate_temporary_passwords",
        "generate_login_otps",
        "clear_otps",
        "activate_users",
        "deactivate_users",
        "clear_must_change_flag",
    ]

    def get_full_name(self, obj):
        return (f"{obj.first_name} {obj.last_name}").strip() or "-"

    get_full_name.short_description = "Full name"

    # ---------- TEMP PASSWORD ACTION ----------

    @admin.action(
        description="Generate temporary passwords (CSV) & require change"
    )
    def generate_temporary_passwords(self, request, queryset):
        """
        Sets a random temporary password for selected users, marks them to change at next login,
        and returns a CSV with username/email/temp_password/staff_id.
        """
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = (
            f'attachment; filename="temporary_passwords_'
            f'{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
        )
        writer = csv.writer(resp)
        writer.writerow(
            [
                "username",
                "email",
                "staff_id",
                "temporary_password",
                "must_change_password",
            ]
        )

        count = 0
        for user in queryset:
            temp = _gen_temp_password()
            user.set_password(temp)
            user.must_change_password = True
            user.temp_password_set_at = timezone.now()
            user.save(
                update_fields=[
                    "password",
                    "must_change_password",
                    "temp_password_set_at",
                ]
            )
            writer.writerow(
                [
                    user.username,
                    user.email,
                    user.employee_id or "",
                    temp,
                    "yes",
                ]
            )
            count += 1

        messages.success(
            request, f"Temporary passwords generated for {count} user(s)."
        )
        return resp

    # ---------- OTP ACTIONS ----------

    @admin.action(description="Generate OTP codes (CSV, 15 min expiry)")
    def generate_login_otps(self, request, queryset):
        """
        Generate OTP codes for selected users, valid for 15 minutes.
        Returns a CSV with the OTPs (for manual sending or debugging).
        In production you would normally *send* the OTP via email/SMS instead.
        """
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = (
            f'attachment; filename="user_otps_'
            f'{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
        )
        writer = csv.writer(resp)
        writer.writerow(
            ["username", "email", "staff_id", "otp_code", "otp_expires_at"]
        )

        expiry = timezone.now() + timezone.timedelta(minutes=15)
        count = 0

        for user in queryset:
            code = _gen_otp_code()
            user.otp_code = code
            user.otp_expires_at = expiry
            user.save(update_fields=["otp_code", "otp_expires_at"])
            writer.writerow(
                [
                    user.username,
                    user.email,
                    user.employee_id or "",
                    code,
                    expiry.isoformat(),
                ]
            )
            count += 1

        messages.success(request, f"OTP codes generated for {count} user(s).")
        return resp

    @admin.action(description="Clear OTP codes")
    def clear_otps(self, request, queryset):
        updated = queryset.update(otp_code=None, otp_expires_at=None)
        messages.success(
            request, f"Cleared OTP data for {updated} user(s)."
        )

    # ---------- SIMPLE STATUS ACTIONS ----------

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
        messages.success(
            request, f"Cleared flag for {updated} user(s)."
        )


@admin.register(SecurityIncident)
class SecurityIncidentAdmin(admin.ModelAdmin):
    def reported_agency(self, obj):
        if obj.reported_by and obj.reported_by.agency:
            return (
                obj.reported_by.agency.code
                or obj.reported_by.agency.name
            )
        return ""

    reported_agency.short_description = "Agency"

    def reported_staff_id(self, obj):
        if obj.reported_by:
            return obj.reported_by.employee_id or ""
        return ""

    reported_staff_id.short_description = "Staff ID"

    list_display = (
        "title",
        "severity",
        "location",
        "reported_by",
        "reported_staff_id",
        "reported_agency",
        "reported_at",
        "resolved",
        "resolved_by",
        "resolved_at",
    )
    list_filter = (
        "severity",
        "resolved",
        "reported_at",
        "reported_by__agency",
    )
    search_fields = (
        "title",
        "description",
        "location",
        "reported_by__username",
        "reported_by__employee_id",
        "reported_by__agency__name",
        "reported_by__agency__code",
    )
    date_hierarchy = "reported_at"
    actions = ["export_csv", "mark_resolved", "mark_unresolved"]
    ordering = ("-reported_at",)

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp[
            "Content-Disposition"
        ] = 'attachment; filename="security_incidents.csv"'
        writer = csv.writer(resp)
        writer.writerow(
            [
                "Title",
                "Severity",
                "Location",
                "Reported By",
                "Reported By Staff ID",
                "Reported By Agency",
                "Reported At (UTC)",
                "Resolved",
                "Resolved By",
                "Resolved At (UTC)",
            ]
        )
        for i in queryset.select_related(
            "reported_by", "resolved_by", "reported_by__agency"
        ):
            writer.writerow(
                [
                    i.title,
                    i.severity,
                    i.location,
                    getattr(i.reported_by, "username", "") or "",
                    getattr(i.reported_by, "employee_id", "") or "",
                    getattr(
                        getattr(i.reported_by, "agency", None), "code", ""
                    )
                    or getattr(
                        getattr(i.reported_by, "agency", None), "name", ""
                    ),
                    timezone.localtime(i.reported_at).isoformat()
                    if i.reported_at
                    else "",
                    "Yes" if i.resolved else "No",
                    getattr(i.resolved_by, "username", "") or "",
                    timezone.localtime(i.resolved_at).isoformat()
                    if i.resolved_at
                    else "",
                ]
            )
        return resp

    @admin.action(description="Mark as resolved")
    def mark_resolved(self, request, queryset):
        # Note: for FKs, update should use *_id, but leaving as-is
        queryset.update(
            resolved=True,
            resolved_by=request.user,
            resolved_at=timezone.now(),
        )

    @admin.action(description="Mark as unresolved")
    def mark_unresolved(self, request, queryset):
        queryset.update(resolved=False, resolved_by=None, resolved_at=None)


@admin.register(EmployeeIDCardRequest)
class EmployeeIDCardRequestAdmin(admin.ModelAdmin):
    """
    Admin for Staff ID Card requests (HR flow).
    Shows Staff ID, Agency, and full lifecycle: requested → approved → printed → issued.
    """

    # Small helpers so the list is nice & readable
    def staff_id(self, obj):
        return obj.for_user.employee_id or ""

    staff_id.short_description = "Staff ID"

    def employee_name(self, obj):
        u = obj.for_user
        if not u:
            return "-"
        full = f"{u.first_name} {u.last_name}".strip()
        return full or u.username

    employee_name.short_description = "Employee"

    def agency(self, obj):
        u = obj.for_user
        if u and u.agency:
            return u.agency.code or u.agency.name
        return ""

    agency.short_description = "Agency"

    def requested_by_name(self, obj):
        u = obj.requested_by
        if not u:
            return ""
        full = f"{u.first_name} {u.last_name}".strip()
        return full or u.username

    requested_by_name.short_description = "Requested By"

    list_display = (
        "id",
        "employee_name",
        "staff_id",
        "agency",
        "request_type",
        "status",
        "requested_by_name",
        "created_at",
        "approved_at",
        "printed_at",
        "issued_at",
    )

    list_filter = (
        "status",
        "request_type",
        "for_user__agency",
        "created_at",
        "approved_at",
        "printed_at",
        "issued_at",
    )

    search_fields = (
        "for_user__username",
        "for_user__first_name",
        "for_user__last_name",
        "for_user__employee_id",
        "for_user__agency__code",
        "for_user__agency__name",
        "requested_by__username",
        "requested_by__first_name",
        "requested_by__last_name",
        "reason",
    )

    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    readonly_fields = (
        "created_at",
        "approved_at",
        "printed_at",
        "issued_at",
        "request_type",
        "for_user",
        "requested_by",
    )

    actions = [
        "action_mark_approved",
        "action_mark_printed",
        "action_mark_issued",
        "export_to_csv",
    ]

    @admin.action(description="Mark selected requests as APPROVED")
    def action_mark_approved(self, request, queryset):
        count = 0
        for obj in queryset:
            if hasattr(obj, "mark_approved"):
                obj.mark_approved(request.user)
            else:
                obj.status = "approved"
                obj.approver = request.user
                obj.approved_at = timezone.now()
                obj.save()
            count += 1
        messages.success(
            request, f"{count} request(s) marked as approved."
        )

    @admin.action(description="Mark selected requests as PRINTED")
    def action_mark_printed(self, request, queryset):
        count = 0
        for obj in queryset:
            if hasattr(obj, "mark_printed"):
                obj.mark_printed(request.user)
            else:
                obj.status = "printed"
                obj.printed_by = request.user
                obj.printed_at = timezone.now()
                obj.save()
            count += 1
        messages.success(
            request, f"{count} request(s) marked as printed."
        )

    @admin.action(description="Mark selected requests as ISSUED")
    def action_mark_issued(self, request, queryset):
        count = 0
        for obj in queryset:
            if hasattr(obj, "mark_issued"):
                obj.mark_issued(request.user)
            else:
                obj.status = "issued"
                obj.issued_by = request.user
                obj.issued_at = timezone.now()
                obj.save()
            count += 1
        messages.success(
            request, f"{count} request(s) marked as issued."
        )

    @admin.action(description="Export selected requests to CSV")
    def export_to_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp[
            "Content-Disposition"
        ] = 'attachment; filename="idcard_requests.csv"'
        writer = csv.writer(resp)
        writer.writerow(
            [
                "ID",
                "Employee Username",
                "Employee Name",
                "Staff ID",
                "Agency",
                "Request Type",
                "Status",
                "Requested By",
                "Reason",
                "Created At",
                "Approved At",
                "Printed At",
                "Issued At",
            ]
        )
        for obj in queryset.select_related(
            "for_user", "requested_by", "for_user__agency"
        ):
            u = obj.for_user
            full = ""
            username = ""
            staff_id = ""
            agency_code = ""

            if u:
                username = u.username
                full = (f"{u.first_name} {u.last_name}").strip() or u.username
                staff_id = u.employee_id or ""
                if u.agency:
                    agency_code = u.agency.code or u.agency.name

            writer.writerow(
                [
                    obj.pk,
                    username,
                    full,
                    staff_id,
                    agency_code,
                    obj.get_request_type_display()
                    if hasattr(obj, "get_request_type_display")
                    else obj.request_type,
                    obj.get_status_display()
                    if hasattr(obj, "get_status_display")
                    else obj.status,
                    self.requested_by_name(obj),
                    obj.reason or "",
                    obj.created_at.isoformat()
                    if obj.created_at
                    else "",
                    getattr(obj, "approved_at", "") or "",
                    getattr(obj, "printed_at", "") or "",
                    getattr(obj, "issued_at", "") or "",
                ]
            )
        return resp


# -------------------------------------------------------------------
# ROOM AMENITY ADMIN
# -------------------------------------------------------------------


@admin.register(RoomAmenity)
class RoomAmenityAdmin(admin.ModelAdmin):
    """
    Admin for reusable room amenities/features.
    Example: projector, video conferencing, whiteboard, etc.
    """
    list_display = ("name", "code", "icon_class", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "description")
    ordering = ("name",)


# -------------------------------------------------------------------
# ROOM / BOOKING / APPROVER ADMINS
# -------------------------------------------------------------------


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    """
    Admin for meeting rooms / spaces.
    Now with amenities support.
    """
    list_display = (
        "name",
        "code",
        "room_type",
        "location",
        "capacity",
        "is_active",
        "amenities_display",   # ✅ custom column
    )
    list_filter = (
        "room_type",
        "location",
        "is_active",
        "amenities",           # ✅ filter by amenity
    )
    search_fields = ("name", "code", "location", "description")
    filter_horizontal = (
        "amenities",           # ✅ nice multi-select widget
        "approvers",
    )

    def amenities_display(self, obj):
        """
        Show a comma-separated list of active amenities on the list page.
        Uses Room.amenities_for_display property.
        """
        names = [a.name for a in obj.amenities_for_display]
        return ", ".join(names) if names else "-"

    amenities_display.short_description = "Amenities"


@admin.register(RoomBooking)
class RoomBookingAdmin(admin.ModelAdmin):
    """
    Admin for room bookings.

    This version is SAFE even if your model does NOT have `start` / `end` fields.
    It uses helper methods instead of direct field names.
    """

    list_display = (
        "title",
        "room",
        "start_display",
        "end_display",
        "status",
        "requested_by_display",
    )

    # Keep filters conservative
    list_filter = ("status", "room")

    # Use only guaranteed fields for searching to avoid system check errors
    search_fields = ("id",)

    # Optional: make it easier to pick room and requester
    autocomplete_fields = ("room", "requested_by")

    # ------- Display helpers (these are allowed in list_display) -------

    def start_display(self, obj):
        """
        Try a few common datetime/time field names.
        """
        value = (
            getattr(obj, "start", None)
            or getattr(obj, "start_time", None)
            or getattr(obj, "start_datetime", None)
            or getattr(obj, "begin_at", None)
        )
        return value or "-"

    start_display.short_description = "Start"

    def end_display(self, obj):
        """
        Try a few common datetime/time field names.
        """
        value = (
            getattr(obj, "end", None)
            or getattr(obj, "end_time", None)
            or getattr(obj, "end_datetime", None)
            or getattr(obj, "finish_at", None)
        )
        return value or "-"

    end_display.short_description = "End"

    def requested_by_display(self, obj):
        """
        Show whoever requested the booking.
        We try both `requested_by` and `created_by`.
        """
        user = getattr(obj, "requested_by", None) or getattr(obj, "created_by", None)
        if not user:
            return "-"
        full = f"{user.first_name} {user.last_name}".strip()
        return full or user.username

    requested_by_display.short_description = "Requested by"


@admin.register(RoomApprover)
class RoomApproverAdmin(admin.ModelAdmin):
    """
    Admin for RoomApprover mapping (who can approve which room).
    """
    list_display = (
        "room",
        "user",
        "is_primary",
        "can_approve_all_agency",
        "is_active",
        "created_at",
    )
    list_filter = (
        "is_active",
        "is_primary",
        "can_approve_all_agency",
        "room",
    )
    search_fields = (
        "room__name",
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__employee_id",
    )


admin.site.register(AgencyServiceConfig)
admin.site.register(AgencyAssetRoles)
admin.site.register(Unit)
admin.site.register(AssetCategory)
admin.site.register(Asset)
admin.site.register(AssetRequest)
admin.site.register(AssetChangeRequest)
admin.site.register(ExitRequest)
admin.site.register(MobileLine)
admin.site.register(CellServiceFocalPoint)