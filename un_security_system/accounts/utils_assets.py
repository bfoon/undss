import io
import random
from PIL import Image
import qrcode
from django.core.files.base import ContentFile
from django.urls import reverse

def generate_unique_asset_tag(agency, prefix="AST", length=6, AssetModel=None):
    """
    Generates a unique tag per agency:
      AST-GMB-000123 (if agency code exists)
      or AST-000123
    """
    if AssetModel is None:
        raise ValueError("AssetModel is required")

    agency_code = getattr(agency, "code", None)  # optional
    base_prefix = f"{prefix}-{agency_code}" if agency_code else prefix

    for _ in range(100):  # safe attempts
        digits = "".join(str(random.randint(0, 9)) for _ in range(length))
        tag = f"{base_prefix}-{digits}"
        exists = AssetModel.objects.filter(agency=agency, asset_tag=tag).exists()
        if not exists:
            return tag

    raise RuntimeError("Failed to generate unique asset tag after many attempts.")


def build_qr_payload(request, asset, include_url=True):
    """
    QR payload can be just the tag or tag+URL.
    """
    tag = asset.asset_tag or ""
    if not include_url:
        return tag

    url = request.build_absolute_uri(reverse("accounts:asset_detail", args=[asset.id]))
    # payload format (simple + audit friendly)
    return f"{tag}\n{url}"


def generate_qr_image(payload: str, agency_logo_path: str | None = None) -> Image.Image:
    """
    Generates QR and overlays center logo (agency logo) if provided.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # high because logo overlay
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    if agency_logo_path:
        try:
            logo = Image.open(agency_logo_path).convert("RGBA")
            # resize logo relative to QR
            qr_w, qr_h = img.size
            target = int(min(qr_w, qr_h) * 0.22)  # 22% of QR size
            logo.thumbnail((target, target), Image.LANCZOS)

            # center
            x = (qr_w - logo.size[0]) // 2
            y = (qr_h - logo.size[1]) // 2

            # optional: put white pad behind logo for clarity
            pad = int(target * 0.12)
            bg = Image.new("RGBA", (logo.size[0] + pad, logo.size[1] + pad), (255, 255, 255, 255))
            bx = (qr_w - bg.size[0]) // 2
            by = (qr_h - bg.size[1]) // 2
            img.alpha_composite(bg, (bx, by))
            img.alpha_composite(logo, (x, y))
        except Exception:
            # silently fall back to QR without logo
            pass

    return img

def save_qr_to_asset(asset, qr_img: Image.Image, filename_prefix="qr"):
    """
    Saves PIL image to Asset.qr_code
    """
    buffer = io.BytesIO()
    qr_img.save(buffer, format="PNG")
    buffer.seek(0)
    fname = f"{filename_prefix}_{asset.id}.png"
    asset.qr_code.save(fname, ContentFile(buffer.getvalue()), save=False)




def get_manager_emails_for_request(req) -> list[str]:
    """
    Unit Head + Unit Asset Managers OR Operations Manager (for core/unallocated).
    """
    emails = []

    # core/unallocated -> ops manager
    manager = req.get_required_manager()
    if manager and getattr(manager, "email", None):
        emails.append(manager.email)

    # if unit exists, include unit asset managers as backup
    if req.unit:
        for u in req.unit.asset_managers.all():
            if u.email:
                emails.append(u.email)

    # unique
    return sorted(set(emails))


def get_ict_custodian_emails(req=None, agency=None) -> list[str]:
    """
    Agency ICT custodians + ict_focal fallback.
    Accepts either:
      - req (must have .agency)
      - agency (direct)
    """
    emails = []

    # Resolve agency safely
    if agency is None and req is not None:
        agency = getattr(req, "agency", None)

    if agency is None:
        return []

    roles = getattr(agency, "asset_roles", None)

    if roles:
        for u in roles.ict_custodian.all():
            if u.email:
                emails.append(u.email)

    # fallback: if you have a known "ict_focal" role users in your User model
    try:
        ict_users = agency.users.filter(role="ict_focal")
        for u in ict_users:
            if u.email:
                emails.append(u.email)
    except Exception:
        pass

    return sorted(set(emails))


def get_manager_emails_for_asset(asset):
    emails = set()

    # core/unallocated -> ops manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        roles = getattr(asset.agency, "asset_roles", None)
        if roles and roles.operations_manager and roles.operations_manager.email:
            emails.add(roles.operations_manager.email)
        return list(emails)

    # unit head + asset managers
    if asset.unit:
        if asset.unit.unit_head and asset.unit.unit_head.email:
            emails.add(asset.unit.unit_head.email)
        for u in asset.unit.asset_managers.all():
            if u.email:
                emails.add(u.email)

    return list(emails)


def can_user_approve_asset_change(user, asset, agency_roles) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    # Core/unallocated -> operations manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        return bool(agency_roles and agency_roles.operations_manager_id == user.id)

    # unit head or asset manager for that unit
    if asset.unit and asset.unit.unit_head_id == user.id:
        return True
    if asset.unit and asset.unit.asset_managers.filter(id=user.id).exists():
        return True

    return False

# -------------------------------------------------------------------
# Return approval helper (Manager / Ops / Superuser)
# -------------------------------------------------------------------
def can_user_approve_return(user, agency_roles, asset) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True

    # Core/unallocated assets -> Ops Manager
    if asset.unit is None or getattr(asset.unit, "is_core_unit", False):
        return bool(agency_roles and agency_roles.operations_manager_id == user.id)

    # Unit assets -> unit head or asset manager
    if asset.unit and asset.unit.unit_head_id == user.id:
        return True
    if asset.unit and asset.unit.asset_managers.filter(id=user.id).exists():
        return True

    return False

