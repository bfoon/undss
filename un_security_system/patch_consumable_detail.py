"""
Run from your project root on the server:
  python patch_consumable_detail.py

Applies three changes:
  1. Appends consumable_item_detail view to view_asset_management.py
  2. Adds the URL to accounts/urls.py
  3. Makes item names clickable in _consumables_table.html
"""
import ast, re, sys

# ─── Paths (adjust if your project structure differs) ───────────────────────
VIEW_FILE   = "accounts/view_asset_management.py"
URL_FILE    = "accounts/urls.py"
TABLE_FILE  = "accounts/templates/accounts/assets/partials/_consumables_table.html"

# ─── 1. Append view ──────────────────────────────────────────────────────────
NEW_VIEW = '''

@login_required
def consumable_item_detail(request, item_id: int):
    """
    Detail page for a single consumable item.
    Shows item info, current stock level, and full stock history.
    ICT / Ops can restock or correct stock directly from this page.
    """
    user   = request.user
    agency = getattr(user, "agency", None)

    if not agency:
        messages.error(request, "You are not assigned to an agency.")
        return redirect("accounts:profile")

    svc, _ = AgencyServiceConfig.objects.get_or_create(agency=agency)
    if not svc.asset_mgmt_enabled and not user.is_superuser:
        messages.warning(request, "Asset Management is not enabled for your agency.")
        return redirect("accounts:profile")

    roles, _ = AgencyAssetRoles.objects.get_or_create(agency=agency)
    is_ict    = _is_ict(user, agency)
    is_ops    = _is_ops_manager(user, agency)
    can_manage = is_ict or is_ops or user.is_superuser

    item = get_object_or_404(
        ConsumableItem.objects.select_related("category", "agency"),
        id=item_id, agency=agency,
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "restock_consumable_item":
            if not can_manage:
                messages.error(request, "Only ICT or Operations Manager can restock supplies.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            qty_str = (request.POST.get("restock_qty") or "0").strip()
            note    = (request.POST.get("restock_note") or "").strip()
            try:
                qty = int(qty_str)
            except ValueError:
                messages.error(request, "Invalid quantity.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            if qty <= 0:
                messages.error(request, "Restock quantity must be a positive number.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            with transaction.atomic():
                item_locked = ConsumableItem.objects.select_for_update().get(id=item.id)
                before = item_locked.stock_qty
                item_locked.stock_qty += qty
                item_locked.save(update_fields=["stock_qty"])
                ConsumableStockLog.objects.create(
                    agency=agency, item=item_locked, event="restocked",
                    quantity_before=before, quantity_change=qty,
                    quantity_after=item_locked.stock_qty,
                    note=note or f"Restocked by {user.get_full_name() or user.username}",
                    actor=user,
                )
                item.refresh_from_db()

            messages.success(request, f"Restocked {item.name}: +{qty} {item.unit_of_measure}. New stock: {item.stock_qty}.")
            return redirect("accounts:consumable_item_detail", item_id=item.id)

        if action == "correct_stock":
            if not can_manage:
                messages.error(request, "Only ICT or Operations Manager can correct stock.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            qty_str = (request.POST.get("corrected_qty") or "").strip()
            note    = (request.POST.get("correction_note") or "").strip()
            try:
                new_qty = int(qty_str)
                if new_qty < 0:
                    raise ValueError
            except ValueError:
                messages.error(request, "Corrected quantity must be a non-negative number.")
                return redirect("accounts:consumable_item_detail", item_id=item.id)

            with transaction.atomic():
                item_locked = ConsumableItem.objects.select_for_update().get(id=item.id)
                before = item_locked.stock_qty
                change = new_qty - before
                item_locked.stock_qty = new_qty
                item_locked.save(update_fields=["stock_qty"])
                ConsumableStockLog.objects.create(
                    agency=agency, item=item_locked, event="corrected",
                    quantity_before=before, quantity_change=change,
                    quantity_after=new_qty,
                    note=note or f"Manual correction by {user.get_full_name() or user.username}",
                    actor=user,
                )
                item.refresh_from_db()

            messages.success(request, f"Stock corrected. {item.name} is now {item.stock_qty} {item.unit_of_measure}.")
            return redirect("accounts:consumable_item_detail", item_id=item.id)

        messages.error(request, "Unknown action.")
        return redirect("accounts:consumable_item_detail", item_id=item.id)

    # GET
    stock_logs = ConsumableStockLog.objects.filter(
        agency=agency, item=item
    ).select_related("actor", "reference_request", "reference_request__requester").order_by("-created_at")

    total_dispatched = sum(abs(l.quantity_change) for l in stock_logs if l.event == "dispatched")
    total_restocked  = sum(l.quantity_change      for l in stock_logs if l.event == "restocked")

    recent_requests = ConsumableRequestItem.objects.filter(
        item=item
    ).select_related(
        "request", "request__requester", "request__unit"
    ).order_by("-request__created_at")[:20]

    return render(request, "accounts/assets/consumable_item_detail.html", {
        "item":             item,
        "stock_logs":       stock_logs,
        "total_dispatched": total_dispatched,
        "total_restocked":  total_restocked,
        "recent_requests":  recent_requests,
        "can_manage":       can_manage,
        "is_ict":           is_ict,
        "is_ops":           is_ops,
    })
'''

