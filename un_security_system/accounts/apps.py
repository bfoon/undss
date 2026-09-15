from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenancy"
    verbose_name = "Tenancy & feature switches"

    def ready(self):
        from . import signals  # noqa: F401
        from .scope_control import install_audit_action_labels

        install_audit_action_labels()
