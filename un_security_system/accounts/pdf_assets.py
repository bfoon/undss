# accounts/pdf_assets.py
import io
from dataclasses import dataclass
from typing import Iterable, Optional

from django.core.files.storage import default_storage
from django.conf import settings

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

import qrcode
from PIL import Image


@dataclass
class LabelSpec:
    # sticker size in mm
    w_mm: float = 70
    h_mm: float = 35
    # layout on A4
    cols: int = 3
    rows: int = 8
    margin_x_mm: float = 8
    margin_y_mm: float = 10
    gap_x_mm: float = 2.5
    gap_y_mm: float = 2.5


def _open_logo_reader(agency) -> Optional[ImageReader]:
    """
    Returns ImageReader for agency logo if available.
    Works for local storage. For remote storage, it falls back safely.
    """
    logo_field = getattr(agency, "logo", None)
    if not logo_field:
        return None
    try:
        # file exists locally
        if hasattr(logo_field, "path"):
            return ImageReader(logo_field.path)
    except Exception:
        pass

    # If storage is remote, try reading via storage API
    try:
        with default_storage.open(logo_field.name, "rb") as f:
            return ImageReader(io.BytesIO(f.read()))
    except Exception:
        return None


def _qr_image_reader_from_payload(payload: str, agency_logo_pil: Optional[Image.Image] = None) -> ImageReader:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # good for logo overlay
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")

    if agency_logo_pil:
        try:
            logo = agency_logo_pil.convert("RGBA")
            qr_w, qr_h = img.size
            target = int(min(qr_w, qr_h) * 0.22)
            logo.thumbnail((target, target), Image.LANCZOS)

            # white pad behind logo
            pad = int(target * 0.14)
            bg = Image.new("RGBA", (logo.size[0] + pad, logo.size[1] + pad), (255, 255, 255, 255))
            bx = (qr_w - bg.size[0]) // 2
            by = (qr_h - bg.size[1]) // 2
            img.alpha_composite(bg, (bx, by))

            x = (qr_w - logo.size[0]) // 2
            y = (qr_h - logo.size[1]) // 2
            img.alpha_composite(logo, (x, y))
        except Exception:
            pass

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _get_agency_logo_pil(agency) -> Optional[Image.Image]:
    logo_field = getattr(agency, "logo", None)
    if not logo_field:
        return None

    try:
        if hasattr(logo_field, "path"):
            return Image.open(logo_field.path)
    except Exception:
        pass

    try:
        with default_storage.open(logo_field.name, "rb") as f:
            return Image.open(io.BytesIO(f.read()))
    except Exception:
        return None


def _asset_payload(request, asset, include_url: bool = True) -> str:
    """
    QR payload: tag + (optional) URL to asset detail page
    """
    tag = getattr(asset, "asset_tag", "") or ""
    if not include_url:
        return tag
    try:
        from django.urls import reverse
        url = request.build_absolute_uri(reverse("accounts:asset_detail", args=[asset.id]))
        return f"{tag}\n{url}"
    except Exception:
        return tag


def _safe_qr_reader(request, asset, agency_logo_pil: Optional[Image.Image], include_url: bool = True) -> ImageReader:
    """
    Use stored asset.qr_code if available. Otherwise generate QR dynamically.
    """
    qr_field = getattr(asset, "qr_code", None)
    if qr_field:
        try:
            if qr_field.name and default_storage.exists(qr_field.name):
                with default_storage.open(qr_field.name, "rb") as f:
                    return ImageReader(io.BytesIO(f.read()))
        except Exception:
            pass

    payload = _asset_payload(request, asset, include_url=include_url)
    return _qr_image_reader_from_payload(payload, agency_logo_pil=agency_logo_pil)


