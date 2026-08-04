# accounts/converters_esign.py
"""
Office → PDF conversion for eSign.

One backend: a local headless LibreOffice binary. Install it in the web image
(see INSTALL.md §10) and this module needs no configuration at all.

If LibreOffice isn't present the module degrades quietly — PDF and image
uploads keep working, and the envelope builder shows a note explaining that
Office uploads are unavailable.
"""

import os
import shutil
import subprocess
import tempfile

from django.conf import settings

__all__ = [
    "ConversionError",
    "SUPPORTED_OFFICE_EXT",
    "is_office_file",
    "convert_to_pdf",
    "conversion_backend_available",
]


class ConversionError(Exception):
    """Raised when a document could not be turned into a PDF."""


SUPPORTED_OFFICE_EXT = (
    ".docx", ".doc", ".odt", ".rtf", ".txt",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
)


def is_office_file(filename: str) -> bool:
    return str(filename or "").lower().endswith(SUPPORTED_OFFICE_EXT)


def _binary():
    """Path to LibreOffice, or None."""
    configured = getattr(settings, "ESIGN_SOFFICE_BIN", "soffice")
    return shutil.which(configured) or shutil.which("libreoffice")


def conversion_backend_available():
    """(ok, description) — used by the envelope builder and handy from a shell."""
    binary = _binary()
    if binary:
        return True, f"LibreOffice at {binary}"
    return False, (
        "LibreOffice is not installed in this container, so Word/Excel/"
        "PowerPoint uploads can't be converted. Install libreoffice-core in "
        "the web image, or upload documents as PDF."
    )


def convert_to_pdf(raw: bytes, filename: str) -> bytes:
    """Convert an office document to PDF bytes."""
    if not raw:
        raise ConversionError("The uploaded file is empty.")

    binary = _binary()
    if not binary:
        raise ConversionError(conversion_backend_available()[1])

    timeout = int(getattr(settings, "ESIGN_CONVERT_TIMEOUT", 120))
    base = os.path.basename(filename) or "document.docx"
    stem = os.path.splitext(base)[0] or "document"

    with tempfile.TemporaryDirectory(prefix="esign-convert-") as tmp:
        src = os.path.join(tmp, base)
        outdir = os.path.join(tmp, "out")
        profile = os.path.join(tmp, "profile")
        os.makedirs(outdir, exist_ok=True)

        with open(src, "wb") as fh:
            fh.write(raw)

        cmd = [
            binary,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            # A private profile per call, so two gunicorn workers converting at
            # the same time don't deadlock on a shared LibreOffice lock file.
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf",
            "--outdir", outdir,
            src,
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise ConversionError(f"LibreOffice timed out after {timeout}s.")

        produced = os.path.join(outdir, stem + ".pdf")
        if not os.path.exists(produced):
            err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            raise ConversionError(err[:200] or "LibreOffice produced no output file.")

        with open(produced, "rb") as fh:
            out = fh.read()

    if out[:5] != b"%PDF-":
        raise ConversionError("Conversion produced something that is not a PDF.")
    return out
