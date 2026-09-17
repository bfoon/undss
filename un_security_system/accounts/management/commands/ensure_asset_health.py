from django.core.management.base import BaseCommand
from django.db import connection


SQL = r"""
CREATE TABLE IF NOT EXISTS accounts_asset_health_device (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL UNIQUE,
    monitor_type VARCHAR(24) NOT NULL DEFAULT 'endpoint',
    hostname VARCHAR(255) NOT NULL DEFAULT '',
    ip_address INET NULL,
    mac_address VARCHAR(64) NOT NULL DEFAULT '',
    vendor VARCHAR(120) NOT NULL DEFAULT '',
    model_name VARCHAR(160) NOT NULL DEFAULT '',
    serial_number VARCHAR(160) NOT NULL DEFAULT '',
    token_hash VARCHAR(64) NOT NULL DEFAULT '',
    printer_queue_name VARCHAR(160) NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(24) NOT NULL DEFAULT 'unknown',
    last_seen TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asset_health_device_type
ON accounts_asset_health_device(monitor_type, enabled);

CREATE INDEX IF NOT EXISTS idx_asset_health_device_seen
ON accounts_asset_health_device(last_seen DESC);

CREATE TABLE IF NOT EXISTS accounts_asset_health_snapshot (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL,
    source VARCHAR(24) NOT NULL DEFAULT 'agent',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    health_score INTEGER NOT NULL DEFAULT 100,
    status VARCHAR(24) NOT NULL DEFAULT 'healthy',
    cpu_percent DOUBLE PRECISION NULL,
    memory_percent DOUBLE PRECISION NULL,
    disk_free_percent DOUBLE PRECISION NULL,
    battery_health_percent DOUBLE PRECISION NULL,
    temperature_c DOUBLE PRECISION NULL,
    uptime_seconds BIGINT NULL,
    os_name VARCHAR(160) NOT NULL DEFAULT '',
    os_version VARCHAR(160) NOT NULL DEFAULT '',
    firewall_status VARCHAR(32) NOT NULL DEFAULT '',
    antivirus_status VARCHAR(32) NOT NULL DEFAULT '',
    page_count_total BIGINT NULL,
    toner_black_percent DOUBLE PRECISION NULL,
    ports_up INTEGER NULL,
    ports_down INTEGER NULL,
    port_errors BIGINT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_asset_health_snapshot_asset_time
ON accounts_asset_health_snapshot(asset_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS accounts_asset_health_alert (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL,
    severity VARCHAR(16) NOT NULL,
    code VARCHAR(80) NOT NULL,
    message VARCHAR(500) NOT NULL,
    is_open BOOLEAN NOT NULL DEFAULT TRUE,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_asset_health_alert_open
ON accounts_asset_health_alert(asset_id, is_open, severity);

CREATE TABLE IF NOT EXISTS accounts_printer_counter (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_pages BIGINT NULL,
    black_pages BIGINT NULL,
    color_pages BIGINT NULL,
    supplies JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_printer_counter_asset_time
ON accounts_printer_counter(asset_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS accounts_print_job_log (
    id BIGSERIAL PRIMARY KEY,
    printer_asset_id BIGINT NOT NULL,
    job_ref VARCHAR(120) NOT NULL DEFAULT '',
    user_identifier VARCHAR(180) NOT NULL DEFAULT '',
    pages INTEGER NULL,
    copies INTEGER NOT NULL DEFAULT 1,
    color_mode VARCHAR(24) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT '',
    submitted_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_print_job_printer_time
ON accounts_print_job_log(printer_asset_id, submitted_at DESC);
"""


class Command(BaseCommand):
    help = "Create/repair Asset Health tables without migrations."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(SQL)
        self.stdout.write(
            self.style.SUCCESS("Asset Health schema is ready. No migrations used.")
        )
