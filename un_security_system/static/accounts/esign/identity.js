/* accounts/esign/identity.js — NES signing identity, browser side.
 *
 * This file is where "sole control" is actually implemented. Everything it
 * does happens in the signatory's own browser:
 *
 *   enrol   generate an ECDSA P-256 keypair, encrypt the private key under a
 *           key derived from the passphrase, upload only the public key and
 *           the ciphertext
 *   sign    decrypt the private key with the passphrase, sign the server's
 *           challenge, upload only the signature
 *
 * The passphrase never crosses the network. The private key never crosses the
 * network in usable form. The server therefore cannot sign as the user, and
 * neither can anyone who takes the database or a backup.
 *
 * Honest limits, so nobody over-claims this:
 *
 *  - It relies on the page being served intact over HTTPS. A server that
 *    chose to serve modified JavaScript could capture a passphrase as it is
 *    typed. Defending against your own server needs a smartcard or an
 *    external signing device; this is the strongest arrangement available in
 *    a plain browser, and it rules out the far more likely threats: a stolen
 *    database, a backup tape, a curious administrator, a compromised host
 *    *after* the fact.
 *  - JavaScript strings cannot be wiped from memory. The passphrase is held
 *    in one local variable for as long as the operation takes and never
 *    stored, logged, or put in a form field, but "zeroise" is not something
 *    this platform can honestly offer.
 *  - A forgotten passphrase cannot be recovered, by anyone. That is the
 *    feature. Rotating to a new key keeps the lifetime signature ID.
 *
 * Requires a secure context (https, or localhost) — window.crypto.subtle is
 * unavailable otherwise, by browser policy rather than by our choice.
 */
