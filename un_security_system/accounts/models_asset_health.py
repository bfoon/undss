"""
Unmanaged Asset Health models.

Create the backing PostgreSQL tables with:
    python manage.py ensure_asset_health

managed=False is intentional: this feature does not alter the existing
UN PASS migration graph.
"""
from django.db import models


class AssetHealthDevice(models.Model):
    asset_id = models.BigIntegerField(unique=True)
    monitor_type = models.CharField(max_length=24, default="endpoint")
    hostname = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    mac_address = models.CharField(max_length=64, blank=True)
    vendor = models.CharField(max_length=120, blank=True)
    model_name = models.CharField(max_length=160, blank=True)
    serial_number = models.CharField(max_length=160, blank=True)
    token_hash = models.CharField(max_length=64, blank=True)
    printer_queue_name = models.CharField(max_length=160, blank=True)
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=24, default="unknown")
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "accounts_asset_health_device"
        app_label = "accounts"


class AssetHealthSnapshot(models.Model):
    asset_id = models.BigIntegerField(db_index=True)
    source = models.CharField(max_length=24, default="agent")
    recorded_at = models.DateTimeField(db_index=True)
    health_score = models.IntegerField(default=100)
    status = models.CharField(max_length=24, default="healthy")
    cpu_percent = models.FloatField(null=True, blank=True)
    memory_percent = models.FloatField(null=True, blank=True)
    disk_free_percent = models.FloatField(null=True, blank=True)
    battery_health_percent = models.FloatField(null=True, blank=True)
    temperature_c = models.FloatField(null=True, blank=True)
    uptime_seconds = models.BigIntegerField(null=True, blank=True)
    os_name = models.CharField(max_length=160, blank=True)
    os_version = models.CharField(max_length=160, blank=True)
    firewall_status = models.CharField(max_length=32, blank=True)
    antivirus_status = models.CharField(max_length=32, blank=True)
    page_count_total = models.BigIntegerField(null=True, blank=True)
    toner_black_percent = models.FloatField(null=True, blank=True)
    ports_up = models.IntegerField(null=True, blank=True)
    ports_down = models.IntegerField(null=True, blank=True)
    port_errors = models.BigIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = "accounts_asset_health_snapshot"
        app_label = "accounts"


class AssetHealthAlert(models.Model):
    asset_id = models.BigIntegerField(db_index=True)
    severity = models.CharField(max_length=16)
    code = models.CharField(max_length=80)
    message = models.CharField(max_length=500)
    is_open = models.BooleanField(default=True)
    opened_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    meta = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = "accounts_asset_health_alert"
        app_label = "accounts"


class PrinterCounter(models.Model):
    asset_id = models.BigIntegerField(db_index=True)
    recorded_at = models.DateTimeField(db_index=True)
    total_pages = models.BigIntegerField(null=True, blank=True)
    black_pages = models.BigIntegerField(null=True, blank=True)
    color_pages = models.BigIntegerField(null=True, blank=True)
    supplies = models.JSONField(default=dict)
    raw = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = "accounts_printer_counter"
        app_label = "accounts"


class PrintJobLog(models.Model):
    printer_asset_id = models.BigIntegerField(db_index=True)
    job_ref = models.CharField(max_length=120, blank=True)
    user_identifier = models.CharField(max_length=180, blank=True)
    pages = models.IntegerField(null=True, blank=True)
    copies = models.IntegerField(default=1)
    color_mode = models.CharField(max_length=24, blank=True)
    status = models.CharField(max_length=32, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    meta = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = "accounts_print_job_log"
        app_label = "accounts"
