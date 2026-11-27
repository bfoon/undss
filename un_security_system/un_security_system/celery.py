# un_security_system/celery.py
"""
The Celery application.

UN PASS had Celery in requirements.txt and @shared_task functions in the code,
but no Celery app — so `shared_task` bound to Celery's default app, which
points at a broker that does not exist, and nothing ever ran. This is the app;
`un_security_system/__init__.py` imports it so tasks bind to it.

Run with (docker-compose.yml does this for you):

    celery -A un_security_system worker --loglevel=info
    celery -A un_security_system beat   --loglevel=info

The schedule itself lives in un_security_system/schedule.py.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "un_security_system.settings")

app = Celery("un_security_system")

# Every CELERY_* setting in settings.py configures this app.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Finds tasks.py in every installed app, and the scheduler's run_job.
app.autodiscover_tasks()
app.autodiscover_tasks(["accounts"], related_name="scheduler")
