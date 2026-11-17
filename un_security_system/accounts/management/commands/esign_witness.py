# accounts/management/commands/esign_witness.py
"""
Manage NES cross-office witnessing.

Setting up a pair of offices — run on each side:

    # 1. generate a token for the OTHER office to use when pushing to us,
    #    and export our seal certificate for them
    python manage.py esign_witness --new-token
    python manage.py esign_witness --export-cert /tmp/our-seal.crt

    # 2. register the peer (swap tokens and certificates out of band)
    python manage.py esign_witness --add-peer UNDP-SN \\
        --name "UNDP Senegal" --url https://unpass.sn \\
        --outbound-token <token THEY gave us> \\
        --inbound-token  <token WE gave them> \\
        --cert /tmp/their-seal.crt

    # 3. confirm the link works in both directions
    python manage.py esign_witness --ping

Day to day, in cron:

    python manage.py esign_witness --send-pending     # push queued, every 15m
    python manage.py esign_witness --audit            # compare, nightly

Investigating:

    python manage.py esign_witness --status
    python manage.py esign_witness --audit <ENVELOPE_ID>
    python manage.py esign_witness --reissue <ENVELOPE_ID>
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Manage NES cross-office witnessing (peers, distribution, audit)."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true",
                            help="Show peers, backlog and open alerts.")
        parser.add_argument("--new-token", action="store_true",
                            help="Generate a token to give a peer.")
        parser.add_argument("--export-cert", metavar="PATH",
                            help="Write our seal certificate (PEM) for a peer.")

        parser.add_argument("--add-peer", metavar="CODE",
                            help="Register or update a participating office.")
        parser.add_argument("--name", default="", help="With --add-peer.")
        parser.add_argument("--url", default="", help="With --add-peer.")
        parser.add_argument("--outbound-token", default="",
                            help="Token we present to that peer.")
        parser.add_argument("--inbound-token", default="",
                            help="Token we require from that peer.")
        parser.add_argument("--cert", metavar="PATH", default="",
                            help="That peer's seal certificate (PEM).")
        parser.add_argument("--remove-peer", metavar="CODE",
                            help="Deactivate a peer (records are kept).")

        parser.add_argument("--ping", action="store_true",
                            help="Check every peer answers a lookup.")
        parser.add_argument("--send-pending", action="store_true",
                            help="Push queued attestations to peers.")
        parser.add_argument("--audit", metavar="ENVELOPE_ID", nargs="?",
                            const="__all__",
                            help="Compare stored files against attestations.")
        parser.add_argument("--no-peers", action="store_true",
                            help="With --audit, check locally only.")
        parser.add_argument("--reissue", metavar="ENVELOPE_ID",
                            help="Re-witness one envelope and queue it.")
        parser.add_argument("--notify", action="store_true",
                            help="Email outstanding alerts now.")
        parser.add_argument("--limit", type=int, default=200)

    # -- output helpers -------------------------------------------------
    def _ok(self, m):
        return self.style.SUCCESS(f"  OK    {m}")

    def _bad(self, m):
        return self.style.ERROR(f"  FAIL  {m}")

    def _warn(self, m):
        return self.style.WARNING(f"  WARN  {m}")

    def _info(self, m):
        return f"        {m}"

    def handle(self, *args, **opts):
        from accounts import witness_esign as W

        acted = False

        if opts.get("new_token"):
            token = secrets.token_urlsafe(32)
            self.stdout.write(self.style.SUCCESS(f"\n{token}\n"))
            self.stdout.write(
                "Give this to the peer office. They set it as their\n"
                "--outbound-token; you set it as your --inbound-token for "
                "them.\nSend it over a channel other than the one it will "
                "authenticate.\n"
            )
            acted = True

        if opts.get("export_cert"):
            self.export_cert(opts["export_cert"])
            acted = True

        if opts.get("add_peer"):
            self.add_peer(opts)
            acted = True

        if opts.get("remove_peer"):
            self.remove_peer(opts["remove_peer"])
            acted = True

        if opts.get("status"):
            self.show_status()
            acted = True

        if opts.get("ping"):
            self.ping()
            acted = True

        if opts.get("send_pending"):
            self.send_pending(opts["limit"])
            acted = True

        if opts.get("reissue"):
            self.reissue(opts["reissue"])
            acted = True

        if opts.get("audit"):
            self.audit(opts["audit"], not opts.get("no_peers"), opts["limit"])
            acted = True

        if opts.get("notify"):
            result = W.notify_alerts()
            self.stdout.write(
                self._ok(result["detail"]) if result["sent"]
                else self._warn(result["detail"])
            )
            acted = True

        if not acted:
            self.stdout.write(self.style.WARNING(
                "Nothing to do. Try --status, --new-token, --export-cert, "
                "--add-peer,\n--ping, --send-pending, --audit or --reissue."
            ))

    # ------------------------------------------------------------------
    def export_cert(self, path: str):
        from accounts import pades_esign as P

        ready, reason = P.pades_configured()
        if not ready:
            raise CommandError(f"No seal certificate to export: {reason}")

        from asn1crypto import pem

        signer = P.load_seal_signer()
        der = signer.signing_cert.dump()
        with open(path, "wb") as fh:
            fh.write(pem.armor("CERTIFICATE", der))

        self.stdout.write(self._ok(f"wrote {path}"))
        self.stdout.write(self._info(
            f"subject: {signer.signing_cert.subject.human_friendly}"
        ))
        self.stdout.write(
            "\nGive this to the peer office. They register it with --cert, and\n"
            "will then accept statements signed by this key and no other.\n"
        )

    # ------------------------------------------------------------------
    def add_peer(self, opts):
        from accounts.models_esign_witness import WitnessPeer

        code = opts["add_peer"].strip()
        cert_pem = ""
        if opts.get("cert"):
            try:
                with open(opts["cert"], "r", encoding="utf-8") as fh:
                    cert_pem = fh.read()
            except Exception as exc:
                raise CommandError(f"Could not read --cert: {exc}")
            if "BEGIN CERTIFICATE" not in cert_pem:
                raise CommandError(
                    "--cert does not look like a PEM certificate."
                )

        peer, created = WitnessPeer.objects.get_or_create(
            code=code, defaults={"name": opts.get("name") or code,
                                 "base_url": opts.get("url") or ""},
        )
        if opts.get("name"):
            peer.name = opts["name"]
        if opts.get("url"):
            peer.base_url = opts["url"]
        if opts.get("outbound_token"):
            peer.outbound_token = opts["outbound_token"]
        if opts.get("inbound_token"):
            peer.inbound_token = opts["inbound_token"]
        if cert_pem:
            peer.certificate_pem = cert_pem
        peer.active = True
        peer.save()

        self.stdout.write(
            self._ok(f"{'registered' if created else 'updated'} {peer.code}")
        )
        self.stdout.write(self._info(f"name      : {peer.name}"))
        self.stdout.write(self._info(f"url       : {peer.base_url or '—'}"))
        self.stdout.write(self._info(
            f"push to   : {peer.receive_url if peer.base_url else '—'}"
        ))
        for label, value in (
            ("outbound token", peer.outbound_token),
            ("inbound token", peer.inbound_token),
            ("certificate", peer.certificate_pem),
        ):
            self.stdout.write(
                self._ok(f"{label} set") if value
                else self._warn(f"{label} NOT set")
            )
        if not peer.certificate_pem:
            self.stdout.write(self._warn(
                "Without their certificate we cannot verify anything they "
                "send, and their deposits will be rejected."
            ))

    def remove_peer(self, code: str):
        from accounts.models_esign_witness import WitnessPeer

        peer = WitnessPeer.objects.filter(code=code).first()
        if peer is None:
            raise CommandError(f"No peer with code {code!r}.")
        peer.active = False
        peer.save(update_fields=["active"])
        self.stdout.write(self._ok(
            f"{code} deactivated. Attestations already exchanged are kept — "
            "they are evidence."
        ))

    # ------------------------------------------------------------------
    def show_status(self):
        from accounts import witness_esign as W
        from accounts.models_esign_witness import (
            WitnessAlert, WitnessDelivery, WitnessPeer, WitnessRecord,
        )

        self.stdout.write("\nNES cross-office witnessing")
        self.stdout.write("=" * 62)

        if W.witnessing_enabled():
            self.stdout.write(self._ok("ESIGN_WITNESS_ENABLED is on"))
        else:
            self.stdout.write(self._warn(
                "ESIGN_WITNESS_ENABLED is off — nothing is being distributed."
            ))

        code = W.office_code()
        self.stdout.write(
            self._ok(f"this office is {code}") if code
            else self._bad("ESIGN_WITNESS_OFFICE_CODE is not set")
        )

        recipients = W.alert_recipients()
        self.stdout.write(
            self._ok(f"alerts go to {', '.join(recipients)}") if recipients
            else self._warn(
                "No ESIGN_WITNESS_ALERT_EMAILS and no ADMINS — alerts would "
                "be recorded but nobody emailed."
            )
        )

        self.stdout.write(self._info(
            f"our attestations : {WitnessRecord.objects.filter(received_from__isnull=True).count()}"
        ))
        self.stdout.write(self._info(
            f"held for peers   : {WitnessRecord.objects.filter(received_from__isnull=False).count()}"
        ))
        pending = WitnessDelivery.objects.filter(
            status=WitnessDelivery.STATUS_PENDING).count()
        failed = WitnessDelivery.objects.filter(
            status=WitnessDelivery.STATUS_FAILED).count()
        self.stdout.write(self._info(f"queued to send   : {pending}"))
        if failed:
            self.stdout.write(self._warn(f"{failed} delivery(ies) gave up"))

        self.stdout.write("\nPeers")
        self.stdout.write("-" * 62)
        peers = WitnessPeer.objects.order_by("code")
        if not peers:
            self.stdout.write(self._warn(
                "No peers registered. Witnessing only protects you once at "
                "least one other office holds copies."
            ))
        for peer in peers:
            flag = "active" if peer.active else "inactive"
            self.stdout.write(f"  {peer.code:14s} {peer.name[:28]:30s} {flag}")
            self.stdout.write(self._info(f"url   : {peer.base_url or '—'}"))
            self.stdout.write(self._info(
                f"ready : {'yes' if peer.is_configured else 'NO — missing url or certificate'}"
            ))
            self.stdout.write(self._info(
                f"held  : {WitnessRecord.objects.filter(received_from=peer).count()} "
                f"| last contact: {peer.last_contact_at or 'never'}"
            ))
            if peer.last_error:
                self.stdout.write(self._warn(peer.last_error[:140]))

        open_alerts = WitnessAlert.objects.filter(resolved=False).order_by(
            "-raised_at")[:20]
        self.stdout.write("\nOpen alerts")
        self.stdout.write("-" * 62)
        if not open_alerts:
            self.stdout.write(self._ok("none"))
        for alert in open_alerts:
            style = self._bad if alert.severity == "critical" else self._warn
            self.stdout.write(style(
                f"{alert.get_kind_display()} — {alert.envelope_uid[:16] or '—'}"
            ))
            self.stdout.write(self._info(alert.detail[:160]))
        self.stdout.write("")

    # ------------------------------------------------------------------
    def ping(self):
        from accounts import witness_esign as W
        from accounts.models_esign_witness import WitnessPeer

        peers = list(WitnessPeer.objects.filter(active=True))
        if not peers:
            self.stdout.write(self._warn("No active peers."))
            return

        self.stdout.write("\nPeer reachability")
        self.stdout.write("=" * 62)
        for peer in peers:
            # A deliberately non-existent id: we only want to know whether the
            # endpoint answers and accepts our token, not to read anything.
            attestations, error = W._ask_peer(
                peer, "PING" + "0" * 28, W.office_code(), 10
            )
            if error:
                self.stdout.write(self._bad(f"{peer.code}: {error[:150]}"))
                if "401" in error:
                    self.stdout.write(self._info(
                        "401 means they do not recognise our outbound token. "
                        "They must set it as their inbound token for us."
                    ))
            else:
                self.stdout.write(self._ok(
                    f"{peer.code}: answered ({len(attestations)} attestation(s) "
                    "for the probe id, 0 expected)"
                ))
        self.stdout.write("")

    # ------------------------------------------------------------------
    def send_pending(self, limit: int):
        from accounts import witness_esign as W

        report = W.send_pending(limit=limit)
        self.stdout.write("\nDistributing attestations")
        self.stdout.write("=" * 62)
        self.stdout.write(self._info(f"attempted : {report['attempted']}"))
        self.stdout.write(self._ok(f"sent      : {report['sent']}"))
        if report["failed"]:
            self.stdout.write(self._warn(f"failed    : {report['failed']}"))
        if report["gave_up"]:
            self.stdout.write(self._bad(
                f"gave up   : {report['gave_up']} (alert raised)"
            ))
        for error in report["errors"][:10]:
            self.stdout.write(self._info(error[:150]))
        self.stdout.write("")

    # ------------------------------------------------------------------
    def reissue(self, envelope_id: str):
        from accounts import witness_esign as W

        env = self._envelope(envelope_id)
        result = W.issue_witness(env, kind="seal")
        if result["ok"]:
            self.stdout.write(self._ok(
                f"re-witnessed {env.envelope_id}, queued to "
                f"{result['queued']} peer(s)"
            ))
        else:
            self.stdout.write(self._bad(result["detail"]))
        if result.get("detail") and result["ok"]:
            self.stdout.write(self._warn(result["detail"]))

    def _envelope(self, envelope_id: str):
        from accounts.models_esign import Envelope

        env = (
            Envelope.objects.filter(envelope_id=envelope_id).first()
            or Envelope.objects.filter(
                envelope_id__istartswith=envelope_id).first()
        )
        if env is None:
            raise CommandError(f"No envelope matches {envelope_id!r}.")
        return env

    # ------------------------------------------------------------------
    def audit(self, target: str, ask_peers: bool, limit: int):
        from accounts import witness_esign as W

        self.stdout.write("\nIntegrity audit against witnessed copies")
        self.stdout.write("=" * 62)
        if not ask_peers:
            self.stdout.write(self._info("local records only (--no-peers)"))

        if target == "__all__":
            report = W.audit(limit=limit, ask_peers=ask_peers)
        else:
            env = self._envelope(target)
            report = W.audit(ask_peers=ask_peers, envelope=env)

        for result in report["results"]:
            if result["ok"]:
                continue
            self.stdout.write(self._bad(f"{result['envelope_uid']}"))
            self.stdout.write(self._info(
                f"file now : {result['live_sha256'][:32]}…"
            ))
            self.stdout.write(self._info(
                f"peers    : {result['peers_agreeing']} agree, "
                f"{result['peers_disagreeing']} disagree, "
                f"{result['peers_unreachable']} unreachable"
            ))
            for finding in result["findings"]:
                self.stdout.write(self._info(
                    f"[{finding.get('severity')}] {finding['detail']}"
                ))
            for row in result["peer_detail"]:
                self.stdout.write(self._info(
                    f"  {row['peer']}: {row['status']} — {row['detail'][:100]}"
                ))

        self.stdout.write("")
        self.stdout.write(self._info(f"checked : {report['checked']}"))
        self.stdout.write(self._ok(f"clean   : {report['clean']}"))
        if report["flagged"]:
            self.stdout.write(self._bad(f"flagged : {report['flagged']}"))
            self.stdout.write(self._info(
                f"{report['alerts']} new alert(s) raised and emailed."
            ))
            self.stdout.write(self.style.ERROR(
                "\nDo not delete anything. A divergence is evidence; the "
                "evidence package\nand the witnessed copies are what prove "
                "what the document used to be.\n"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nEvery checked document matches what was witnessed.\n"
            ))
