from django.apps import AppConfig


class VehiclesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vehicles'

    def ready(self):
        # Package documents signed in eSign advance their classic package step.
        # (package_esign.py says this moved here, but ready() was missing, so the
        # receiver in signals.py was never connected.)
        from . import signals  # noqa: F401

        # Packages running on Flow Studio follow their run: status, stage,
        # completion and in-app notifications.
        from .package_studio import connect_signals

        connect_signals()
