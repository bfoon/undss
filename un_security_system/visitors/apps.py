from django.apps import AppConfig


class VisitorsConfig(AppConfig):
    name = 'visitors'
    verbose_name = 'Visitor Management'

    def ready(self):
        """
        Wire up the post-save signal on MeetingAttendee so that whenever an
        attendee is accepted, all linked visitor access requests are automatically
        re-synced. The import is lazy so the app works even if accounts is not
        yet migrated.
        """
        try:
            from .signals import connect_signals
            connect_signals()
        except Exception:
            pass