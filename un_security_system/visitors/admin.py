# visitors/admin.py
from django.contrib import admin
from .models import Visitor, VisitorLog, VisitorCard
from django.http import HttpResponse
import csv

# --- helpers that won't crash if fields are missing ---
def _get(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default

class VisitorLogInline(admin.TabularInline):
    model = VisitorLog
    extra = 0
    # Only include fields we are confident exist
    readonly_fields = ("action", "timestamp", "performed_by")
    fields = ("action", "timestamp", "performed_by")
    can_delete = False
    show_change_link = False

@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    """
    Safe admin that works even if your model uses different field names.
    We render via methods, not model fields, to avoid admin.E108.
    """
    list_display = (
        "full_name_safe",
        "organization_safe",
        "purpose_safe",
        "visit_date_safe",
        "checked_in_safe",
        "checked_out_safe",
        "created_by_safe",
    )
    # Use only boolean fields we're confident about; remove unknowns
    list_filter = ("checked_in", "checked_out",) if hasattr(Visitor, "checked_in") and hasattr(Visitor, "checked_out") else ()
    # Avoid search_fields pointing to non-existent fields
    search_fields = ()
    inlines = [VisitorLogInline]

    # ----- safe getters -----
    def full_name_safe(self, obj):
        return _get(obj, "full_name", "__str__", default="-")
    full_name_safe.short_description = "Full name"

    def organization_safe(self, obj):
        return _get(obj, "organization", default="—")
    organization_safe.short_description = "Organization"

    def purpose_safe(self, obj):
        return _get(obj, "purpose", "reason", default="—")
    purpose_safe.short_description = "Purpose"

    def visit_date_safe(self, obj):
        val = _get(obj, "visit_date", "date", default=None)
        return val or "—"
    visit_date_safe.short_description = "Visit date"

    def created_by_safe(self, obj):
        u = _get(obj, "created_by", "requested_by", default=None)
        if u:
            return getattr(u, "username", str(u))
        return "—"
    created_by_safe.short_description = "Created by"

    def checked_in_safe(self, obj):
        val = _get(obj, "checked_in", default=None)
        if val is True:
            return "Yes"
        if val is False:
            return "No"
        return "—"
    checked_in_safe.short_description = "Checked in"

    def checked_out_safe(self, obj):
        val = _get(obj, "checked_out", default=None)
        if val is True:
            return "Yes"
        if val is False:
            return "No"
        return "—"
    checked_out_safe.short_description = "Checked out"

    # Optional: a very safe CSV export that won't crash
    actions = ["export_csv_safe"]

    @admin.action(description="Export selected to CSV (safe)")
    def export_csv_safe(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="visitors.csv"'
        w = csv.writer(resp)
        w.writerow(["Full name", "Organization", "Purpose", "Visit date", "Phone", "Email", "Vehicle plate", "Checked in", "Checked out"])
        for v in queryset:
            w.writerow([
                _get(v, "full_name", "__str__", default=""),
                _get(v, "organization", default=""),
                _get(v, "purpose", "reason", default=""),
                _get(v, "visit_date", "date", default=""),
                _get(v, "phone", default=""),
                _get(v, "email", default=""),
                _get(v, "vehicle_plate", default=""),
                "Yes" if _get(v, "checked_in", default=False) else "No",
                "Yes" if _get(v, "checked_out", default=False) else "No",
            ])
        return resp

@admin.register(VisitorLog)
class VisitorLogAdmin(admin.ModelAdmin):
    # Only include fields we are confident exist on your model
    list_display = ("visitor", "action", "timestamp", "performed_by")
    list_filter = ("action",) if hasattr(VisitorLog, "action") else ()
    search_fields = ()
    date_hierarchy = "timestamp" if hasattr(VisitorLog, "_meta") and "timestamp" in [f.name for f in VisitorLog._meta.get_fields() if hasattr(f, "name")] else None

@admin.register(VisitorCard)
class VisitorCardAdmin(admin.ModelAdmin):
    list_display = ('number', 'is_active', 'in_use', 'issued_to', 'issued_at', 'returned_at')
    list_filter = ('is_active', 'in_use')
    search_fields = ('number', 'issued_to__full_name')