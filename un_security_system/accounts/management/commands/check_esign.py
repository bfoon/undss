# accounts/management/commands/check_esign.py
"""
Diagnoses an eSign install in one command:

    python manage.py check_esign

Reports the schema state, the converter backend, media writability and the
PDF pipeline, so you don't have to guess which layer is failing.
"""

import io
import traceback

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import connection


def _ok(label, detail=""):
    return f"  \033[92mOK\033[0m    {label}" + (f" — {detail}" if detail else "")


def _bad(label, detail=""):
    return f"  \033[91mFAIL\033[0m  {label}" + (f" — {detail}" if detail else "")


def _warn(label, detail=""):
    return f"  \033[93mWARN\033[0m  {label}" + (f" — {detail}" if detail else "")


class Command(BaseCommand):
    help = "Check that the eSign module is correctly installed."

    def handle(self, *args, **options):
        out = self.stdout.write
        failures = 0

        out("\neSign installation check\n" + "=" * 60)

        # ---- 1. models importable -------------------------------------
        out("\nModels")
        try:
            from accounts.models_esign import Envelope, EnvelopeDocument, SignatureProfile
            out(_ok("models_esign imports"))
        except Exception as exc:
            out(_bad("models_esign imports", f"{type(exc).__name__}: {exc}"))
            out("\n  Add `from .models_esign import *` to the bottom of accounts/models.py")
            return

        # ---- 2. tables and columns exist ------------------------------
        out("\nDatabase schema")
        tables = connection.introspection.table_names()
        for model in (Envelope, EnvelopeDocument, SignatureProfile):
            table = model._meta.db_table
            if table in tables:
                out(_ok(f"table {table}"))
            else:
                failures += 1
                out(_bad(f"table {table} missing"))

        if EnvelopeDocument._meta.db_table in tables:
            cols = {
                c.name
                for c in connection.introspection.get_table_description(
                    connection.cursor(), EnvelopeDocument._meta.db_table
                )
            }
            if "converted_pdf" in cols:
                out(_ok("column converted_pdf"))
            else:
                failures += 1
                out(_bad("column converted_pdf missing",
                         "run makemigrations accounts && migrate"))

        # ---- 3. dependencies ------------------------------------------
        out("\nPython packages")
        for mod, why in (("reportlab", "PDF stamping"),
                         ("pypdf", "PDF merging"),
                         ("PIL", "signature images")):
            try:
                __import__(mod)
                out(_ok(mod, why))
            except ImportError:
                failures += 1
                out(_bad(mod, f"required for {why}"))
        try:
            __import__("docx")
            out(_ok("python-docx", "Word uploads without LibreOffice"))
        except ImportError:
            out(_warn("python-docx", "optional — no .docx uploads without it"))

        # ---- 4. converter ----------------------------------------------
        out("\nOffice conversion")
        try:
            from accounts.converters_esign import (
                conversion_backend_available,
                supported_upload_ext,
            )
            ok, note = conversion_backend_available()
            out((_ok if ok else _warn)("backend", note))
            out(f"        accepted office types: {', '.join(supported_upload_ext()) or 'none'}")
        except ImportError as exc:
            out(_bad("converters_esign", f"{exc} — file is out of date, replace it"))
            failures += 1

        # ---- 5. media writability --------------------------------------
        out("\nMedia storage")
        probe = "esign/.write-probe"
        try:
            name = default_storage.save(probe, ContentFile(b"ok"))
            default_storage.delete(name)
            out(_ok("media volume writable", default_storage.location
                    if hasattr(default_storage, "location") else ""))
        except Exception as exc:
            failures += 1
            out(_bad("media volume not writable", f"{type(exc).__name__}: {exc}"))

        # ---- 6. PDF pipeline -------------------------------------------
        out("\nPDF pipeline")
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas as rl
            from pypdf import PdfReader

            buf = io.BytesIO()
            c = rl.Canvas(buf, pagesize=A4)
            c.drawString(80, 700, "eSign check")
            c.showPage()
            c.save()
            pages = len(PdfReader(io.BytesIO(buf.getvalue())).pages)
            out(_ok("reportlab + pypdf round trip", f"{pages} page generated"))
        except Exception as exc:
            failures += 1
            out(_bad("PDF pipeline", f"{type(exc).__name__}: {exc}"))
            out(traceback.format_exc())

        # ---- 7. URLs ----------------------------------------------------
        out("\nURLs")
        try:
            from django.urls import reverse
            reverse("accounts:esign_dashboard")
            reverse("accounts:esign_new")
            out(_ok("eSign routes registered"))
        except Exception as exc:
            failures += 1
            out(_bad("eSign routes", f"{exc} — check the import in accounts/urls.py"))

        out("\n" + "=" * 60)
        if failures:
            out(self.style.ERROR(f"{failures} problem(s) found.\n"))
        else:
            out(self.style.SUCCESS("eSign is correctly installed.\n"))
