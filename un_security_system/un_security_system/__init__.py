# Load the Celery app whenever Django starts, so @shared_task binds to it.
#
# Guarded: the web process must start even where Celery is not installed —
# scheduled jobs then simply do not run, and `manage.py unpass_doctor` says so.
try:
    from .celery import app as celery_app
except Exception:  # pragma: no cover
    celery_app = None

__all__ = ("celery_app",)
