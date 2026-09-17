from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from accounts.asset_health import issue_token
from accounts.models import Asset


class Command(BaseCommand):
    help = "Issue/rotate a one-time endpoint-agent token for one Asset."

    def add_arguments(self, parser):
        parser.add_argument("asset_id", type=int)

    def handle(self, *args, **options):
        asset_id = options["asset_id"]
        try:
            asset = Asset.objects.get(pk=asset_id)
        except Asset.DoesNotExist:
            raise CommandError(f"Asset {asset_id} does not exist.")

        token, digest = issue_token()
        now = timezone.now()

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts_asset_health_device
                (asset_id,monitor_type,token_hash,enabled,status,created_at,updated_at)
                VALUES (%s,'endpoint',%s,TRUE,'unknown',%s,%s)
                ON CONFLICT (asset_id)
                DO UPDATE SET monitor_type='endpoint',token_hash=EXCLUDED.token_hash,
                              enabled=TRUE,updated_at=EXCLUDED.updated_at
                """,
                [asset_id, digest, now, now],
            )

        self.stdout.write(self.style.SUCCESS(f"Agent token issued for: {asset}"))
        self.stdout.write("Copy this token now. It is not stored in plaintext:")
        self.stdout.write(token)
