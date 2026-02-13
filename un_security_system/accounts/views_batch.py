# assets/views_batch.py
import csv
import io
from datetime import datetime
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction, models
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_GET

from .models import Asset, AssetCategory, Unit, MobileLine  # adjust import path

User = get_user_model()


# ---------- helpers ----------

def _is_ict(user):
    # You already use role="ict_focal" in your system :contentReference[oaicite:2]{index=2}
    return user.is_superuser or getattr(user, "role", "") in ("ict_focal", "lsa", "soc")

def _parse_date(value: str):
    """
    Accepts:
    - DD/MM/YYYY  (preferred) e.g. 01/01/2024
    - DD-MM-YYYY
    - YYYY-MM-DD  (fallback)
    """
    value = (value or "").strip()
    if not value:
        return None

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    return None

def _clean(value):
    return (value or "").strip()

def _find_user_in_agency(agency_id, identifier: str):
    """
    identifier can be username OR email OR employee_id.
    """
    identifier = _clean(identifier)
    if not identifier:
        return None
    return User.objects.filter(
        agency_id=agency_id
    ).filter(
        models.Q(username__iexact=identifier) |
        models.Q(email__iexact=identifier) |
        models.Q(employee_id__iexact=identifier)
    ).first()


# ---------- templates ----------

@require_GET
def download_csv_template(request, kind: str):
    if not request.user.is_authenticated:
        return redirect("login")

    if not _is_ict(request.user):
        messages.error(request, "You do not have permission to download batch templates.")
        return redirect("accounts:asset_management")  # adjust

    kind = (kind or "").lower().strip()

    if kind == "assets":
        filename = "assets_template.csv"
        headers = [
            "name",                 # required
            "category_name",        # required (match AssetCategory.name)
            "unit_name",            # optional (match Unit.name)
            "serial_number",        # optional
            "asset_tag",            # optional
            "status",               # optional: available/assigned/maintenance/retired
            "acquired_at",          # optional: YYYY-MM-DD
            "current_holder",       # optional: username/email/employee_id (same agency)
        ]
        example_row = [
            "HP EliteBook 840 G7",
            "Laptop",
            "ICT",
            "SN-ABC-001",
            "AST-000123",
            "available",
            "2026-01-15",
            "",
        ]

    elif kind == "mobile-lines":
        filename = "mobile_lines_template.csv"
        headers = [
            "msisdn",              # required (unique phone number) :contentReference[oaicite:3]{index=3}
            "line_type",           # required: sim/data/sim_data :contentReference[oaicite:4]{index=4}
            "provider",            # optional (QCell/Africell/etc.) :contentReference[oaicite:5]{index=5}
            "sim_serial",          # optional :contentReference[oaicite:6]{index=6}
            "status",              # optional: available/assigned/suspended/retired :contentReference[oaicite:7]{index=7}
            "assigned_to",         # optional: username/email/employee_id (same agency)
            "notes",               # optional :contentReference[oaicite:8]{index=8}
        ]
        example_row = [
            "+220 3XX XXX1",
            "sim_data",
            "QCell",
            "SIM-99887766",
            "available",
            "",
            "Voice + monthly data bundle",
        ]
    else:
        messages.error(request, "Unknown template type.")
        return redirect("accounts:asset_management")  # adjust

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(headers)
    writer.writerow(example_row)
    return response


# ---------- upload + import ----------

@require_http_methods(["GET", "POST"])
def batch_upload_csv(request, kind: str):
    if not request.user.is_authenticated:
        return redirect("login")

    if not _is_ict(request.user):
        messages.error(request, "You do not have permission to batch upload.")
        return redirect("accounts:asset_management")  # adjust

    if request.method == "GET":
        return render(request, "accounts/assets/batch_upload.html", {"kind": kind})

    # POST
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Please choose a CSV file to upload.")
        return redirect(request.path)

    if not upload.name.lower().endswith(".csv"):
        messages.error(request, "Only CSV files are supported.")
        return redirect(request.path)

    agency = request.user.agency
    if not agency:
        messages.error(request, "Your user account is not linked to an agency.")
        return redirect(request.path)

    raw = upload.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))

    kind = (kind or "").lower().strip()

    created_count = 0
    error_rows = []  # list of dicts (original row + error)

    # small caches for speed
    categories = {c.name.lower(): c for c in AssetCategory.objects.filter(agency=agency)}
    units = {u.name.lower(): u for u in Unit.objects.filter(agency=agency)}

    with transaction.atomic():
        for i, row in enumerate(reader, start=2):  # start=2 because header is row 1
            try:
                if kind == "assets":
                    _import_asset_row(row, agency, categories, units)
                elif kind == "mobile-lines":
                    _import_mobile_line_row(row, agency)
                else:
                    raise ValueError("Unknown upload kind.")

                created_count += 1

            except Exception as e:
                error_rows.append({**row, "error": f"Row {i}: {str(e)}"})

    if error_rows:
        # Return an “errors CSV” for quick fixing
        return _errors_csv_response(kind, error_rows, created_count)

    messages.success(request, f"Upload completed: {created_count} record(s) created.")
    return redirect("accounts:asset_management")  # adjust


