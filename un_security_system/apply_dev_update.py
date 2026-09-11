#!/usr/bin/env python3
from pathlib import Path
from datetime import datetime
import re
import shutil
import sys
import py_compile

ROOT = Path.cwd()
APP = ROOT / "un_security_system" / "accounts"
TPL = ROOT / "un_security_system" / "templates" / "accounts" / "esign"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = ROOT / f"_dev_esign_backup_{STAMP}"

FILES = {
    "engine": APP / "workflow_engine_esign.py",
    "sign": TPL / "sign.html",
    "logic": APP / "form_logic_esign.py",
    "sheet": TPL / "studio" / "_form_sheet.html",
}

def fail(msg):
    print("\\nERROR:", msg)
    sys.exit(1)

if not (ROOT / "un_security_system").is_dir():
    fail("Run this from the repository root, the directory containing un_security_system/.")

for path in FILES.values():
    if not path.exists():
        fail(f"Missing required file: {path}")

def backup(path):
    rel = path.relative_to(ROOT)
    target = BACKUP / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(path, target)

def write(path, text):
    backup(path)
    path.write_text(text, encoding="utf-8")
    print("UPDATED:", path.relative_to(ROOT))

# 1) Fix runtime_state naming
for key in ("logic", "sheet"):
    p = FILES[key]
    s = p.read_text(encoding="utf-8")
    if "_runtime_state" in s:
        s = s.replace("_runtime_state", "runtime_state")
        write(p, s)
    else:
        print("OK     :", p.relative_to(ROOT), "(runtime_state already safe)")

# 2) Default saved signature auto-use
p = FILES["sign"]
s = p.read_text(encoding="utf-8")

old_button = '''          <button class="btn btn-sm btn-outline-primary ms-auto es-use-saved"
                  data-id="{{ s.id }}" data-sig="{{ s.url }}" data-init="{{ s.initials }}">Use</button>'''

new_button = '''          {% if s.is_default %}
            <span class="badge bg-success">Default</span>
          {% endif %}
          <button class="btn btn-sm btn-outline-primary ms-auto es-use-saved"
                  data-id="{{ s.id }}"
                  data-sig="{{ s.url }}"
                  data-init="{{ s.initials }}"
                  data-default="{% if s.is_default %}1{% else %}0{% endif %}">
            {% if s.is_default %}Default{% else %}Use{% endif %}
          </button>'''

if 'data-default="{% if s.is_default %}1{% else %}0{% endif %}"' not in s:
    if old_button not in s:
        fail("Could not find the saved-signature button in sign.html.")
    s = s.replace(old_button, new_button, 1)

old_js = '''document.querySelectorAll('.es-use-saved').forEach(b => b.onclick = () => {
  setAdopted(b.dataset.sig, b.dataset.init || b.dataset.sig,
             { kind:'saved', save:false }, b.dataset.id);
  showAdopted();
  reapplyAll();
  nextField();
});'''

new_js = '''document.querySelectorAll('.es-use-saved').forEach(b => b.onclick = () => {
  setAdopted(b.dataset.sig, b.dataset.init || b.dataset.sig,
             { kind:'saved', save:false }, b.dataset.id);
  showAdopted();
  reapplyAll();
  nextField();
});

const esDefaultSaved = document.querySelector(
  '.es-use-saved[data-default="1"]'
);

if (esDefaultSaved) {
  setAdopted(
    esDefaultSaved.dataset.sig,
    esDefaultSaved.dataset.init || esDefaultSaved.dataset.sig,
    { kind:'saved', save:false, isDefault:true },
    esDefaultSaved.dataset.id
  );
  showAdopted();
  const adoptLabel = document.getElementById('esAdoptLabel');
  if (adoptLabel) adoptLabel.textContent = 'Default signature ready';
}'''

if "const esDefaultSaved" not in s:
    if old_js not in s:
        fail("Could not find the saved-signature JavaScript block in sign.html.")
    s = s.replace(old_js, new_js, 1)

write(p, s)

# 3) Workflow PDF current status + one stable visible Envelope ID
p = FILES["engine"]
s = p.read_text(encoding="utf-8")

