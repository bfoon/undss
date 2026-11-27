# accounts/management/commands/nes_sentinel.py
"""
NES Sentinel administration.

    manage.py nes_sentinel keygen            create the publication key (once)
    manage.py nes_sentinel status            key, ledger, nodes, open alerts
    manage.py nes_sentinel publish           publish new blocks now
    manage.py nes_sentinel verify            re-check every block's link and signature
    manage.py nes_sentinel code "UNDP Dakar — cloud" --office SN-DKR
                                             one-time enrolment code
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts import sentinel_esign as S


class Command(BaseCommand):
    help = "NES Sentinel: key, ledger and enrolment administration."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["keygen", "status", "publish", "verify", "code"])
        parser.add_argument("name", nargs="?", default="")
        parser.add_argument("--office", default="")
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--force", action="store_true",
                            help="keygen: replace an existing key. Every Sentinel will "
                                 "then raise 'server_identity_changed' and must re-enrol.")

    def handle(self, *args, **opts):
        getattr(self, "do_" + opts["action"])(opts)

    def do_keygen(self, opts):
        try:
            path = S.generate_key(force=opts["force"])
        except FileExistsError as exc:
            raise CommandError(f"{exc}. Use --force only if you mean to replace it.")
        except OSError as exc:
            raise CommandError(
                f"Cannot write {S.key_path()}: {exc.strerror}. Under Docker the secrets "
                "folder is read-only on purpose — create the key on the host with "
                "scripts/sentinel_keygen.sh instead.")
        fp = S.fingerprint(S.public_key_b64())
        self.stdout.write(self.style.SUCCESS(f"Publication key written to {path} (mode 600)."))
        self.stdout.write(f"Fingerprint: {S.pretty_fingerprint(fp)}")
        self.stdout.write("Back this file up with the seal key. Never commit it.")

    def do_status(self, opts):
        from accounts.models_esign_sentinel import SentinelAlert, SentinelBlock, SentinelNode

        if S.key_ready():
            self.stdout.write(f"Key          {S.key_path()}")
            self.stdout.write(f"Fingerprint  {S.pretty_fingerprint(S.fingerprint(S.public_key_b64()))}")
        else:
            self.stdout.write(self.style.WARNING(f"Key          missing — expected at {S.key_path()}"))
        head = S.ledger_head()
        self.stdout.write(f"Ledger       {SentinelBlock.objects.count()} blocks, head #{head['seq']} "
                          f"{head['hash'][:16]}…")
        for node in SentinelNode.objects.all():
            seen = timezone.localtime(node.last_seen_at).strftime("%Y-%m-%d %H:%M") \
                if node.last_seen_at else "never"
            self.stdout.write(f"Node         {node.name:<30} {node.status:<8} seen {seen}  "
                              f"holds #{node.last_seq}")
        open_alerts = SentinelAlert.objects.filter(resolved_at__isnull=True).count()
        style = self.style.ERROR if open_alerts else self.style.SUCCESS
        self.stdout.write(style(f"Open alerts  {open_alerts}"))

    def do_publish(self, opts):
        self.stdout.write(str(S.publish()))

    def do_verify(self, opts):
        result = S.verify_ledger()
        if result["ok"]:
            self.stdout.write(self.style.SUCCESS(f"All {result['blocks']} blocks verify."))
        else:
            for p in result["problems"][:50]:
                self.stdout.write(self.style.ERROR(f"block #{p['seq']}: {p['kind']}"))
            raise CommandError(f"{len(result['problems'])} problem(s) in the ledger.")

    def do_code(self, opts):
        if not opts["name"]:
            raise CommandError('Give the Sentinel a name: nes_sentinel code "UNDP Dakar — cloud"')
        code = S.create_enrolment(opts["name"], opts["office"], None, hours=opts["hours"])
        self.stdout.write(self.style.SUCCESS(code))
        if S.key_ready():
            self.stdout.write(f"Server fingerprint to read to the installer: "
                              f"{S.pretty_fingerprint(S.fingerprint(S.public_key_b64()))}")