(function (global) {
  "use strict";

  var ALGO = { name: "ECDSA", namedCurve: "P-256" };
  var SIGN_ALGO = { name: "ECDSA", hash: "SHA-256" };

  /* Work factor for turning a passphrase into a key. Higher is slower for
   * the signatory and much slower for anyone brute-forcing a stolen blob.
   * The server enforces its own floor and will reject anything lower. */
  var DEFAULT_ITERATIONS = 600000;

  function subtle() {
    if (!global.crypto || !global.crypto.subtle) {
      throw new Error(
        "This browser cannot create a signing key here. Signing keys need a " +
        "secure connection (https). Open UN PASS over https and try again."
      );
    }
    return global.crypto.subtle;
  }

  // -- encoding -----------------------------------------------------------
  function toB64(buffer) {
    var bytes = new Uint8Array(buffer);
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += String.fromCharCode(bytes[i]);
    }
    return global.btoa(out);
  }

  function fromB64(text) {
    var raw = global.atob(String(text || ""));
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes;
  }

  function utf8(text) {
    return new TextEncoder().encode(text);
  }

  function randomBytes(n) {
    var b = new Uint8Array(n);
    global.crypto.getRandomValues(b);
    return b;
  }

  // -- passphrase -> AES key ---------------------------------------------
  function deriveWrappingKey(passphrase, salt, iterations) {
    return subtle()
      .importKey("raw", utf8(passphrase), "PBKDF2", false, ["deriveKey"])
      .then(function (base) {
        return subtle().deriveKey(
          { name: "PBKDF2", salt: salt, iterations: iterations, hash: "SHA-256" },
          base,
          { name: "AES-GCM", length: 256 },
          false,
          ["encrypt", "decrypt"]
        );
      });
  }

  /* Rough guidance for a passphrase. Not a gate — a weak passphrase is the
   * signatory's own risk to take, and refusing to proceed tends to produce
   * a sticky note rather than a better passphrase. */
  function assessPassphrase(passphrase) {
    var value = String(passphrase || "");
    var classes =
      (/[a-z]/.test(value) ? 1 : 0) +
      (/[A-Z]/.test(value) ? 1 : 0) +
      (/[0-9]/.test(value) ? 1 : 0) +
      (/[^A-Za-z0-9]/.test(value) ? 1 : 0);
    var words = value.trim().split(/\s+/).filter(Boolean).length;

    if (value.length < 10) {
      return { ok: false, level: "weak",
        message: "Use at least 10 characters. Four unrelated words are easy " +
                 "to remember and hard to guess." };
    }
    if (value.length >= 20 || words >= 4 || classes >= 3) {
      return { ok: true, level: "strong", message: "Strong." };
    }
    return { ok: true, level: "fair",
      message: "Workable. Longer is better than more punctuation — there is " +
               "no reset if you forget it." };
  }

  // -- enrolment ----------------------------------------------------------
  /**
   * Create a signing key. Returns the payload to POST to the server.
   * Nothing secret is in the returned object.
   */
  function enroll(passphrase, options) {
    options = options || {};
    var iterations = options.iterations || DEFAULT_ITERATIONS;
    var salt = randomBytes(16);
    var iv = randomBytes(12);
    var keypair;

    return subtle()
      .generateKey(ALGO, true, ["sign", "verify"])
      .then(function (kp) {
        keypair = kp;
        return Promise.all([
          subtle().exportKey("spki", kp.publicKey),
          subtle().exportKey("pkcs8", kp.privateKey),
          deriveWrappingKey(passphrase, salt, iterations)
        ]);
      })
      .then(function (parts) {
        var spki = parts[0], pkcs8 = parts[1], wrapKey = parts[2];
        return subtle()
          .encrypt({ name: "AES-GCM", iv: iv }, wrapKey, pkcs8)
          .then(function (blob) {
            return {
              public_key_spki: toB64(spki),
              encrypted_private_key: toB64(blob),
              kdf_salt: toB64(salt),
              cipher_iv: toB64(iv),
              kdf_iterations: iterations,
              kdf: "PBKDF2-SHA256",
              cipher: "AES-GCM-256",
              algorithm: "ECDSA-P256-SHA256"
            };
          });
      });
  }

  // -- signing ------------------------------------------------------------
  /**
   * Sign a statement with the stored key.
   *
   * `material` is what the server handed back: encrypted_private_key,
   * kdf_salt, cipher_iv, kdf_iterations. The passphrase unlocks it here and
   * is not returned, stored or transmitted.
   *
   * Resolves to base64 of the raw 64-byte P1363 signature.
   */
  function signStatement(statement, material, passphrase) {
    var iv = fromB64(material.cipher_iv);
    var salt = fromB64(material.kdf_salt);
    var blob = fromB64(material.encrypted_private_key);
    var iterations = material.kdf_iterations || DEFAULT_ITERATIONS;

    return deriveWrappingKey(passphrase, salt, iterations)
      .then(function (wrapKey) {
        return subtle().decrypt({ name: "AES-GCM", iv: iv }, wrapKey, blob);
      })
      .catch(function () {
        /* AES-GCM authentication failed. The only realistic cause is a wrong
         * passphrase, so say that rather than leaking a crypto error. */
        throw new Error(
          "That passphrase did not unlock your signing key. There is no " +
          "reset — if you have lost it, you can create a new key, and your " +
          "signature ID stays the same."
        );
      })
      .then(function (pkcs8) {
        return subtle().importKey("pkcs8", pkcs8, ALGO, false, ["sign"]);
      })
      .then(function (privateKey) {
        return subtle().sign(SIGN_ALGO, privateKey, utf8(statement));
      })
      .then(function (signature) {
        return toB64(signature);
      });
  }

  /**
   * Local sanity check: does this passphrase unlock the key at all?
   * Lets the enrolment page confirm a key works before the person relies on
   * it, without involving the server.
   */
  function canUnlock(material, passphrase) {
    return signStatement("nes-selftest", material, passphrase)
      .then(function () { return true; })
      .catch(function () { return false; });
  }

  global.NESIdentity = {
    enroll: enroll,
    signStatement: signStatement,
    canUnlock: canUnlock,
    assessPassphrase: assessPassphrase,
    DEFAULT_ITERATIONS: DEFAULT_ITERATIONS,
    _toB64: toB64,
    _fromB64: fromB64
  };
})(typeof window !== "undefined" ? window : globalThis);
