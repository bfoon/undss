# accounts/esign_formula_runtime.py
"""
UN PASS eSign — formula/serial runtime integration.

This module deliberately requires no database migration.

It does two things:

1. Recalculates formula fields after a FormSubmission has a real reference.
   That is what makes SERIAL(@reference, ...) stable for normal forms and for
   automatically generated receipts/documents.

2. Fixes PDF table footer calculations so "Count items" really counts items
   instead of the PDF renderer always summing numeric values.

Imported once from accounts.apps.AccountsConfig.ready().
"""

from __future__ import annotations

from copy import deepcopy

from django.db.models.signals import post_save
from django.dispatch import receiver

from . import form_formula_esign as FX
from . import form_pdf_esign as PDF
from .models_esign_studio import FormSubmission


@receiver(
    post_save,
    sender=FormSubmission,
    dispatch_uid="esign_formula_reference_recalculation_v2",
)
def recalculate_reference_formulas(sender, instance, **kwargs):
    """
    A submission reference only exists once the FormSubmission is being saved.

    Recompute formula fields with that reference and write the final values
    directly to the row. QuerySet.update() avoids recursively firing post_save.
    """
    reference = str(instance.reference or "").strip()
    if not reference:
        return

    schema = instance.schema or {}
    values = instance.values or {}

    try:
        calculated, _errors = FX.compute_values(
            schema,
            values,
            reference=reference,
        )
    except Exception:
        # A formula error must never stop the submission itself from saving.
        return

    if calculated != values:
        sender.objects.filter(pk=instance.pk).update(values=calculated)
        # Keep the in-memory object correct for code that continues using it
        # in the same request after save().
        instance.values = calculated


# ---------------------------------------------------------------------------
# PDF integration
# ---------------------------------------------------------------------------

_ORIGINAL_RENDER_FORM_PDF = PDF.render_form_pdf


def _table_has_footer(element):
    if element.get("show_total"):
        return True
    return any(
        str((column or {}).get("total") or "").strip()
        for column in (element.get("columns") or [])
    )


def render_form_pdf_with_reference_formulas(
    schema,
    values=None,
    *,
    reference="",
    submitted_at=None,
    submitter="",
    blank=False,
    status_label="",
):
    """
    Recalculate values with @reference immediately before PDF rendering.

    This also means a generated receipt/invoice serial is correct even if its
    FormSubmission object was made by another workflow path.
    """
    schema_for_pdf = deepcopy(schema or {})

    # The legacy PDF renderer allocated a footer row only when show_total was
    # on. Explicit column totals (especially Count items) must also get one.
    for element in schema_for_pdf.get("elements") or []:
        if element.get("type") == "table" and _table_has_footer(element):
            element["show_total"] = True

    calculated = dict(values or {})
    try:
        calculated, _errors = FX.compute_values(
            schema_for_pdf,
            calculated,
            reference=reference or "",
        )
    except Exception:
        pass

    return _ORIGINAL_RENDER_FORM_PDF(
        schema_for_pdf,
        calculated,
        reference=reference,
        submitted_at=submitted_at,
        submitter=submitter,
        blank=blank,
        status_label=status_label,
    )


def _draw_table_with_modes(self, c, el, x, top, w):
    """
    Replacement for the legacy _Painter._draw_table.

    The old PDF code called table_total() for every numeric column, which always
    summed. This version uses FX.totals_for(), so Count/Average/Min/Max/Sum all
    match the browser and server.
    """
    from reportlab.lib import colors
    from reportlab.lib.utils import simpleSplit

    cols = el["columns"]
    units = sum(col["width"] for col in cols) or 1
    widths = [w * col["width"] / units for col in cols]
    rows = self._table_rows(el)
    row_h = 18.0

    c.setFillColor(PDF._tint(self.accent, 0.85))
    c.rect(x, top - row_h, w, row_h, stroke=0, fill=1)
    c.setFont(self.bold, 8)
    c.setFillColor(PDF._color(self.theme.get("header_bg"), "#005A8B"))

    cx = x
    for col, cw in zip(cols, widths):
        label = simpleSplit(col["label"], self.bold, 8, cw - 8)[0] if col["label"] else ""
        if col["kind"] == "number":
            c.drawRightString(cx + cw - 5, top - 12, label)
        else:
            c.drawString(cx + 5, top - 12, label)
        cx += cw

    c.setFont(self.regular, 8.8)
    for r_index, row in enumerate(rows):
        ry = top - row_h * (r_index + 2)
        if r_index % 2:
            c.setFillColor(colors.HexColor("#F8FAFC"))
            c.rect(x, ry, w, row_h, stroke=0, fill=1)

        cx = x
        c.setFillColor(colors.HexColor("#0F172A"))
        for col, cw in zip(cols, widths):
            cell = str(row.get(col["key"]) or "")
            if cell and col["kind"] == "number":
                c.drawRightString(
                    cx + cw - 5,
                    ry + 6,
                    PDF._num(cell.replace(",", "")),
                )
            elif cell:
                fake = {"type": "date"} if col["kind"] == "date" else {"type": "text"}
                shown = PDF.display_value(fake, cell)
                lines = simpleSplit(shown, self.regular, 8.8, cw - 9)
                c.drawString(cx + 5, ry + 6, lines[0] if lines else "")
            cx += cw

    n = len(rows) + 1

    if _table_has_footer(el):
        ry = top - row_h * (n + 1)
        c.setFillColor(PDF._tint(self.accent, 0.92))
        c.rect(x, ry, w, row_h, stroke=0, fill=1)
        c.setFont(self.bold, 8.5)
        c.setFillColor(colors.HexColor("#0F172A"))

        all_rows = el.get("_rows_all") or rows
        totals = FX.totals_for(el, all_rows)

        cx = x
        first_label_done = False
        for col, cw in zip(cols, widths):
            if col["key"] in totals and not self.blank:
                value = totals[col["key"]]
                # Counts should print as whole numbers. Other modes keep the
                # existing compact numeric formatter.
                mode = str(col.get("total") or "").lower()
                shown = (
                    str(int(value))
                    if mode == "count"
                    else PDF._num(value)
                )
                c.drawRightString(cx + cw - 5, ry + 6, shown)
            elif not first_label_done:
                c.drawString(cx + 5, ry + 6, "Total")
                first_label_done = True
            cx += cw

        n += 1

    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.setLineWidth(0.5)
    c.rect(x, top - row_h * n, w, row_h * n, stroke=1, fill=0)

    cx = x
    for cw in widths[:-1]:
        cx += cw
        c.line(cx, top, cx, top - row_h * n)

    for i in range(1, n):
        c.line(x, top - row_h * i, x + w, top - row_h * i)


def install_runtime_patches():
    # Idempotent for Django autoreload/imports.
    if getattr(PDF, "_formula_serial_runtime_installed", False):
        return

    PDF.render_form_pdf = render_form_pdf_with_reference_formulas
    PDF._Painter._draw_table = _draw_table_with_modes
    PDF._formula_serial_runtime_installed = True


install_runtime_patches()