def build_asset_labels_pdf(
    request,
    assets: Iterable,
    agency,
    mode: str = "a4",  # "a4" or "sticker"
    spec: Optional[LabelSpec] = None,
    include_url_in_qr: bool = True,
) -> bytes:
    """
    mode:
      - "a4": grid of labels on A4 (recommended for batch audits)
      - "sticker": single label per page (for sticker printer workflows)
    """
    if spec is None:
        spec = LabelSpec()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4

    logo_reader = _open_logo_reader(agency)
    logo_pil = _get_agency_logo_pil(agency)

    # label box sizes in points
    label_w = spec.w_mm * mm
    label_h = spec.h_mm * mm

    margin_x = spec.margin_x_mm * mm
    margin_y = spec.margin_y_mm * mm
    gap_x = spec.gap_x_mm * mm
    gap_y = spec.gap_y_mm * mm

    cols = spec.cols
    rows = spec.rows

    def draw_label(x, y, asset):
        """
        Draw one label anchored at bottom-left (x,y).
        """
        # border
        c.setLineWidth(0.6)
        c.setStrokeColorRGB(0.1, 0.25, 0.35)
        c.roundRect(x, y, label_w, label_h, 6, stroke=1, fill=0)

        # header band
        c.setFillColorRGB(0.92, 0.96, 0.98)
        c.rect(x, y + label_h - (8 * mm), label_w, (8 * mm), stroke=0, fill=1)

        # logo
        if logo_reader:
            try:
                c.drawImage(
                    logo_reader,
                    x + (2.5 * mm),
                    y + label_h - (7.2 * mm),
                    width=(6.5 * mm),
                    height=(6.5 * mm),
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception:
                pass

        # agency name (small)
        c.setFillColorRGB(0.05, 0.2, 0.3)
        c.setFont("Helvetica-Bold", 7.5)
        agency_name = getattr(agency, "name", "Agency")
        c.drawString(x + (10 * mm), y + label_h - (5.7 * mm), agency_name[:38])

        # asset name
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 8.8)
        asset_name = (getattr(asset, "name", "") or "")[:36]
        c.drawString(x + (2.5 * mm), y + label_h - (12.5 * mm), asset_name)

        # tag + meta
        c.setFont("Helvetica", 7.5)
        tag = getattr(asset, "asset_tag", "") or "-"
        cat = getattr(getattr(asset, "category", None), "name", "") or ""
        unit = getattr(getattr(asset, "unit", None), "name", "") or "Unallocated/Core"
        c.setFillColorRGB(0.15, 0.15, 0.15)
        c.drawString(x + (2.5 * mm), y + label_h - (17.3 * mm), f"TAG: {tag}")
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(x + (2.5 * mm), y + label_h - (21.2 * mm), f"{cat[:22]} • {unit[:22]}")

        # QR on right
        qr_reader = _safe_qr_reader(request, asset, logo_pil, include_url=include_url_in_qr)
        qr_size = 20 * mm
        c.drawImage(
            qr_reader,
            x + label_w - qr_size - (2.5 * mm),
            y + (2.5 * mm),
            width=qr_size,
            height=qr_size,
            preserveAspectRatio=True,
            mask="auto",
        )

        # bottom tiny footer
        c.setFont("Helvetica", 6.5)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(x + (2.5 * mm), y + (3.0 * mm), "Scan to verify / audit")

    assets_list = list(assets)

    if mode == "sticker":
        # one label per page centered
        for asset in assets_list:
            x = (page_w - label_w) / 2
            y = (page_h - label_h) / 2
            draw_label(x, y, asset)
            c.showPage()
    else:
        # A4 grid
        x0 = margin_x
        y0_top = page_h - margin_y - label_h

        idx = 0
        while idx < len(assets_list):
            for r in range(rows):
                for col in range(cols):
                    if idx >= len(assets_list):
                        break
                    x = x0 + col * (label_w + gap_x)
                    y = y0_top - r * (label_h + gap_y)

                    # stop if out of page bounds
                    if x + label_w > page_w - margin_x + 1 or y < margin_y - 1:
                        continue

                    draw_label(x, y, assets_list[idx])
                    idx += 1

            c.showPage()

    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf
