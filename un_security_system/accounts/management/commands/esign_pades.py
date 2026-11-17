# accounts/management/commands/esign_pades.py
"""
Configure and prove out the NES PAdES seal (ETSI EN 319 142).

    python manage.py esign_pades --check
        Report the configuration and, when a certificate is present, sign a
        throwaway PDF and validate it. Changes nothing.

    python manage.py esign_pades --make-dev-cert /etc/unpass/nes-dev-seal.p12
        Generate a self-signed PKCS#12 seal for a TEST environment, so the
        whole pipeline can be exercised before a real certificate is issued.

    python manage.py esign_pades --seal-envelope 9F3C7781A118
        Re-seal one already-completed envelope in place. Useful after a
        certificate is installed for envelopes completed before that.

    python manage.py esign_pades --verify-envelope 9F3C7781A118
        Validate the seal on a completed envelope's stored PDF.

On the production server these run through Docker, e.g.
    docker compose exec web python manage.py esign_pades --check
"""

from __future__ import annotations

import datetime
import io
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check, test, or apply the NES PAdES digital seal."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Report configuration and run a sign/validate self-test.",
        )
        parser.add_argument(
            "--make-dev-cert", metavar="PATH",
            help="Write a self-signed PKCS#12 seal for testing.",
        )
        parser.add_argument(
            "--dev-cert-passphrase", default="unpass-dev",
            help="Passphrase for --make-dev-cert (default: unpass-dev).",
        )
        parser.add_argument(
            "--seal-envelope", metavar="ENVELOPE_ID",
            help="Re-seal one completed envelope's stored PDF in place.",
        )
        parser.add_argument(
            "--verify-envelope", metavar="ENVELOPE_ID",
            help="Validate the seal on a completed envelope's stored PDF.",
        )
        parser.add_argument(
            "--verify-chain", metavar="ENVELOPE_ID",
            help="Check the tamper-evident audit trail and its anchor.",
        )
        parser.add_argument(
            "--test-tsa", metavar="URL", nargs="?", const="__configured__",
            help="Do a real RFC 3161 round trip against a timestamp "
                 "authority. With no URL, tests the configured one.",
        )
        parser.add_argument(
            "--refresh-ltv", metavar="ENVELOPE_ID", nargs="?", const="__due__",
            help="Renew archival timestamps. With no value, every seal that "
                 "is due; with an envelope ID, just that one.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="With --refresh-ltv, refresh even when not yet due.",
        )
        parser.add_argument(
            "--limit", type=int, default=200,
            help="With --refresh-ltv, how many seals to process (default 200).",
        )
        parser.add_argument(
            "--evidence", metavar="ENVELOPE_ID",
            help="Write the offline evidence package for one envelope.",
        )
        parser.add_argument(
            "--register", metavar="DAYS", nargs="?", const="30",
            help="Build and seal a register of documents sealed in the last "
                 "DAYS days (default 30).",
        )
        parser.add_argument(
            "--out", metavar="PATH",
            help="Where to write the --evidence or --register output.",
        )

    # -- helpers --------------------------------------------------------
    def _ok(self, msg):
        return self.style.SUCCESS(f"  OK    {msg}")

    def _bad(self, msg):
        return self.style.ERROR(f"  FAIL  {msg}")

    def _warn(self, msg):
        return self.style.WARNING(f"  WARN  {msg}")

    def _info(self, msg):
        return f"        {msg}"

    def handle(self, *args, **opts):
        did_something = False

        if opts.get("make_dev_cert"):
            self.make_dev_cert(opts["make_dev_cert"], opts["dev_cert_passphrase"])
            did_something = True

        if opts.get("check"):
            self.run_check()
            did_something = True

        if opts.get("seal_envelope"):
            self.seal_envelope(opts["seal_envelope"])
            did_something = True

        if opts.get("verify_envelope"):
            self.verify_envelope(opts["verify_envelope"])
            did_something = True

        if opts.get("verify_chain"):
            self.verify_chain_cmd(opts["verify_chain"])
            did_something = True

        if opts.get("test_tsa"):
            self.test_tsa_cmd(opts["test_tsa"])
            did_something = True

        if opts.get("refresh_ltv"):
            self.refresh_ltv_cmd(
                opts["refresh_ltv"],
                force=bool(opts.get("force")),
                limit=int(opts.get("limit") or 200),
            )
            did_something = True

        if opts.get("evidence"):
            self.evidence_cmd(opts["evidence"], opts.get("out"))
            did_something = True

        if opts.get("register"):
            self.register_cmd(opts["register"], opts.get("out"))
            did_something = True

        if not did_something:
            self.stdout.write(self.style.WARNING(
                "Nothing to do. Try --check, --make-dev-cert, --seal-envelope, "
                "--verify-envelope, --verify-chain, --refresh-ltv, --evidence "
                "or --register."
            ))

    # ------------------------------------------------------------------
    # --check
    # ------------------------------------------------------------------
    def run_check(self):
        from accounts import pades_esign as P

        cfg = P.pades_config()
        self.stdout.write("\nNES PAdES seal — configuration check")
        self.stdout.write("=" * 62)

        if P.pades_available():
            import pyhanko
            ver = getattr(pyhanko, "__version__", "")
            self.stdout.write(self._ok(f"pyHanko importable {ver}".rstrip()))
        else:
            self.stdout.write(self._bad(
                "pyHanko is not installed — add pyHanko>=0.20 to "
                "requirements.txt and rebuild the image."
            ))
            return

        self.stdout.write(
            self._ok("ESIGN_PADES_ENABLED is on") if cfg["enabled"]
            else self._warn(
                "ESIGN_PADES_ENABLED is off — completed PDFs are not sealed."
            )
        )

        ready, reason = P.pades_configured()
        self.stdout.write(self._ok(reason) if ready else self._warn(reason))

        self.stdout.write(self._info(f"target level     : {cfg['level']}"))
        if cfg["tsa_url"]:
            self.stdout.write(self._ok(f"timestamp authority: {cfg['tsa_url']}"))
        else:
            self.stdout.write(self._warn(
                "No ESIGN_PADES_TSA_URL — the seal is capped at PAdES B-B "
                "(no proof of signing time)."
            ))
        self.stdout.write(self._info(
            f"max achievable   : {P.max_achievable_level(cfg)}"
        ))
        self.stdout.write(self._info(
            f"trust roots      : {len(cfg['trust_roots'])} configured"
        ))

        if not ready:
            self.stdout.write(self.style.WARNING(
                "\nSelf-test skipped — no usable signing certificate.\n"
                "Generate one for testing with:\n"
                "  python manage.py esign_pades "
                "--make-dev-cert /etc/unpass/nes-dev-seal.p12\n"
            ))
            return

        # -- certificate facts ----------------------------------------
        try:
            signer = P.load_seal_signer(cfg)
        except Exception as exc:
            self.stdout.write(self._bad(f"certificate not loadable: {exc}"))
            return

        details = P.signer_details(signer)
        self.stdout.write(self._ok("signing certificate loaded"))
        self.stdout.write(self._info(f"common name      : {details['common_name']}"))
        self.stdout.write(self._info(f"organization     : {details['organization']}"))
        self.stdout.write(self._info(f"serial           : {details['serial']}"))
        self.stdout.write(self._info(f"issuer           : {details['issuer']}"))
        self.stdout.write(self._info(f"expires          : {details['expires_at']}"))

        expires = details["expires_at"]
        if expires:
            try:
                now = datetime.datetime.now(datetime.timezone.utc)
                days = (expires - now).days
                if days < 0:
                    self.stdout.write(self._bad(
                        "the signing certificate has EXPIRED — seals will fail"
                    ))
                elif days < 30:
                    self.stdout.write(self._warn(
                        f"certificate expires in {days} day(s) — plan a rotation"
                    ))
            except Exception:
                pass

        # -- end-to-end self-test -------------------------------------
        self.stdout.write("\nSelf-test: sign a throwaway PDF and validate it")
        self.stdout.write("-" * 62)

        sample = self._sample_pdf()
        sealed, info = P.seal_pdf(sample, None)

        if info["status"] != "sealed":
            self.stdout.write(self._bad(
                f"sealing did not succeed ({info['status']}): {info['detail']}"
            ))
            return

        achieved = (info["level_achieved"] or "").upper()
        target = (info["level_requested"] or "").upper()
        if info["level_achieved"] == info["level_requested"]:
            self.stdout.write(self._ok(f"sealed at PAdES {achieved}"))
        else:
            self.stdout.write(self._warn(
                f"sealed at PAdES {achieved}, below the target {target}"
            ))
            self.stdout.write(self._info(info["detail"]))

        self.stdout.write(self._info(
            f"size             : {len(sample)} -> {len(sealed)} bytes"
        ))
        self.stdout.write(self._info(f"sha256           : {info['document_sha256']}"))

        result = P.verify_pdf(sealed)
        if not result["signatures"]:
            self.stdout.write(self._bad(
                f"no signature found after sealing: {result['error']}"
            ))
            return

        for row in result["signatures"]:
            self.stdout.write(self._ok(f"signature field  : {row['field_name']}"))
            self.stdout.write(self._info(f"intact           : {row['intact']}"))
            self.stdout.write(self._info(f"coverage         : {row['coverage']}"))
            self.stdout.write(self._info(f"trusted here     : {row['trusted']}"))
            self.stdout.write(self._info(f"summary          : {row['summary']}"))

        if not result["ok"]:
            self.stdout.write(self._bad("the sealed PDF did NOT validate"))
            return

        if not any(r["trusted"] for r in result["signatures"]):
            self.stdout.write(self._warn(
                "intact but not trusted by UN PASS itself. Expected for a "
                "self-signed test seal; for a real certificate add its root "
                "to ESIGN_PADES_TRUST_ROOTS. Recipients' readers use their "
                "own trust stores either way."
            ))

        # -- tamper check ---------------------------------------------
        # Flip the marker inside the signed byte range and confirm validation
        # now fails. This is the property the whole feature exists for, so it
        # is asserted rather than assumed.
        marker = self.TAMPER_MARKER
        at = sealed.find(marker)
        if at <= 0:
            self.stdout.write(self._warn(
                "tamper check skipped — the marker was not found in the raw "
                "bytes (the content stream may be compressed)."
            ))
        else:
            broken = bytearray(sealed)
            broken[at:at + len(marker)] = b"X" * len(marker)
            after = P.verify_pdf(bytes(broken))
            if after["ok"]:
                self.stdout.write(self._bad(
                    "a modified PDF still validated — this should not happen. "
                    "Do not rely on these seals; raise it with the maintainer."
                ))
            else:
                self.stdout.write(self._ok(
                    "tamper check: a modified copy correctly fails validation"
                ))

        self.stdout.write(self.style.SUCCESS(
            "\nNES PAdES is working. Completed envelopes will be sealed.\n"
        ))

    #: Plain-text marker the tamper check flips. `pageCompression=0` keeps the
    #: content stream uncompressed so the marker is findable in the raw bytes;
    #: with compression on it is Flate-encoded and the check silently skips.
    TAMPER_MARKER = b"NES PAdES self-test"

    def _sample_pdf(self) -> bytes:
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pageCompression=0)
        c.drawString(72, 740, self.TAMPER_MARKER.decode())
        c.drawString(72, 720, "This file is generated, signed and discarded.")
        c.showPage()
        c.save()
        return buf.getvalue()

    # ------------------------------------------------------------------
    # --make-dev-cert
    # ------------------------------------------------------------------
    def make_dev_cert(self, path: str, passphrase: str):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import NameOID
        from django.conf import settings

        if os.path.exists(path):
            raise CommandError(
                f"{path} already exists — refusing to overwrite a key. "
                "Move it aside first."
            )

        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        brand = getattr(settings, "ESIGN_BRAND", "NES")

        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "UN PASS"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, f"{brand} Document Seal (TEST)"
            ),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=730))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=True,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CODE_SIGNING]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        blob = pkcs12.serialize_key_and_certificates(
            name=b"nes-dev-seal",
            key=key,
            cert=cert,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(
                passphrase.encode()
            ),
        )
        with open(path, "wb") as fh:
            fh.write(blob)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(f"\nWrote a TEST seal to {path}"))
        self.stdout.write(
            "\nThis certificate is self-signed. It proves the pipeline works and "
            "it will\ndetect tampering, but no reader will show it as trusted. "
            "Do not use it\nfor documents that matter — obtain a certificate "
            "from a CA your\nrecipients trust.\n"
            "\nAdd to settings.py:\n"
            f'    ESIGN_PADES_ENABLED = True\n'
            f'    ESIGN_PADES_PKCS12 = "{path}"\n'
            f'    ESIGN_PADES_PKCS12_PASSPHRASE = "{passphrase}"\n'
            f'    ESIGN_PADES_LEVEL = "b-b"\n'
            "\nThen:  python manage.py esign_pades --check\n"
        )

    # ------------------------------------------------------------------
    # --seal-envelope / --verify-envelope
    # ------------------------------------------------------------------
    def _envelope(self, envelope_id: str):
        from accounts.models_esign import Envelope

        env = (
            Envelope.objects.filter(envelope_id=envelope_id).first()
            or Envelope.objects.filter(envelope_id__istartswith=envelope_id).first()
        )
        if env is None:
            raise CommandError(f"No envelope matches {envelope_id!r}.")
        return env

    def _stored_pdf(self, envelope) -> bytes:
        if not envelope.completed_pdf:
            raise CommandError(
                f"Envelope {envelope.envelope_id} has no completed PDF "
                f"(status: {envelope.status})."
            )
        envelope.completed_pdf.open("rb")
        try:
            return envelope.completed_pdf.read()
        finally:
            envelope.completed_pdf.close()

    def seal_envelope(self, envelope_id: str):
        from django.core.files.base import ContentFile
        from accounts import pades_esign as P
        from accounts.models_esign import Envelope
        from accounts.utils_esign import log_event

        env = self._envelope(envelope_id)
        if env.status != Envelope.STATUS_COMPLETED:
            raise CommandError(
                f"Envelope {env.envelope_id} is {env.status}, not completed. "
                "Only completed envelopes are sealed."
            )

        raw = self._stored_pdf(env)

        existing = P.verify_pdf(raw)
        if existing["signatures"]:
            self.stdout.write(self.style.WARNING(
                f"{env.envelope_id} already carries "
                f"{len(existing['signatures'])} embedded signature(s). "
                "Sealing again would add another."
            ))

        sealed, info = P.seal_pdf(raw, env)
        if info["status"] != "sealed":
            raise CommandError(
                f"Sealing failed ({info['status']}): {info['detail']}"
            )

        env.completed_pdf.save(
            f"{env.envelope_id}-signed.pdf", ContentFile(sealed), save=False
        )
        env.save(update_fields=["completed_pdf"])
        P.record_seal(env, info)
        log_event(
            env, "sealed",
            note=f"Re-sealed with PAdES {(info['level_achieved'] or '').upper()} "
                 f"from the command line"[:255],
            meta={"level": info["level_achieved"],
                  "sha256": info["document_sha256"]},
        )

        self.stdout.write(self.style.SUCCESS(
            f"Sealed {env.envelope_id} at PAdES "
            f"{(info['level_achieved'] or '').upper()} "
            f"({len(raw)} -> {len(sealed)} bytes)"
        ))

    def verify_envelope(self, envelope_id: str):
        from accounts import pades_esign as P

        env = self._envelope(envelope_id)
        raw = self._stored_pdf(env)
        result = P.verify_pdf(raw)

        self.stdout.write(f"\nEnvelope {env.envelope_id} — {env.subject}")
        self.stdout.write("=" * 62)
        self.stdout.write(self._info(f"stored sha256    : {P.sha256_of(raw)}"))

        if not result["signatures"]:
            self.stdout.write(self._warn(
                result["error"] or "No embedded signature."
            ))
            self.stdout.write(self._info(
                "The visible signatures and the audit trail are unaffected. "
                "Run --seal-envelope to add a seal."
            ))
            return

        for row in result["signatures"]:
            self.stdout.write(self._ok(f"field            : {row['field_name']}"))
            self.stdout.write(self._info(f"intact           : {row['intact']}"))
            self.stdout.write(self._info(f"coverage         : {row['coverage']}"))
            self.stdout.write(self._info(f"trusted here     : {row['trusted']}"))
            self.stdout.write(self._info(f"signer           : {row['signer']}"))
            self.stdout.write(self._info(f"signed at        : {row['signed_at']}"))
            self.stdout.write(self._info(f"summary          : {row['summary']}"))
            if row["error"]:
                self.stdout.write(self._warn(row["error"]))

        self.stdout.write(
            self.style.SUCCESS("\nThe document is intact.\n") if result["ok"]
            else self.style.ERROR("\nThe document did NOT validate.\n")
        )

    # ------------------------------------------------------------------
    # --verify-chain
    # ------------------------------------------------------------------
    def verify_chain_cmd(self, envelope_id: str):
        from accounts.audit_chain_esign import verify_anchor, verify_chain
        from accounts.models_esign_pades import EnvelopeSeal

        env = self._envelope(envelope_id)
        state = verify_chain(env)
        seal = EnvelopeSeal.objects.filter(envelope=env).first()
        anchor = verify_anchor(env, seal)

        self.stdout.write(f"\nAudit trail — {env.envelope_id}")
        self.stdout.write("=" * 62)
        self.stdout.write(self._info(f"entries total    : {state['total']}"))
        self.stdout.write(self._info(f"chained          : {state['chained']}"))
        if state["unchained"]:
            self.stdout.write(self._warn(
                f"{state['unchained']} entr"
                f"{'y' if state['unchained'] == 1 else 'ies'} predate the "
                "hash chain and cannot be checked — not a sign of tampering."
            ))
        self.stdout.write(self._info(f"head             : {state['head'] or '—'}"))

        if state["problems"]:
            self.stdout.write(self._bad("the trail has been altered:"))
            for p in state["problems"]:
                self.stdout.write(self._info(
                    f"[{p['kind']}] event {p.get('event_id')} "
                    f"({p.get('event')}) — {p['detail']}"
                ))
        elif state["chained"]:
            self.stdout.write(self._ok("every chained entry verifies"))

        self.stdout.write("\nAnchor")
        self.stdout.write("-" * 62)
        if not anchor["present"]:
            self.stdout.write(self._warn(anchor["detail"]))
            self.stdout.write(self._info(
                "Without an anchor, entries removed from the END of the trail "
                "cannot be detected."
            ))
        else:
            self.stdout.write(self._info(
                f"signature intact : {anchor['signature_intact']}"
            ))
            self.stdout.write(self._info(
                f"head matches     : {anchor['head_matches']}"
            ))
            self.stdout.write(self._info(
                f"length matches   : {anchor['length_matches']}"
            ))
            if anchor["truncated"]:
                self.stdout.write(self._bad(anchor["detail"]))
            elif anchor["detail"]:
                self.stdout.write(self._warn(anchor["detail"]))

        self.stdout.write(
            self.style.SUCCESS("\nThe audit trail is intact.\n")
            if state["ok"] and (not anchor["present"] or anchor["ok"])
            else self.style.ERROR("\nThe audit trail did NOT verify.\n")
        )

    # ------------------------------------------------------------------
    # --test-tsa
    # ------------------------------------------------------------------
    def test_tsa_cmd(self, url: str):
        from accounts import pades_esign as P

        cfg = P.pades_config()
        if url == "__configured__":
            url = cfg["tsa_url"]
            if not url:
                raise CommandError(
                    "No ESIGN_PADES_TSA_URL is configured. Pass a URL to "
                    "test one before committing to it, e.g.\n"
                    "  python manage.py esign_pades "
                    "--test-tsa http://timestamp.digicert.com"
                )

        self.stdout.write(f"\nTimestamp authority test — {url}")
        self.stdout.write("=" * 62)
        self.stdout.write(self._info("asking for a timestamp over a random "
                                     "digest..."))

        result = P.probe_tsa(
            url,
            username=cfg["tsa_username"],
            password=cfg["tsa_password"],
        )

        if not result["ok"]:
            self.stdout.write(self._bad(f"no usable timestamp: {result['error']}"))
            self.stdout.write(self._info(
                "Common causes: the server has no outbound access to this "
                "host or port;\n        the URL needs a path (many "
                "authorities are not served at /);\n        the authority "
                "requires credentials or an account."
            ))
            return

        self.stdout.write(self._ok(
            f"timestamp issued ({result['token_bytes']} bytes)"
        ))
        self.stdout.write(self._info(f"time asserted    : {result['gen_time']}"))
        if result["skew_seconds"] is not None:
            self.stdout.write(self._info(
                f"clock difference : {result['skew_seconds']:.1f}s vs this server"
            ))
        self.stdout.write(self._info(f"policy OID       : {result['policy_oid'] or '—'}"))
        self.stdout.write(self._info(f"accuracy         : {result['accuracy'] or 'not stated'}"))
        self.stdout.write(self._info(f"serial           : {result['serial']}"))
        self.stdout.write(self._info(f"authority        : {result['signer_subject'] or '—'}"))
        self.stdout.write(self._info(f"issued by        : {result['signer_issuer'] or '—'}"))
        self.stdout.write(self._info(f"its cert expires : {result['signer_not_after']}"))
        self.stdout.write(self._info(f"certs in token   : {result['certs_in_token']}"))

        if result["trusted_here"] is True:
            self.stdout.write(self._ok("its chain validates against your trust roots"))
        elif result["trusted_here"] is False:
            self.stdout.write(self._warn("its chain does NOT validate here"))

        for note in result["notes"]:
            self.stdout.write(self._warn(note))

        self.stdout.write(self.style.SUCCESS(
            "\nThis authority works. To use it:\n"
            f'    ESIGN_PADES_TSA_URL = "{url}"\n'
            '    ESIGN_PADES_LEVEL = "b-lt"\n'
            "\nThen re-run --check to confirm sealing reaches B-LT.\n"
        ))

    # ------------------------------------------------------------------
    # --refresh-ltv
    # ------------------------------------------------------------------
    def refresh_ltv_cmd(self, target: str, *, force: bool, limit: int):
        from accounts import pades_esign as P

        self.stdout.write("\nArchival refresh (PAdES long-term validity)")
        self.stdout.write("=" * 62)

        cfg = P.pades_config()
        if not cfg["tsa_url"]:
            self.stdout.write(self._warn(
                "No ESIGN_PADES_TSA_URL. Document timestamps are what keep a "
                "signature verifiable once its certificate expires, and they "
                "need a timestamp authority. Refresh can still embed "
                "revocation data, but cannot reach B-LTA."
            ))

        if target == "__due__":
            seals = list(P.seals_due_for_refresh(limit=limit))
            if not seals:
                self.stdout.write(self._ok("nothing is due for refresh"))
                return
            self.stdout.write(self._info(
                f"{len(seals)} seal(s) due"
            ))
            envelopes = [s.envelope for s in seals]
        else:
            envelopes = [self._envelope(target)]

        changed = failed = 0
        for env in envelopes:
            report = P.refresh_ltv(env, force=force)
            label = f"{env.envelope_id}"
            if report["ok"] and report["changed"]:
                changed += 1
                self.stdout.write(self._ok(
                    f"{label}: {report['level_before'] or '—'} -> "
                    f"{report['level_after']}"
                ))
            elif report["ok"]:
                self.stdout.write(self._info(
                    f"      {label}: no change — {report['detail']}"
                ))
            else:
                failed += 1
                self.stdout.write(self._bad(f"{label}: {report['detail']}"))

        self.stdout.write(
            f"\n{changed} refreshed, {failed} failed, "
            f"{len(envelopes) - changed - failed} unchanged\n"
        )

    # ------------------------------------------------------------------
    # --evidence
    # ------------------------------------------------------------------
    def evidence_cmd(self, envelope_id: str, out_path: str | None):
        from accounts.evidence_esign import envelope_evidence

        env = self._envelope(envelope_id)
        blob, report = envelope_evidence(env)
        path = out_path or f"{env.envelope_id}-evidence.asice.zip"
        with open(path, "wb") as fh:
            fh.write(blob)

        self.stdout.write(f"\nEvidence package — {env.envelope_id}")
        self.stdout.write("=" * 62)
        self.stdout.write(self._ok(f"wrote {path} ({len(blob)} bytes)"))
        self.stdout.write(self._info(f"entries          : {report['files']}"))
        self.stdout.write(self._info(
            f"document valid   : {report.get('validation_ok')}"
        ))
        self.stdout.write(self._info(
            f"audit chain ok   : {report.get('chain_ok')}"
        ))
        self.stdout.write(self._info(
            f"anchor ok        : {report.get('anchor_ok')}"
        ))
        for note in report.get("notes", []):
            self.stdout.write(self._warn(note))
        self.stdout.write(
            "\nThe package verifies offline — see VERIFY.txt inside it.\n"
        )

    # ------------------------------------------------------------------
    # --register
    # ------------------------------------------------------------------
    def register_cmd(self, days_text: str, out_path: str | None):
        from datetime import timedelta

        from django.utils import timezone

        from accounts.evidence_esign import build_register

        try:
            days = max(1, int(days_text))
        except (TypeError, ValueError):
            raise CommandError(f"--register expects a number of days, got {days_text!r}")

        until = timezone.now()
        since = until - timedelta(days=days)
        blob, report = build_register(since=since, until=until)

        stamp = until.strftime("%Y%m%d")
        path = out_path or f"nes-seal-register-{stamp}.pdf"
        with open(path, "wb") as fh:
            fh.write(blob)

        json_path = path.rsplit(".", 1)[0] + ".json"
        with open(json_path, "wb") as fh:
            fh.write(report["json"])

        self.stdout.write(f"\nSeal register — last {days} day(s)")
        self.stdout.write("=" * 62)
        self.stdout.write(self._ok(f"wrote {path} ({len(blob)} bytes)"))
        self.stdout.write(self._ok(f"wrote {json_path}"))
        self.stdout.write(self._info(f"documents listed : {report['entry_count']}"))
        self.stdout.write(self._info(f"merkle root      : {report['merkle_root'] or '—'}"))
        self.stdout.write(self._info(f"register sha256  : {report['register_sha256']}"))

        if report["seal_status"] == "sealed":
            self.stdout.write(self._ok(
                f"register sealed at PAdES "
                f"{(report['seal_level'] or '').upper()}"
            ))
        else:
            self.stdout.write(self._warn(
                f"the register itself is not sealed: {report['seal_detail']}"
            ))

        self.stdout.write(
            "\nPublish this where it will outlive the system — the records "
            "registry, a\nrepository, or printed. A register entry proves a "
            "document existed in\nexactly that form by this date, even after "
            "every certificate expires.\n"
        )