print("── Patching view file ──")
with open(VIEW_FILE) as f:
    view_content = f.read()

if "def consumable_item_detail" in view_content:
    print("  ℹ  consumable_item_detail already exists in view — skipping.")
else:
    view_content = view_content.rstrip() + "\n" + NEW_VIEW + "\n"
    try:
        ast.parse(view_content)
        print("  ✓ Syntax OK")
    except SyntaxError as e:
        print(f"  ✗ Syntax error at line {e.lineno}: {e.msg}")
        sys.exit(1)
    with open(VIEW_FILE, "w") as f:
        f.write(view_content)
    print(f"  ✓ View appended to {VIEW_FILE}")


# ─── 2. Add URL ──────────────────────────────────────────────────────────────
print("\n── Patching urls.py ──")
with open(URL_FILE) as f:
    url_content = f.read()

if "consumable_item_detail" in url_content:
    print("  ℹ  URL already exists — skipping.")
else:
    # Find the asset_detail URL line and insert after it
    # Try common patterns
    patterns = [
        r"(path\(['\"]assets/detail/<int:asset_id>/['\"].*?\),)",
        r"(path\(['\"]asset/<int:asset_id>/['\"].*?\),)",
        r"(.*asset_detail.*)",
    ]
    inserted = False
    for pat in patterns:
        match = re.search(pat, url_content)
        if match:
            old = match.group(0)
            new = old + "\n    path('assets/supply/<int:item_id>/', views.consumable_item_detail, name='consumable_item_detail'),"
            url_content = url_content.replace(old, new, 1)
            inserted = True
            print(f"  ✓ URL inserted after: {old.strip()[:60]}…")
            break

    if not inserted:
        # Fallback: insert before the closing ] of urlpatterns
        url_content = re.sub(
            r'(\]\s*$)',
            "    path('assets/supply/<int:item_id>/', views.consumable_item_detail, name='consumable_item_detail'),\n\\1",
            url_content,
            count=1,
            flags=re.MULTILINE,
        )
        print("  ✓ URL appended (fallback) — verify placement in urls.py")

    with open(URL_FILE, "w") as f:
        f.write(url_content)
    print(f"  ✓ {URL_FILE} updated")


# ─── 3. Make item names clickable in stock table ─────────────────────────────
print("\n── Patching _consumables_table.html ──")
with open(TABLE_FILE) as f:
    tpl = f.read()

OLD_TD = """        <td class="fw-semibold">
          {{ item.name }}
          {% if item.is_out_of_stock %}
            <span class="badge bg-danger ms-1">Out of Stock</span>
          {% elif item.is_low_stock %}
            <span class="badge bg-warning text-dark ms-1">Low</span>
          {% endif %}
        </td>"""

NEW_TD = """        <td class="fw-semibold">
          <a href="{% url 'accounts:consumable_item_detail' item.id %}"
             class="text-decoration-none text-dark">
            {{ item.name }}
          </a>
          {% if item.is_out_of_stock %}
            <span class="badge bg-danger ms-1">Out of Stock</span>
          {% elif item.is_low_stock %}
            <span class="badge bg-warning text-dark ms-1">Low</span>
          {% endif %}
        </td>"""

if OLD_TD in tpl:
    tpl = tpl.replace(OLD_TD, NEW_TD)
    with open(TABLE_FILE, "w") as f:
        f.write(tpl)
    print(f"  ✓ Item names are now clickable in {TABLE_FILE}")
elif "consumable_item_detail" in tpl:
    print("  ℹ  Links already present — skipping.")
else:
    print("  ✗ Could not find item name <td> block — patch manually:")
    print("    Wrap {{ item.name }} in:")
    print("    <a href=\"{% url 'accounts:consumable_item_detail' item.id %}\">{{ item.name }}</a>")

print("\n✅ All patches applied.")
