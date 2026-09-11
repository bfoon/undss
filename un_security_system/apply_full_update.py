#!/usr/bin/env python3
from pathlib import Path
from datetime import datetime
import json
import re
import shutil
import sys
import py_compile

ROOT = Path.cwd()
HERE = Path(__file__).resolve().parent
OPS_FILE = HERE / "patch_data" / "operations.json"
PAYLOAD = HERE / "payload"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = ROOT / f"_esign_upgrade_backup_{STAMP}"

if not (ROOT / "un_security_system" / "accounts").is_dir():
    print("ERROR: Run this from the UNPASS repository root (the folder containing un_security_system).")
    sys.exit(1)

ops = json.loads(OPS_FILE.read_text(encoding="utf-8"))

def backup(path):
    rel = path.relative_to(ROOT)
    out = BACKUP / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        shutil.copy2(path, out)

def fail(label, path):
    print(f"\nERROR while applying: {label}")
    print(f"File: {path}")
    print("The installer stopped rather than guessing because this local file differs from the reviewed GitHub master.")
    print(f"Any files already touched were backed up under: {BACKUP}")
    sys.exit(2)

# Copy complete new files first.
for src in sorted(PAYLOAD.rglob("*")):
    if not src.is_file():
        continue
    rel = src.relative_to(PAYLOAD)
    dest = ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup(dest)
    shutil.copy2(src, dest)
    print("COPY ", rel)

# Apply exact/regex changes to existing project files.
for op in ops:
    path = ROOT / op["path"]
    if not path.exists():
        fail(op["label"], path)
    text = path.read_text(encoding="utf-8")
    original = text

    if op["type"] == "replace":
        old, new = op["old"], op["new"]
        if new in text:
            print("SKIP ", op["label"], "(already applied)")
            continue
        if old not in text:
            fail(op["label"], path)
        text = text.replace(old, new, 1)

    elif op["type"] == "insert_before":
        marker, addition = op["marker"], op["addition"]
        if addition.strip() in text:
            print("SKIP ", op["label"], "(already applied)")
            continue
        if marker not in text:
            fail(op["label"], path)
        text = text.replace(marker, addition + marker, 1)

    elif op["type"] == "regex":
        pattern, repl = op["pattern"], op["replacement"]
        # A distinctive piece of the replacement makes reruns harmless.
        signature = repl.strip().splitlines()[0] if repl.strip() else ""
        if signature and signature in text and re.search(pattern, text, flags=re.S) is None:
            print("SKIP ", op["label"], "(already applied)")
            continue
        text2, count = re.subn(pattern, repl, text, count=1, flags=re.S)
        if count != 1:
            fail(op["label"], path)
        text = text2
    else:
        fail("unknown operation", path)

    if text != original:
        backup(path)
        path.write_text(text, encoding="utf-8")
        print("PATCH", op["label"])

# Syntax check the Python files touched/added.
checks = [
    ROOT / "un_security_system/accounts/utils_esign.py",
    ROOT / "un_security_system/accounts/workflow_engine_esign.py",
    ROOT / "un_security_system/accounts/form_pdf_esign.py",
    ROOT / "un_security_system/accounts/views_esign_workflow.py",
    ROOT / "un_security_system/accounts/views_esign_forms.py",
    ROOT / "un_security_system/accounts/esign_condition_engine.py",
    ROOT / "un_security_system/accounts/workflow_portability_esign.py",
    ROOT / "un_security_system/accounts/docx_form_import_esign.py",
    ROOT / "un_security_system/accounts/form_logic_esign.py",
]
for p in checks:
    py_compile.compile(str(p), doraise=True)

print("\nUNPASS eSign upgrade applied successfully.")
print("Backup folder:", BACKUP)
print("\nNext:")
print("  python manage.py check")
print("or Docker:")
print("  docker compose -f docker-compose.prod.yml exec web python manage.py check")
