import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';

class ApiException implements Exception {
  ApiException(this.message,
      {this.status = 0, this.signedOut = false, this.stale = false, this.mustChangePassword = false});
  final String message;
  final int status;
  final bool signedOut;
  final bool stale;

  /// The account is flagged to change its password. Everything except signing
  /// in is refused until that is done, in a browser.
  final bool mustChangePassword;

  @override
  String toString() => message;
}

class Api {
  Api._();
  static final Api instance = Api._();

  static const _store = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
  );

  static const _kBase = 'base_url';
  static const _kCookie = 'session_cookie';
  static const _kDevice = 'device_id';
  static const _kName = 'device_name';

  String? _baseUrl;
  String? _cookie;
  String? _deviceId;
  String? _deviceName;

  String get baseUrl => _baseUrl ?? '';
  bool get hasSession => (_cookie ?? '').isNotEmpty;
  String get deviceName => _deviceName ?? 'Android phone';

  Future<void> load() async {
    _baseUrl = await _store.read(key: _kBase);
    _cookie = await _store.read(key: _kCookie);
    _deviceId = await _store.read(key: _kDevice);
    _deviceName = await _store.read(key: _kName);
    await deviceId();
  }

  Future<void> setBaseUrl(String url) async {
    var clean = url.trim();
    if (clean.isEmpty) {
      throw ApiException('Enter the address of your UN PASS site.');
    }
    if (!clean.startsWith('http')) clean = 'https://$clean';
    clean = clean.replaceAll(RegExp(r'/+$'), '');
    _baseUrl = clean;
    await _store.write(key: _kBase, value: clean);
  }

  Future<String> deviceId() async {
    if ((_deviceId ?? '').isNotEmpty) return _deviceId!;

    final parts = <String>[];
    try {
      final info = await DeviceInfoPlugin().androidInfo;
      parts.addAll([
        info.id,
        info.fingerprint,
        info.model,
        info.manufacturer,
        info.hardware,
      ]);
      _deviceName =
          '${info.manufacturer} ${info.model} · Android ${info.version.release}';
    } catch (_) {
      _deviceName = Platform.operatingSystem;
    }

    try {
      final pkg = await PackageInfo.fromPlatform();
      parts.add(pkg.packageName);
    } catch (_) {}

    final existing = await _store.read(key: 'device_salt');
    final salt =
        existing ?? DateTime.now().microsecondsSinceEpoch.toRadixString(36);
    if (existing == null) {
      await _store.write(key: 'device_salt', value: salt);
    }
    parts.add(salt);

    _deviceId = sha256
        .convert(utf8.encode(parts.join('|')))
        .toString()
        .substring(0, 48);

    await _store.write(key: _kDevice, value: _deviceId!);
    await _store.write(key: _kName, value: _deviceName!);
    return _deviceId!;
  }

  Uri _uri(String path, [Map<String, String>? query]) {
    if ((_baseUrl ?? '').isEmpty) {
      throw ApiException(
        'No site address is set. Open Settings and enter it.',
      );
    }
    return Uri.parse('$_baseUrl/accounts/api/m$path')
        .replace(queryParameters: query);
  }

  Map<String, String> get _headers => {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-UNPASS-App': 'android',
        if ((_cookie ?? '').isNotEmpty) 'Cookie': _cookie!,
      };

  void _keepCookie(http.Response res) {
    final raw = res.headers['set-cookie'];
    if (raw == null) return;

    for (final piece in raw.split(RegExp(r',(?=[^;]+?=)'))) {
      final first = piece.split(';').first.trim();
      if (first.startsWith('sessionid=')) {
        _cookie = first;
        _store.write(key: _kCookie, value: first);
      }
    }
  }

  Future<Map<String, dynamic>> _send(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Map<String, String>? query,
  }) async {
    late http.Response res;

    try {
      final uri = _uri(path, query);
      res = method == 'GET'
          ? await http
              .get(uri, headers: _headers)
              .timeout(const Duration(seconds: 25))
          : await http
              .post(uri, headers: _headers, body: jsonEncode(body ?? {}))
              .timeout(const Duration(seconds: 30));
    } on SocketException {
      throw ApiException(
        "Can't reach UN PASS. Check your connection, or the site address in Settings.",
      );
    } on HttpException {
      throw ApiException("Can't reach UN PASS. Check your connection.");
    } catch (e) {
      if (e is ApiException) rethrow;
      throw ApiException('The request timed out. Try again in a moment.');
    }

    _keepCookie(res);

    final type = res.headers['content-type'] ?? '';
    if (!type.contains('json')) {
      // A page came back where JSON was expected. The status says why, and
      // these are very different problems — so name the right one rather than
      // blaming the address for all of them.
      lastDiagnosis = 'HTTP ${res.statusCode} · ${type.isEmpty ? 'no content type' : type} · $path';
      String reason;
      switch (res.statusCode) {
        case 404:
          reason = "This server doesn't have the phone app's API yet. "
              "Ask ICT to deploy the latest code and restart the site.";
          break;
        case 400:
          reason = "The server rejected the address the app used. "
              "ICT should check ALLOWED_HOSTS covers ${Uri.parse(_baseUrl ?? '').host}.";
          break;
        case 401:
        case 403:
          reason = 'You have been signed out. Sign in again.';
          await clearSession();
          break;
        case 405:
          reason = 'The server refused that request. This usually means an out-of-date '
              'version of the app or the site — ICT can check both are up to date.';
          break;
        case 502:
        case 503:
        case 504:
          reason = 'The site is not responding. Try again shortly.';
          break;
        default:
          reason = res.statusCode >= 500
              ? 'The server hit a problem (HTTP ${res.statusCode}). Try again shortly.'
              : res.statusCode >= 300
                  ? 'You have been signed out. Sign in again.'
                  : "That address doesn't look like a UN PASS site. Check it in Settings.";
          if (res.statusCode >= 300 && res.statusCode < 400) await clearSession();
      }
      throw ApiException(reason,
          status: res.statusCode, signedOut: res.statusCode == 401 || res.statusCode == 403);
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    if (res.statusCode >= 400 || data['ok'] == false) {
      if (data['signed_out'] == true) await clearSession();
      throw ApiException(
        (data['error'] as String?) ?? 'Something went wrong. Please try again.',
        status: res.statusCode,
        mustChangePassword: data['must_change_password'] == true,
        signedOut: data['signed_out'] == true,
        stale: data['stale'] == true,
      );
    }

    return data;
  }

  Future<void> clearSession() async {
    _cookie = null;
    await _store.delete(key: _kCookie);
  }

  // ── signing in ────────────────────────────────────────────────────────────

  /// The technical detail behind the last failure — status, content type and
  /// path. Shown on the connection check so ICT has something to act on.
  String lastDiagnosis = '';

  Future<Map<String, dynamic>> ping() => _send('GET', '/ping/');

  /// Ask the server what it is, and report plainly what came back. Used by the
  /// "Check the connection" button so a problem can be identified without
  /// anyone reading a log.
  Future<Map<String, String>> checkConnection() async {
    final target = '${_baseUrl ?? ''}/accounts/api/m/ping/';
    if ((_baseUrl ?? '').isEmpty) {
      return {'result': 'No address', 'detail': 'Enter your site address first.', 'ok': 'no'};
    }
    try {
      final res = await http
          .get(Uri.parse(target), headers: {'Accept': 'application/json', if ((_cookie ?? '').isNotEmpty) 'Cookie': _cookie!})
          .timeout(const Duration(seconds: 20));
      final type = res.headers['content-type'] ?? 'unknown';
      if (res.statusCode == 200 && type.contains('json')) {
        final data = jsonDecode(res.body) as Map<String, dynamic>;
        return {
          'result': 'Connected',
          'detail': 'UN PASS API ${data['api']} · ${data['signed_in'] == true ? 'signed in' : 'not signed in'}',
          'ok': 'yes',
        };
      }
      if (res.statusCode == 404) {
        return {
          'result': 'API not installed',
          'detail': 'The site answered, but has no phone API at $target. '
              'ICT should deploy the latest code and restart.',
          'ok': 'no',
        };
      }
      return {
        'result': 'Unexpected answer',
        'detail': 'HTTP ${res.statusCode}, $type from $target',
        'ok': 'no',
      };
    } catch (e) {
      return {'result': 'Cannot reach the site', 'detail': '$target\n$e', 'ok': 'no'};
    }
  }

  /// [identifier] can be either the UNPASS username or registered email.
  /// The server only returns otp_required=true after email delivery has been
  /// accepted by the configured Django email backend.
  Future<Map<String, dynamic>> login(
    String identifier,
    String password,
  ) async =>
      _send('POST', '/auth/login/', body: {
        'identifier': identifier,
        // Backwards-compatible key while the server update is being deployed.
        'username': identifier,
        'password': password,
        'device_id': await deviceId(),
        'device_name': deviceName,
      });

  Future<Map<String, dynamic>> verify(
    String identifier,
    String password,
    String code,
  ) async =>
      _send('POST', '/auth/verify/', body: {
        'identifier': identifier,
        'username': identifier,
        'password': password,
        'code': code,
        'device_id': await deviceId(),
        'device_name': deviceName,
      });

  Future<void> resend(
    String identifier,
    String password,
  ) async =>
      _send('POST', '/auth/resend/', body: {
        'identifier': identifier,
        'username': identifier,
        'password': password,
        'device_id': await deviceId(),
      });

  Future<void> logout({bool forgetDevice = false}) async {
    try {
      await _send(
        'POST',
        '/auth/logout/',
        body: {
          'device_id': await deviceId(),
          'forget_device': forgetDevice,
        },
      );
    } on ApiException {
      // Signing out locally matters more than the round trip succeeding.
    }
    await clearSession();
  }

  Future<Map<String, dynamic>> me() => _send('GET', '/me/');

  // ── eSign ─────────────────────────────────────────────────────────────────

  Future<List<dynamic>> inbox() async =>
      (await _send('GET', '/inbox/'))['items'] as List<dynamic>;

  Future<Map<String, dynamic>> task(String token) async =>
      (await _send('GET', '/tasks/$token/'))['task']
          as Map<String, dynamic>;

  Future<String> decide(
    String token,
    String action, {
    String comment = '',
  }) async {
    final data = await _send(
      'POST',
      '/tasks/$token/decide/',
      body: {'action': action, 'comment': comment},
    );
    return (data['message'] as String?) ?? 'Done.';
  }

  // ── assets ────────────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> lookupAsset(String code) =>
      _send('POST', '/assets/lookup/', body: {'code': code});

  Future<List<dynamic>> searchAssets(String q) async =>
      (await _send(
        'GET',
        '/assets/search/',
        query: {'q': q},
      ))['results'] as List<dynamic>;

  Future<List<dynamic>> myAssets() async =>
      (await _send('GET', '/assets/mine/'))['assets'] as List<dynamic>;

  // ── envelopes and signing ────────────────────────────────────────────────

  Future<List<dynamic>> envelopes({String status = '', String q = ''}) async {
    final query = <String, String>{};
    if (status.isNotEmpty) query['status'] = status;
    if (q.isNotEmpty) query['q'] = q;
    return (await _send('GET', '/envelopes/', query: query.isEmpty ? null : query))['envelopes'] as List<dynamic>;
  }

  Future<Map<String, dynamic>> envelope(int id) async {
    final data =
        (await _send('GET', '/envelopes/$id/'))['envelope']
            as Map<String, dynamic>;

    // The current server response uses an old/non-existent URL shape for the
    // final signed PDF. Keep the correction client-side too, so the dev app
    // remains able to open signed PDFs even before api_mobile.py is changed.
    data['final_url'] = '/accounts/esign/$id/download/final/';
    return data;
  }

  Future<Map<String, dynamic>> signSheet(String token) => _send('GET', '/sign/$token/');

  /// [signature] is either a drawing as a data URL, or `saved:<id>` for one
  /// already set up in Signature Studio on the website.
  Future<String> sign(String token,
      {required String signature, String initials = '', bool saveSignature = false,
      Map<String, dynamic> fields = const {}}) async {
    final data = await _send('POST', '/sign/$token/submit/', body: {
      'signature': signature,
      'initials': initials,
      'consent': true,
      'save_signature': saveSignature,
      'fields': fields,
    });
    return (data['message'] as String?) ?? 'Signed.';
  }

  Future<void> decline(String token, String reason) =>
      _send('POST', '/sign/$token/decline/', body: {'reason': reason});

  // ── room booking ──────────────────────────────────────────────────────────

  Future<List<dynamic>> rooms({String q = ''}) async {
    final query = <String, String>{};
    if (q.trim().isNotEmpty) query['q'] = q.trim();
    return (await _send(
      'GET',
      '/rooms/',
      query: query.isEmpty ? null : query,
    ))['rooms'] as List<dynamic>;
  }

  Future<List<dynamic>> roomBookings() async =>
      (await _send('GET', '/rooms/bookings/'))['bookings'] as List<dynamic>;

  Future<Map<String, dynamic>> bookRoom(
    int roomId, {
    required String title,
    required String date,
    required String startTime,
    required String endTime,
    String description = '',
    String ictSupport = 'none',
    String attendeeEmails = '',
    String virtualMeetingLink = '',
    List<int> amenityIds = const [],
    bool enableAttendance = false,
    bool enableInviteLink = false,
    bool autoAcceptRegistration = false,
  }) =>
      _send('POST', '/rooms/$roomId/book/', body: {
        'title': title,
        'description': description,
        'date': date,
        'start_time': startTime,
        'end_time': endTime,
        'ict_support': ictSupport,
        'attendee_emails': attendeeEmails,
        'virtual_meeting_link': virtualMeetingLink,
        'amenity_ids': amenityIds,
        'enable_attendance': enableAttendance,
        'enable_invite_link': enableInviteLink,
        'auto_accept_registration': autoAcceptRegistration,
      });

  Future<void> cancelRoomBooking(int id) async {
    await _send('POST', '/rooms/bookings/$id/cancel/');
  }

  // ── forms ────────────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> forms() => _send('GET', '/forms/');

  Future<Map<String, dynamic>> form(int id) async =>
      (await _send('GET', '/forms/$id/'))['form'] as Map<String, dynamic>;

  Future<Map<String, dynamic>> submitForm(int id,
          {required Map<String, dynamic> values, Map<String, String> slots = const {}}) =>
      _send('POST', '/forms/$id/submit/', body: {'values': values, 'slots': slots});

  Future<Map<String, dynamic>> submission(int id) async =>
      (await _send('GET', '/submissions/$id/'))['submission'] as Map<String, dynamic>;

  // ── flows and runs ───────────────────────────────────────────────────────

  Future<List<dynamic>> flows() async => (await _send('GET', '/flows/'))['flows'] as List<dynamic>;

  Future<List<dynamic>> runs({String status = 'open'}) async =>
      (await _send('GET', '/runs/', query: {'status': status}))['runs'] as List<dynamic>;

  Future<Map<String, dynamic>> run(int id) async =>
      (await _send('GET', '/runs/$id/'))['run'] as Map<String, dynamic>;

  Future<void> cancelRun(int id, String reason) =>
      _send('POST', '/runs/$id/cancel/', body: {'reason': reason});

  /// Fetch a PDF using the app's own session.
  ///
  /// This is why documents are read in the app rather than handed to the
  /// browser: the browser carries no session cookie, so it would meet a
  /// sign-in page instead of the document.
  Future<List<int>> fetchPdf(String path) async {
    if ((_baseUrl ?? '').isEmpty) throw ApiException('No site address is set.');
    final uri = Uri.parse(path.startsWith('http') ? path : '$_baseUrl$path');
    late http.Response res;
    try {
      res = await http.get(uri, headers: {
        'Accept': 'application/pdf',
        if ((_cookie ?? '').isNotEmpty) 'Cookie': _cookie!,
      }).timeout(const Duration(seconds: 60));
    } on SocketException {
      throw ApiException("Can't reach UN PASS. Check your connection.");
    } catch (_) {
      throw ApiException('The document took too long to arrive. Try again.');
    }
    _keepCookie(res);
    final type = (res.headers['content-type'] ?? '').toLowerCase();
    if (res.statusCode == 401 || res.statusCode == 403) {
      await clearSession();
      throw ApiException('You have been signed out. Sign in again.', signedOut: true);
    }
    if (res.statusCode >= 400) {
      throw ApiException('The document could not be fetched (HTTP ${res.statusCode}).');
    }

    final looksLikePdf = res.bodyBytes.length >= 5 &&
        res.bodyBytes[0] == 0x25 &&
        res.bodyBytes[1] == 0x50 &&
        res.bodyBytes[2] == 0x44 &&
        res.bodyBytes[3] == 0x46 &&
        res.bodyBytes[4] == 0x2D; // "%PDF-"

    if (!looksLikePdf) {
      if (!type.contains('pdf')) {
        throw ApiException(
          'That download was not a PDF. It may be a sign-in/error page.',
        );
      }
      throw ApiException(
        'UN PASS returned a response labelled as PDF, but the file itself is invalid. '
        'The signed PDF should be regenerated on the server.',
      );
    }

    return res.bodyBytes;
  }

  String webUrl(String path) => '$_baseUrl$path';
}
