from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import signals_invites  # noqa: F401
        try:
            from . import tasks_asset_health  # noqa: F401
            tasks_asset_health.register_asset_health_schedule()
        except Exception:
            pass
