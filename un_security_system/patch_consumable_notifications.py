"""
Run this on the server:
  python patch_consumable_notifications.py

It patches view_asset_management.py in-place to add full email
notifications to every consumable action.
"""

import ast, sys

VIEW_PATH = "accounts/view_asset_management.py"

with open(VIEW_PATH) as f:
    content = f.read()

original = content  # keep for rollback check
patches  = []

# ─────────────────────────────────────────────────────────────────
# PATCH 1 — create_consumable_request
# Replace bare send_email_async block with _notify to ops + unit head
# ─────────────────────────────────────────────────────────────────
OLD1 = """            # Notify ops manager / unit head
            roles_obj = getattr(agency, "asset_roles", None)
            ops       = getattr(roles_obj, "operations_manager", None)
            if ops and ops.email:
                try:
                    send_email_async(
                        subject=f"Supply Request #{creq.id} — Pending Approval",
                        to_emails=[ops.email],
                        html_template="emails/consumables/request_submitted.html",
                        context={"agency": agency, "creq": creq, "portal_url": portal_url},
                    )
                except Exception:
                    pass"""

NEW1 = """            # Notify all approvers: ops manager + requester's unit head (if different)
            approval_emails = []
            if roles:
                if roles.operations_manager and roles.operations_manager.email:
                    approval_emails.append(roles.operations_manager.email)
            requester_unit = getattr(user, "unit", None)
            if requester_unit and requester_unit.unit_head and requester_unit.unit_head.email:
                if requester_unit.unit_head.email not in approval_emails:
                    approval_emails.append(requester_unit.unit_head.email)
            _notify(
                subject=f"Supply Request #{creq.id} — Pending Approval",
                to_emails=approval_emails,
                html_template="emails/consumables/request_submitted.html",
                ctx={"creq": creq},
            )"""

patches.append(("create_consumable_request notify", OLD1, NEW1))

# ─────────────────────────────────────────────────────────────────
# PATCH 2 — approve_consumable_request
# Add notification to requester + ICT after approval
# ─────────────────────────────────────────────────────────────────
OLD2 = """            else:
                creq.approve(user)
                messages.success(request, f"Supply request #{creq.id} approved — awaiting dispatch.")"""

NEW2 = """            else:
                creq.approve(user)

                # Notify requester
                _notify(
                    subject=f"Supply Request #{creq.id} — Approved",
                    to_emails=[getattr(creq.requester, "email", None)],
                    html_template="emails/consumables/request_approved.html",
                    ctx={
                        "creq": creq,
                        "approved_by": user.get_full_name() or user.username,
                    },
                )

                # Notify ICT to arrange dispatch
                ict_dispatch_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []
                _notify(
                    subject=f"Supply Request #{creq.id} — Ready for Dispatch",
                    to_emails=ict_dispatch_emails,
                    html_template="emails/consumables/request_submitted.html",
                    ctx={"creq": creq},
                )

                messages.success(request, f"Supply request #{creq.id} approved — awaiting dispatch.")"""

patches.append(("approve_consumable_request notify", OLD2, NEW2))

# ─────────────────────────────────────────────────────────────────
# PATCH 3 — reject_consumable_request
# Add notification to requester after rejection
# ─────────────────────────────────────────────────────────────────
OLD3 = """                creq.reject(user, reason=reason)
                messages.warning(request, f"Supply request #{creq.id} rejected.")"""

NEW3 = """                creq.reject(user, reason=reason)

                # Notify requester
                _notify(
                    subject=f"Supply Request #{creq.id} — Rejected",
                    to_emails=[getattr(creq.requester, "email", None)],
                    html_template="emails/consumables/request_rejected.html",
                    ctx={
                        "creq": creq,
                        "rejected_by": user.get_full_name() or user.username,
                    },
                )

                messages.warning(request, f"Supply request #{creq.id} rejected.")"""

patches.append(("reject_consumable_request notify", OLD3, NEW3))

# ─────────────────────────────────────────────────────────────────
# PATCH 4 — dispatch_consumable
# Replace bare send_email_async with enriched _notify + low-stock alert
# ─────────────────────────────────────────────────────────────────
OLD4 = """            # Notify requester
            req_email = getattr(creq.requester, "email", None)
            if req_email:
                try:
                    send_email_async(
                        subject=f"Your Supply Request #{creq.id} has been dispatched",
                        to_emails=[req_email],
                        html_template="emails/consumables/dispatched.html",
                        context={"agency": agency, "creq": creq, "portal_url": portal_url},
                    )
                except Exception:
                    pass

            messages.success(request, f"Supply request #{creq.id} dispatched successfully.")
            return redirect("accounts:asset_management")"""

NEW4 = """            # Notify requester with full dispatch details
            _notify(
                subject=f"Your Supply Request #{creq.id} has been dispatched",
                to_emails=[getattr(creq.requester, "email", None)],
                html_template="emails/consumables/dispatched.html",
                ctx={
                    "creq": creq,
                    "dispatched_by": user.get_full_name() or user.username,
                    "dispatched_at": timezone.now(),
                    "dispatch_note": dispatch_note,
                },
            )

            # Low-stock alert to ICT / ops after stock is reduced
            dispatched_item_ids = [li.item_id for li in creq.line_items.all()]
            now_low = [
                i for i in ConsumableItem.objects.filter(
                    agency=agency, id__in=dispatched_item_ids, is_active=True,
                )
                if i.is_low_stock
            ]
            if now_low:
                ict_alert_emails = list(roles.ict_custodian.values_list("email", flat=True)) if roles else []
                if is_ops and user.email and user.email not in ict_alert_emails:
                    ict_alert_emails.append(user.email)
                _notify(
                    subject=f"[{agency}] Low Stock Alert — {len(now_low)} item(s) need restocking",
                    to_emails=ict_alert_emails,
                    html_template="emails/consumables/low_stock_alert.html",
                    ctx={"creq": creq, "low_stock_items": now_low},
                )

            messages.success(request, f"Supply request #{creq.id} dispatched successfully.")
            return redirect("accounts:asset_management")"""

patches.append(("dispatch_consumable notify + low-stock alert", OLD4, NEW4))

# ─────────────────────────────────────────────────────────────────
# Apply patches
# ─────────────────────────────────────────────────────────────────
errors = []
for name, old, new in patches:
    count = content.count(old)
    if count == 0:
        errors.append(f"  ✗ NOT FOUND: {name}")
    elif count > 1:
        errors.append(f"  ✗ AMBIGUOUS ({count} matches): {name}")
    else:
        content = content.replace(old, new)
        print(f"  ✓ Patched: {name}")

if errors:
    print("\nErrors:")
    for e in errors:
        print(e)
    print("\nFile NOT written. Check the error messages above.")
    sys.exit(1)

# Verify syntax
try:
    ast.parse(content)
    print("\n✓ Syntax OK")
except SyntaxError as e:
    print(f"\n✗ Syntax error at line {e.lineno}: {e.msg}")
    print("File NOT written.")
    sys.exit(1)

with open(VIEW_PATH, "w") as f:
    f.write(content)

print(f"✓ {VIEW_PATH} updated successfully.")
