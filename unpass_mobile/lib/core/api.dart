import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';

class ApiException implements Exception {
  ApiException(this.message, {this.status = 0, this.signedOut = false, this.stale = false});
  final String message;
  final int status;
  final bool signedOut;
  final bool stale;

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
      throw ApiException(
        res.statusCode >= 500
            ? 'The server hit a problem. Try again shortly.'
            : "That address doesn't look like a UN PASS site. Check it in Settings.",
        status: res.statusCode,
      );
    }

    final data = jsonDecode(res.body) as Map<String, dynamic>;
    if (res.statusCode >= 400 || data['ok'] == false) {
      if (data['signed_out'] == true) await clearSession();
      throw ApiException(
        (data['error'] as String?) ?? 'Something went wrong. Please try again.',
        status: res.statusCode,
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

  Future<Map<String, dynamic>> ping() => _send('GET', '/ping/');

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

  String webUrl(String path) => '$_baseUrl$path';
}
