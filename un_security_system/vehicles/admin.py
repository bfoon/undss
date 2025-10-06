# vehicles/admin.py
from django.contrib import admin
from django.http import HttpResponse
import csv
from .models import Vehicle, VehicleMovement, ParkingCard, ParkingCardRequest

# Try to import optional models without crashing
try:
    from .models import AssetExit, AssetExitItem
except Exception:
    AssetExit = None
    AssetExitItem = None

def _get(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default

@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("plate_number", "vehicle_type", "make", "model", "color", "un_agency_safe", "parking_card_safe")
    list_filter = ("vehicle_type",) if hasattr(Vehicle, "vehicle_type") else ()
    search_fields = ("plate_number",) if hasattr(Vehicle, "plate_number") else ()

    def un_agency_safe(self, obj):
        return _get(obj, "un_agency", default="—")
    un_agency_safe.short_description = "UN agency"

    def parking_card_safe(self, obj):
        pc = _get(obj, "parking_card", default=None)
        return getattr(pc, "card_number", str(pc)) if pc else "—"
    parking_card_safe.short_description = "Parking card"

@admin.register(VehicleMovement)
class VehicleMovementAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "movement_type", "gate", "driver_name", "timestamp", "recorded_by")
    list_filter = tuple(f for f in ("movement_type", "gate") if hasattr(VehicleMovement, f))
    search_fields = ("vehicle__plate_number", "driver_name") if hasattr(VehicleMovement, "driver_name") else ("vehicle__plate_number",)
    actions = ["export_csv"]

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="vehicle_movements.csv"'
        w = csv.writer(resp)
        w.writerow(["Plate", "Type", "Gate", "Driver", "Purpose", "Notes", "Timestamp", "Recorded by"])
        for m in queryset.select_related("vehicle", "recorded_by"):
            w.writerow([
                m.vehicle.plate_number if getattr(m, "vehicle_id", None) else "",
                _get(m, "movement_type", default=""),
                _get(m, "gate", default=""),
                _get(m, "driver_name", default=""),
                _get(m, "purpose", default=""),
                _get(m, "notes", default=""),
                _get(m, "timestamp", default=""),
                m.recorded_by.username if getattr(m, "recorded_by_id", None) else "",
            ])
        return resp

@admin.register(ParkingCard)
class ParkingCardAdmin(admin.ModelAdmin):
    list_display = ("card_number", "owner_name", "department_safe", "vehicle_plate", "expiry_date", "is_active")
    list_filter = ("is_active",) if hasattr(ParkingCard, "is_active") else ()
    search_fields = ("card_number", "owner_name", "vehicle_plate") if hasattr(ParkingCard, "card_number") else ()
    actions = ["export_csv", "deactivate_selected", "activate_selected"]
    date_hierarchy = "expiry_date" if hasattr(ParkingCard, "_meta") and "expiry_date" in [f.name for f in ParkingCard._meta.get_fields() if hasattr(f, "name")] else None

    def department_safe(self, obj):
        return _get(obj, "department", default="—")
    department_safe.short_description = "Department"

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="parking_cards.csv"'
        w = csv.writer(resp)
        w.writerow(["Card No.", "Owner", "Owner ID", "Phone", "Dept", "Vehicle Plate", "Expiry", "Active"])
        for c in queryset:
            w.writerow([
                _get(c, "card_number", default=""),
                _get(c, "owner_name", default=""),
                _get(c, "owner_id", default=""),
                _get(c, "phone", default=""),
                _get(c, "department", default=""),
                _get(c, "vehicle_plate", default=""),
                _get(c, "expiry_date", default=""),
                "Yes" if _get(c, "is_active", default=False) else "No"
            ])
        return resp

    @admin.action(description="Deactivate selected")
    def deactivate_selected(self, request, queryset):
        if hasattr(queryset.model, "is_active"):
            queryset.update(is_active=False)

    @admin.action(description="Activate selected")
    def activate_selected(self, request, queryset):
        if hasattr(queryset.model, "is_active"):
            queryset.update(is_active=True)

# Optional: Asset Exit admin (guarded; only registers if model exists)
if AssetExit:
    class AssetExitItemInline(admin.TabularInline):
        model = AssetExitItem
        extra = 0

    @admin.register(AssetExit)
    class AssetExitAdmin(admin.ModelAdmin):
        list_display = (
            "code_safe",
            "agency_name_safe",
            "requested_by_safe",
            "status_safe",
            "created_at_safe",
            "lsa_user_safe",
            "signed_out_at_safe",
            "signed_in_at_safe",
        )
        # Only filter on very likely field
        list_filter = ("status",) if hasattr(AssetExit, "status") else ()
        search_fields = ()
        inlines = [AssetExitItemInline] if AssetExitItem else []
        actions = ["export_csv"]

        # ---- safe getters for list_display ----
        def code_safe(self, obj): return _get(obj, "code", default="—")
        code_safe.short_description = "Code"

        def agency_name_safe(self, obj): return _get(obj, "agency_name", default="—")
        agency_name_safe.short_description = "Agency"

        def requested_by_safe(self, obj):
            u = _get(obj, "requested_by", default=None)
            return getattr(u, "username", str(u)) if u else "—"
        requested_by_safe.short_description = "Requested by"

        def status_safe(self, obj): return _get(obj, "status", default="—")
        status_safe.short_description = "Status"

        def created_at_safe(self, obj): return _get(obj, "created_at", default="—")
        created_at_safe.short_description = "Created"

        def lsa_user_safe(self, obj):
            u = _get(obj, "lsa_user", default=None)
            return getattr(u, "username", str(u)) if u else "—"
        lsa_user_safe.short_description = "LSA"

        def signed_out_at_safe(self, obj): return _get(obj, "signed_out_at", default="—")
        signed_out_at_safe.short_description = "Signed out"

        def signed_in_at_safe(self, obj): return _get(obj, "signed_in_at", default="—")
        signed_in_at_safe.short_description = "Signed in"

        @admin.action(description="Export selected to CSV")
        def export_csv(self, request, queryset):
            resp = HttpResponse(content_type="text/csv")
            resp["Content-Disposition"] = 'attachment; filename="asset_exits.csv"'
            w = csv.writer(resp)
            w.writerow(["Code", "Agency", "Requested by", "Status", "Created", "LSA", "Signed out", "Signed in"])
            for ax in queryset:
                w.writerow([
                    _get(ax, "code", default=""),
                    _get(ax, "agency_name", default=""),
                    getattr(_get(ax, "requested_by", default=None), "username", ""),
                    _get(ax, "status", default=""),
                    _get(ax, "created_at", default=""),
                    getattr(_get(ax, "lsa_user", default=None), "username", ""),
                    _get(ax, "signed_out_at", default=""),
                    _get(ax, "signed_in_at", default=""),
                ])
            return resp

@admin.register(ParkingCardRequest)
class ParkingCardRequestAdmin(admin.ModelAdmin):
    list_display = ('id','owner_name','vehicle_plate','status','requested_by','requested_at','decided_by','decided_at')
    list_filter = ('status','requested_at','decided_at','department')
    search_fields = ('owner_name','owner_id','vehicle_plate','requested_by__username','department')
    date_hierarchy = 'requested_at'