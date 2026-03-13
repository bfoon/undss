"""
Run from your project root on the server:
  python patch_consumable_detail_fix.py

Only patches _consumables_table.html (URL and view were already applied).
Finds the file automatically regardless of where it lives.
"""
import os, sys

# ── Auto-find the table partial ──────────────────────────────────────────────
def find_file(filename, search_root="."):
    for dirpath, _, filenames in os.walk(search_root):
        if filename in filenames:
            return os.path.join(dirpath, filename)
    return None

TABLE_FILE = find_file("_consumables_table.html")

if not TABLE_FILE:
    print("✗ Could not find _consumables_table.html anywhere under the current directory.")
    print("  Make sure you're running this from your project root (e.g. /app/un_security_system/).")
    sys.exit(1)

print(f"  Found: {TABLE_FILE}")

with open(TABLE_FILE) as f:
    tpl = f.read()

if "consumable_item_detail" in tpl:
    print("  ℹ  Links already present — nothing to do.")
    sys.exit(0)

# ── Strategy: wrap every bare {{ item.name }} in the stock table with a link ─
# We look for the item name cell pattern — works regardless of exact whitespace.
import re

# Pattern: the td that contains item.name and the stock badges
OLD_PATTERN = re.compile(
    r'(<td[^>]*class="[^"]*fw-semibold[^"]*"[^>]*>)\s*'
    r'(\{\{\s*item\.name\s*\}\})',
    re.MULTILINE,
)

if OLD_PATTERN.search(tpl):
    def replacer(m):
        return (
            m.group(1) + "\n"
            '          <a href="{% url \'accounts:consumable_item_detail\' item.id %}"\n'
            '             class="text-decoration-none text-dark">\n'
            '            ' + m.group(2) + '\n'
            '          </a>'
        )
    new_tpl = OLD_PATTERN.sub(replacer, tpl, count=1)
    with open(TABLE_FILE, "w") as f:
        f.write(new_tpl)
    print(f"  ✓ Item name is now a clickable link in {TABLE_FILE}")
else:
    # Fallback: plain string replace on the most common pattern
    FALLBACK_OLD = "{{ item.name }}"
    FALLBACK_NEW = (
        '<a href="{% url \'accounts:consumable_item_detail\' item.id %}" '
        'class="text-decoration-none text-dark">{{ item.name }}</a>'
    )

    if tpl.count(FALLBACK_OLD) == 1:
        new_tpl = tpl.replace(FALLBACK_OLD, FALLBACK_NEW)
        with open(TABLE_FILE, "w") as f:
            f.write(new_tpl)
        print(f"  ✓ Item name linked (fallback replace) in {TABLE_FILE}")
    elif tpl.count(FALLBACK_OLD) > 1:
        print(f"  ✗ Found {tpl.count(FALLBACK_OLD)} occurrences of {{{{ item.name }}}} — too ambiguous to auto-patch.")
        print("  Please manually wrap the item name cell with:")
        print('  <a href="{% url \'accounts:consumable_item_detail\' item.id %}">{{ item.name }}</a>')
    else:
        print("  ✗ Could not locate {{ item.name }} in the template.")
        print(f"  Open {TABLE_FILE} and manually wrap the item name with the link above.")

print("\n✅ Done.")
