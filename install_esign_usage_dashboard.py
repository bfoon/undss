#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path.cwd()
PROJECT_URLS = ROOT / "un_security_system/un_security_system/urls.py"
NAV_ITEMS = ROOT / "un_security_system/templates/partials/_nav_items.html"


def backup(path: Path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(
        path.name + f".before_esign_usage_dashboard_{stamp}"
    )
    shutil.copy2(path, target)
    print(f"Backup created: {target}")


def patch_project_urls():
    if not PROJECT_URLS.exists():
        raise SystemExit(f"ERROR: file not found: {PROJECT_URLS}")

    text = PROJECT_URLS.read_text(encoding="utf-8")
    route = (
        '    path("insights/esign/", '
        'include("accounts.dashboard_esign_urls")),\n'
    )

    if 'include("accounts.dashboard_esign_urls")' in text:
        print("Project URLs already contain the dashboard route.")
        return

    backup(PROJECT_URLS)

    anchor = (
        '    path("platform/", '
        'include("tenancy.urls", namespace="tenancy")),\n'
    )

    if anchor in text:
        text = text.replace(anchor, route + anchor, 1)
    else:
        # Safe fallback: insert immediately before the urlpatterns closing
        # bracket, after the last known project route.
        start = text.find("urlpatterns = [")
        if start == -1:
            raise SystemExit(
                "ERROR: could not find urlpatterns in project urls.py"
            )

        close = text.find("\n]", start)
        if close == -1:
            raise SystemExit(
                "ERROR: could not find urlpatterns closing bracket."
            )

        text = text[:close] + "\n" + route.rstrip("\n") + text[close:]

    PROJECT_URLS.write_text(text, encoding="utf-8")
    print("Added /insights/esign/ route.")


def patch_navigation():
    if not NAV_ITEMS.exists():
        raise SystemExit(f"ERROR: file not found: {NAV_ITEMS}")

    text = NAV_ITEMS.read_text(encoding="utf-8")

    if (
        "esign_dashboard_tags" in text
        and "eSign Usage Dashboard" in text
    ):
        print("Navigation already contains the dashboard button.")
        return

    backup(NAV_ITEMS)

    if "esign_dashboard_tags" not in text:
        text = "{% load esign_dashboard_tags %}\n" + text

    block = """
  {% esign_dashboard_access as esign_usage_access %}
  {% if esign_usage_access.can_view %}
  <div class="appnav-section">
    <div class="appnav-heading">Analytics</div>
    {% url 'esign_analytics:dashboard' as u %}
    <a class="appnav-link {% if '/insights/esign/' in request.path %}is-active{% endif %}"
       href="{{ u }}?scope={{ esign_usage_access.preferred_scope }}"
       data-appnav-label="eSign Usage Dashboard">
      <i class="bi bi-bar-chart-line-fill" aria-hidden="true"></i>
      <span>eSign Usage Dashboard</span>
      <span class="appnav-count">L{{ esign_usage_access.max_level }}</span>
    </a>
  </div>
  {% endif %}

"""

    anchor = (
        "  {% if user.is_superuser or "
        "user.role == 'ict_focal' %}\n"
    )

    if anchor not in text:
        raise SystemExit(
            "ERROR: could not find the Administration section in "
            "_nav_items.html. No navigation change was written."
        )

    text = text.replace(anchor, block + anchor, 1)
    NAV_ITEMS.write_text(text, encoding="utf-8")
    print("Added permission-aware dashboard navigation button.")


patch_project_urls()
patch_navigation()
print("eSign / Forms / Flow dashboard wiring complete.")