def _import_asset_row(row, agency, categories, units):
    name = _clean(row.get("name"))
    category_name = _clean(row.get("category_name"))
    if not name:
        raise ValueError("Missing required field: name")
    if not category_name:
        raise ValueError("Missing required field: category_name")

    category = categories.get(category_name.lower())
    if not category:
        raise ValueError(f"Invalid category_name '{category_name}' (must exist for your agency).")

    unit_name = _clean(row.get("unit_name"))
    unit = units.get(unit_name.lower()) if unit_name else None
    if unit_name and not unit:
        raise ValueError(f"Invalid unit_name '{unit_name}' (must exist for your agency).")

    status = _clean(row.get("status")) or "available"
    allowed_status = {"available", "assigned", "maintenance", "retired"}
    if status not in allowed_status:
        raise ValueError(f"Invalid status '{status}'. Allowed: {', '.join(sorted(allowed_status))}")

    acquired_at = _parse_date(row.get("acquired_at"))
    if row.get("acquired_at") and not acquired_at:
        raise ValueError("Invalid acquired_at date. Use DD/MM/YYYY (e.g., 01/01/2024).")

    serial_number = _clean(row.get("serial_number")) or None
    asset_tag = _clean(row.get("asset_tag")) or None

    # optional current_holder: must be in same agency
    holder_identifier = _clean(row.get("current_holder"))
    current_holder = None
    if holder_identifier:
        current_holder = User.objects.filter(agency=agency).filter(
            models.Q(username__iexact=holder_identifier) |
            models.Q(email__iexact=holder_identifier) |
            models.Q(employee_id__iexact=holder_identifier)
        ).first()
        if not current_holder:
            raise ValueError(f"current_holder '{holder_identifier}' not found in your agency.")

    Asset.objects.create(
        agency=agency,
        category=category,
        unit=unit,
        name=name,
        serial_number=serial_number,
        asset_tag=asset_tag,
        status=status,
        acquired_at=acquired_at,
        current_holder=current_holder,
    )


def _import_mobile_line_row(row, agency):
    msisdn = _clean(row.get("msisdn"))
    line_type = _clean(row.get("line_type"))
    if not msisdn:
        raise ValueError("Missing required field: msisdn")
    if not line_type:
        raise ValueError("Missing required field: line_type")

    allowed_types = {"sim", "data", "sim_data"}
    if line_type not in allowed_types:
        raise ValueError(f"Invalid line_type '{line_type}'. Allowed: {', '.join(sorted(allowed_types))}")

    status = _clean(row.get("status")) or "available"
    allowed_status = {"available", "assigned", "suspended", "retired"}
    if status not in allowed_status:
        raise ValueError(f"Invalid status '{status}'. Allowed: {', '.join(sorted(allowed_status))}")

    provider = _clean(row.get("provider"))
    sim_serial = _clean(row.get("sim_serial"))
    notes = _clean(row.get("notes"))

    assigned_to_identifier = _clean(row.get("assigned_to"))
    assigned_to = None
    if assigned_to_identifier:
        assigned_to = User.objects.filter(agency=agency).filter(
            models.Q(username__iexact=assigned_to_identifier) |
            models.Q(email__iexact=assigned_to_identifier) |
            models.Q(employee_id__iexact=assigned_to_identifier)
        ).first()
        if not assigned_to:
            raise ValueError(f"assigned_to '{assigned_to_identifier}' not found in your agency.")

    # msisdn is unique in your model :contentReference[oaicite:9]{index=9}
    MobileLine.objects.create(
        agency=agency,
        line_type=line_type,
        provider=provider,
        msisdn=msisdn,
        sim_serial=sim_serial,
        status=status,
        assigned_to=assigned_to,
        custodian=getattr(agency, "asset_roles", None) and None,  # optional: you can set custodian=request.user
        notes=notes,
        issued_at=timezone.now() if status == "assigned" else None,
    )


def _errors_csv_response(kind, error_rows, created_count):
    filename = f"{kind}_upload_errors.csv".replace("-", "_")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    # include error column + the original columns found in upload
    all_keys = set()
    for r in error_rows:
        all_keys.update(r.keys())
    # keep 'error' first
    fieldnames = ["error"] + sorted([k for k in all_keys if k != "error"])

    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    for r in error_rows:
        writer.writerow(r)

    # You can also add a header-like message by setting another header:
    response["X-Upload-Created"] = str(created_count)
    response["X-Upload-Errors"] = str(len(error_rows))
    return response
