#!/usr/bin/env python3
from pathlib import Path
import sys

def detect_repo_root():
    starts = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    for start in starts:
        for p in (start, *start.parents):
            if (
                (p / "un_security_system" / "accounts").is_dir()
                and (p / "un_security_system" / "manage.py").exists()
            ):
                return p
            if (
                p.name == "un_security_system"
                and (p / "accounts").is_dir()
                and (p / "manage.py").exists()
            ):
                return p.parent
    return None

root = detect_repo_root()
if root is None:
    print("FAIL: Could not locate repository.")
    sys.exit(1)

base = root / "un_security_system"

checks = [
    ("advanced condition engine", base / "accounts/esign_condition_engine.py", "CONDITION_OPS"),
    ("workflow portability", base / "accounts/workflow_portability_esign.py", "unpass-esign-flow"),
    ("Word form importer", base / "accounts/docx_form_import_esign.py", "docx"),
    ("runtime state engine", base / "accounts/form_logic_esign.py", "runtime_state"),
    ("advanced workflow conditions", base / "accounts/workflow_engine_esign.py", "esign_condition_engine"),
    ("stable Envelope ID", base / "accounts/workflow_engine_esign.py", "primary_envelope_id"),
    ("final PDF status overlay", base / "accounts/workflow_engine_esign.py", "_stamp_workflow_form_pdf"),
    ("workflow child ID suppression", base / "accounts/utils_esign.py", "_is_workflow_child_envelope"),
    ("free-position form design", base / "templates/accounts/esign/studio/form_designer.html", "canvas"),
    ("flow import page", base / "templates/accounts/esign/studio/import_flow.html", "Import"),
    ("Word import page", base / "templates/accounts/esign/studio/import_word_form.html", "Word"),
    ("default saved signature", base / "templates/accounts/esign/sign.html", "const esDefaultSaved"),
]

failed = False
for label, path, needle in checks:
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    ok = needle.lower() in text.lower()
    print(("OK   " if ok else "FAIL "), label)
    if not ok:
        print("      ", path)
        failed = True

for rel in [
    "accounts/form_logic_esign.py",
    "templates/accounts/esign/studio/_form_sheet.html",
]:
    p = base / rel
    if p.exists() and "_runtime_state" in p.read_text(encoding="utf-8", errors="ignore"):
        print("FAIL legacy _runtime_state remains:", p)
        failed = True

print()
if failed:
    print("VERIFICATION FAILED")
    sys.exit(1)

print("ALL CUMULATIVE DEV UPDATE CHECKS PASSED")
