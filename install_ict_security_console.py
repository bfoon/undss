#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path.cwd()
SETTINGS = ROOT / "un_security_system/un_security_system/settings.py"
PROJECT_URLS = ROOT / "un_security_system/un_security_system/urls.py"
NAV = ROOT / "un_security_system/templates/partials/_nav_items.html"
ICT_USERS = ROOT / "un_security_system/templates/accounts/ict/user_list.html"
VIEWS_ICT = ROOT / "un_security_system/accounts/views_ict.py"


def backup(path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(
        path.name + f".before_security_console_{stamp}"
    )
    shutil.copy2(path, target)
    print(f"Backup: {target}")


def replace_python_list_assignment(text, variable, replacement):
    marker = f"{variable} = ["
    start = text.find(marker)
    if start == -1:
        return None

    bracket = text.find("[", start)
    depth = 0
    in_string = None
    escaped = False
    end = None

    for idx in range(bracket, len(text)):
        ch = text[idx]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue

        if ch in ("'", '"'):
            in_string = ch
            continue

        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break

    if end is None:
        return None

    return text[:start] + replacement + text[end:]


def patch_settings():
    if not SETTINGS.exists():
        raise SystemExit(f"Missing settings file: {SETTINGS}")

    original = SETTINGS.read_text(encoding="utf-8")
    text = original

    middleware = "'accounts.security_console.SecurityTelemetryMiddleware'"
    if middleware not in text:
        auth_line = "'django.contrib.auth.middleware.AuthenticationMiddleware',"
        if auth_line not in text:
            raise SystemExit(
                "Could not find AuthenticationMiddleware in settings.py."
            )
        text = text.replace(
            auth_line,
            auth_line + "\n    " + middleware + ",",
            1,
        )

    validator_block = """AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "accounts.security_console.SecurityPasswordPolicyValidator",
    },
]"""

    replaced = replace_python_list_assignment(
        text,
        "AUTH_PASSWORD_VALIDATORS",
        validator_block,
    )
    if replaced is None:
        raise SystemExit(
            "Could not find AUTH_PASSWORD_VALIDATORS in settings.py."
        )
    text = replaced

    if text != original:
        backup(SETTINGS)
        SETTINGS.write_text(text, encoding="utf-8")
        print(
            "Settings wired: telemetry middleware + "
            "dynamic password policy validator."
        )
    else:
        print("Settings already wired.")


def patch_urls():
    if not PROJECT_URLS.exists():
        raise SystemExit(f"Missing project urls.py: {PROJECT_URLS}")

    text = PROJECT_URLS.read_text(encoding="utf-8")
    marker = 'include("accounts.security_console_urls")'
    if marker in text:
        print("Security Console URL route already present.")
        return

    route = (
        '    path("ict/security/", '
        'include("accounts.security_console_urls")),\n'
    )
    anchor = '    path("accounts/", include("accounts.urls")),\n'

    if anchor in text:
        new_text = text.replace(anchor, anchor + route, 1)
    else:
        anchor = "urlpatterns = [\n"
        if anchor not in text:
            raise SystemExit(
                "Could not locate urlpatterns in project urls.py."
            )
        new_text = text.replace(anchor, anchor + route, 1)

    backup(PROJECT_URLS)
    PROJECT_URLS.write_text(new_text, encoding="utf-8")
    print("Added /ict/security/ route.")


def patch_nav():
    if not NAV.exists():
        print(
            "WARNING: navigation template not found; "
            "skipped nav button."
        )
        return

    text = NAV.read_text(encoding="utf-8")
    if (
        "security_console_tags" in text
        and "ICT Security Analytics" in text
    ):
        print("Security Console navigation already present.")
        return

    original = text

    if "security_console_tags" not in text:
        text = "{% load security_console_tags %}\n" + text

    block = r"""
  {% security_console_access as security_access %}
  {% if security_access.can_view %}
  <div class="appnav-section">
    <div class="appnav-heading">ICT Security</div>
    {% url 'security_console:dashboard' as u_security %}
    <a class="appnav-link {% if '/ict/security/' in request.path %}is-active{% endif %}"
       href="{{ u_security }}?scope={{ security_access.preferred_scope }}"
       data-appnav-label="ICT Security Analytics">
      <i class="bi bi-shield-lock-fill" aria-hidden="true"></i>
      <span>ICT Security Analytics</span>
    </a>
    {% if security_access.can_manage_policy %}
      {% url 'security_console:password_policy' as u_policy %}
      <a class="appnav-link appnav-link-sub {% if request.path == u_policy %}is-active{% endif %}"
         href="{{ u_policy }}?scope={{ security_access.preferred_scope }}"
         data-appnav-label="Password Policy">
        <i class="bi bi-key-fill" aria-hidden="true"></i>
        <span>Password Policy</span>
      </a>
    {% endif %}
  </div>
  {% endif %}

"""

    admin_anchor = (
        "  {% if user.is_superuser or "
        "user.role == 'ict_focal' %}\n"
    )

    if admin_anchor not in text:
        print(
            "WARNING: Administration anchor not found in "
            "_nav_items.html; security nav block was not inserted."
        )
        return

    text = text.replace(admin_anchor, block + admin_anchor, 1)

    if text != original:
        backup(NAV)
        NAV.write_text(text, encoding="utf-8")
        print("Added permission-aware ICT Security navigation.")


def patch_ict_user_list():
    if not ICT_USERS.exists():
        print(
            "WARNING: ICT user list template not found; "
            "skipped console buttons."
        )
        return

    text = ICT_USERS.read_text(encoding="utf-8")

    if (
        "security_console_access" in text
        and "Security Analytics" in text
    ):
        print(
            "ICT user list already has Security Analytics buttons."
        )
        return

    original = text

    if "security_console_tags" not in text:
        if "{% load tenancy_status %}" in text:
            text = text.replace(
                "{% load tenancy_status %}",
                "{% load tenancy_status security_console_tags %}",
                1,
            )
        else:
            text = "{% load security_console_tags %}\n" + text

    content_anchor = "{% block content %}\n"
    tag_line = "{% security_console_access as security_access %}\n"
    if (
        content_anchor in text
        and tag_line.strip() not in text
    ):
        text = text.replace(
            content_anchor,
            content_anchor + tag_line,
            1,
        )

    marker = """      <a class="btn btn-warning" href="{% url 'accounts:registration_links_list' %}">
        <i class="bi bi-link-45deg me-1"></i>Registration Links
      </a>
"""

    addition = marker + """
      {% if security_access.can_view %}
        <a class="btn btn-outline-primary"
           href="{% url 'security_console:dashboard' %}?scope={{ security_access.preferred_scope }}">
          <i class="bi bi-shield-lock-fill me-1"></i>Security Analytics
        </a>
      {% endif %}

      {% if security_access.can_manage_policy %}
        <a class="btn btn-outline-dark"
           href="{% url 'security_console:password_policy' %}?scope={{ security_access.preferred_scope }}">
          <i class="bi bi-key-fill me-1"></i>Password Policy
        </a>
      {% endif %}
"""

    if marker not in text:
        print(
            "WARNING: Registration Links button marker not found in "
            "user_list.html. The sidebar link will still work."
        )
        return

    text = text.replace(marker, addition, 1)

    if text != original:
        backup(ICT_USERS)
        ICT_USERS.write_text(text, encoding="utf-8")
        print(
            "Added Security Analytics / Password Policy buttons "
            "to ICT Users page."
        )


def patch_invite_password_policy():
    if not VIEWS_ICT.exists():
        print(
            "WARNING: views_ict.py not found; "
            "invite password hook skipped."
        )
        return

    text = VIEWS_ICT.read_text(encoding="utf-8")

    if "validate_password_for_office" in text:
        print(
            "Invite registration already uses the dynamic "
            "password policy."
        )
        return

    old = """        if password1 and len(password1) < 8:
            errors["password"] = "Password must be at least 8 characters long."
"""

    new = """        # Apply the effective Global / Country Office password policy.
        # No password value is logged or stored by the policy engine.
        if password1 and "password" not in errors:
            try:
                from django.core.exceptions import ValidationError as PasswordPolicyError
                from .invite_scope import invite_scope
                from .security_console import validate_password_for_office

                invite_target = invite_scope(invite)
                validate_password_for_office(
                    password1,
                    office_id=invite_target.office_id,
                )
            except PasswordPolicyError as exc:
                errors["password"] = " ".join(exc.messages)
"""

    if old not in text:
        print(
            "WARNING: hard-coded registration password block was "
            "not found in views_ict.py. Public invite registration "
            "may still use its old minimum-length rule. All Django "
            "password forms will use the new policy."
        )
        return

    backup(VIEWS_ICT)
    VIEWS_ICT.write_text(
        text.replace(old, new, 1),
        encoding="utf-8",
    )
    print(
        "Registration-link password validation now uses "
        "the effective CO policy."
    )


patch_settings()
patch_urls()
patch_nav()
patch_ict_user_list()
patch_invite_password_policy()

print("")
print("ICT Security Console wiring complete.")
print("Next:")
print(
    "  docker compose run --rm --no-deps web "
    "python manage.py check"
)
print(
    "  docker compose run --rm --no-deps web "
    "python manage.py ensure_security_console"
)
print("  docker compose restart web")
