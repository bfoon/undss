from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the system roles from the old fixed roles and offer them any new default capabilities."

    def handle(self, *args, **opts):
        from access import registry
        from access.models import AccessRole
        from access.services import bump, ensure_system_roles

        registry.autodiscover()
        ensure_system_roles(force=True)
        bump()
        for r in AccessRole.objects.filter(is_system=True).order_by("name"):
            self.stdout.write(f"  {r.name:<34} {len(r.capabilities):>2} powers  ({r.legacy_code})")
        self.stdout.write(self.style.SUCCESS("Access Studio is ready."))