new_document_section = r'''def _workflow_primary_envelope_id(run):
    context = run.context if isinstance(run.context, dict) else {}
    saved = str(context.get("primary_envelope_id") or "").strip()
    if saved:
        return saved

    try:
        task = (
            run.tasks.filter(envelope__isnull=False)
            .select_related("envelope")
            .order_by("created_at", "id")
            .first()
        )
        if task and task.envelope:
            return task.envelope.envelope_id
    except Exception:
        logger.exception(
            "eSign Studio: could not resolve primary envelope for run %s",
            run.pk,
        )
    return ""


def _stamp_workflow_form_pdf(raw, run, *, envelope_id="", status_label=""):
    if not raw or not run.submission_id:
        return raw

    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib import colors
        from reportlab.pdfgen import canvas as rl_canvas

        reader = PdfReader(io.BytesIO(raw))
        writer = PdfWriter()

        submission = run.submission
        schema = submission.schema or {}
        header = schema.get("header") or {}

        submitted_at = getattr(submission, "created_at", None)
        submitted_stamp = ""
        if submitted_at:
            submitted_stamp = timezone.localtime(submitted_at).strftime(
                "%d %b %Y %H:%M"
            )

        for index, page in enumerate(reader.pages):
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)

            overlay_buf = io.BytesIO()
            c = rl_canvas.Canvas(overlay_buf, pagesize=(width, height))

            if envelope_id:
                c.setFillColor(colors.white)
                c.rect(
                    max(20, width - 270),
                    height - 25,
                    min(235, width - 40),
                    16,
                    stroke=0,
                    fill=1,
                )
                c.setFillColor(colors.HexColor("#64748B"))
                c.setFont("Courier", 6.5)
                c.drawRightString(
                    width - 36,
                    height - 16,
                    f"Envelope ID: {envelope_id}",
                )

            if index == 0 and status_label:
                band_h = 58.0 + (14.0 if header.get("subtitle") else 0.0)
                meta_y = height - 36.0 + 14.0 - band_h - 13.0

                c.setFillColor(colors.white)
                c.rect(
                    max(250, width - 300),
                    meta_y - 6,
                    min(265, width - 285),
                    16,
                    stroke=0,
                    fill=1,
                )

                right = status_label
                if submitted_stamp:
                    right = f"{right}   |   {submitted_stamp}"

                c.setFillColor(colors.HexColor("#64748B"))
                c.setFont("Helvetica-Bold", 7.8)
                c.drawRightString(width - 42, meta_y, right)

            c.save()
            overlay_buf.seek(0)

            overlay_reader = PdfReader(overlay_buf)
            if overlay_reader.pages:
                page.merge_page(overlay_reader.pages[0])

            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    except Exception:
        logger.exception(
            "eSign Studio: could not stamp workflow PDF metadata for run %s",
            run.pk,
        )
        return raw


def run_pdf_bytes(run, status_label=""):
    if run.submission_id and not run.context.get("frozen"):
        from .form_pdf_esign import render_form_pdf

        sub = run.submission
        pdf, boxes = render_form_pdf(
            sub.schema,
            sub.values,
            reference=sub.reference,
            submitted_at=sub.created_at,
            submitter=sub.submitter_name,
            status_label=status_label or run.get_status_display(),
        )
        run._form_boxes = boxes
        return pdf

    raw = _read(run.document)

    if run.submission_id and raw:
        raw = _stamp_workflow_form_pdf(
            raw,
            run,
            envelope_id=_workflow_primary_envelope_id(run),
            status_label=status_label or run.get_status_display(),
        )

    return raw

'''

if "def _stamp_workflow_form_pdf(" not in s:
    pattern = r'def run_pdf_bytes\(run, status_label=""\):.*?(?=\ndef set_run_document\()'
    s2, count = re.subn(pattern, new_document_section + "\\n", s, count=1, flags=re.S)
    if count != 1:
        fail("Could not replace run_pdf_bytes() in workflow_engine_esign.py.")
    s = s2

old_envelope_block = '''            reference=run.reference,
        )
        doc = EnvelopeDocument.objects.create(
'''

new_envelope_block = '''            reference=run.reference,
        )

        if run.submission_id:
            if not isinstance(run.context, dict):
                run.context = {}

            primary_id = (
                run.context.get("primary_envelope_id")
                or envelope.envelope_id
            )

            if not run.context.get("primary_envelope_id"):
                run.context["primary_envelope_id"] = primary_id
                run.save(update_fields=["context"])

            raw = _stamp_workflow_form_pdf(
                raw,
                run,
                envelope_id=primary_id,
                status_label=run.get_status_display(),
            )

        doc = EnvelopeDocument.objects.create(
'''

sig_start = s.find("def _start_signature")
tail = s[sig_start:] if sig_start >= 0 else s
if 'run.context.get("primary_envelope_id")' not in tail:
    if old_envelope_block not in s:
        fail("Could not find the workflow signature-envelope creation block.")
    s = s.replace(old_envelope_block, new_envelope_block, 1)

write(p, s)

for path in (FILES["engine"], FILES["logic"]):
    py_compile.compile(str(path), doraise=True)

print("\\nDEV UPDATE APPLIED SUCCESSFULLY")
print("Backup:", BACKUP)
print("\\nNext:")
print("  python manage.py check")
print("  python verify_dev_update.py")
