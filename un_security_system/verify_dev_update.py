#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path.cwd()
checks = {
    "workflow status overlay":
        ("un_security_system/accounts/workflow_engine_esign.py",
         "def _stamp_workflow_form_pdf("),
    "stable envelope id":
        ("un_security_system/accounts/workflow_engine_esign.py",
         "primary_envelope_id"),
    "default signature":
        ("un_security_system/templates/accounts/esign/sign.html",
         "const esDefaultSaved"),
    "safe runtime state":
        ("un_security_system/accounts/form_logic_esign.py",
         "runtime_state"),
}

failed = False
for label, (rel, needle) in checks.items():
    p = root / rel
    ok = p.exists() and needle in p.read_text(encoding="utf-8")
    print(("OK   " if ok else "FAIL "), label)
    failed |= not ok

for rel in [
    "un_security_system/accounts/form_logic_esign.py",
    "un_security_system/templates/accounts/esign/studio/_form_sheet.html",
]:
    p = root / rel
    if p.exists() and "_runtime_state" in p.read_text(encoding="utf-8"):
        print("FAIL legacy _runtime_state remains in", rel)
        failed = True

sys.exit(1 if failed else 0)
