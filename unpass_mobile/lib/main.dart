import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';

import 'core/api.dart';
import 'core/theme.dart';
import 'screens/home.dart';
import 'widgets/common.dart';
import 'widgets/connection_check.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
  await Api.instance.load();
  runApp(const UnPassApp());
}

class UnPassApp extends StatelessWidget {
  const UnPassApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'UN PASS',
      debugShowCheckedModeBanner: false,
      theme: unpassTheme(),
      home: const Gate(),
    );
  }
}

class Gate extends StatefulWidget {
  const Gate({super.key});

  @override
  State<Gate> createState() => _GateState();
}

class _GateState extends State<Gate> {
  bool _checking = true;
  Map<String, dynamic>? _user;

  @override
  void initState() {
    super.initState();
    _check();
  }

  Future<void> _check() async {
    if (Api.instance.baseUrl.isEmpty || !Api.instance.hasSession) {
      setState(() => _checking = false);
      return;
    }

    try {
      final data = await Api.instance.me();
      if (!mounted) return;
      setState(() {
        _user = data['user'] as Map<String, dynamic>;
        _checking = false;
      });
    } on ApiException {
      if (!mounted) return;
      setState(() => _checking = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_checking) {
      return const Scaffold(
        body: DecoratedBox(
          decoration: BoxDecoration(gradient: UnStyle.deepGradient),
          child: Center(
            child: CircularProgressIndicator(color: Colors.white),
          ),
        ),
      );
    }

    if (_user != null) {
      return HomeScreen(
        user: _user!,
        onSignedOut: () => setState(() => _user = null),
      );
    }

    return SignInScreen(
      onSignedIn: (user) => setState(() => _user = user),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sign in
// ─────────────────────────────────────────────────────────────────────────────

class SignInScreen extends StatefulWidget {
  const SignInScreen({super.key, required this.onSignedIn});

  final void Function(Map<String, dynamic>) onSignedIn;

  @override
  State<SignInScreen> createState() => _SignInScreenState();
}

class _SignInScreenState extends State<SignInScreen> {
  final _identifier = TextEditingController();
  final _password = TextEditingController();
  final _site = TextEditingController(text: Api.instance.baseUrl);

  bool _busy = false;
  bool _showPassword = false;
  String? _error;

  @override
  void dispose() {
    _identifier.dispose();
    _password.dispose();
    _site.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();

    final identifier = _identifier.text.trim();
    if (identifier.isEmpty || _password.text.isEmpty) {
      setState(() => _error = 'Enter your username or email and password.');
      return;
    }

    setState(() {
      _busy = true;
      _error = null;
    });

    try {
      if (_site.text.trim() != Api.instance.baseUrl) {
        await Api.instance.setBaseUrl(_site.text);
      }

      final data = await Api.instance.login(identifier, _password.text);
      if (!mounted) return;

      if (data['otp_required'] == true) {
        final user = await Navigator.of(context).push<Map<String, dynamic>>(
          MaterialPageRoute(
            builder: (_) => OtpScreen(
              identifier: identifier,
              password: _password.text,
              sentTo: (data['sent_to'] as String?) ?? 'your registered email',
            ),
          ),
        );

        if (user != null && mounted) {
          widget.onSignedIn(user);
        }
      } else {
        if (data['must_change_password'] == true && mounted) {
          await _showMustChangePassword();
          return;
        }
        widget.onSignedIn(data['user'] as Map<String, dynamic>);
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// The account is flagged to change its password. Nothing else will work
  /// until that is done, and it has to be done in a browser.
  Future<void> _showMustChangePassword() async {
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        icon: const Icon(Icons.lock_reset, size: 34, color: UnColors.amber),
        title: const Text('Change your password first'),
        content: const Text(
          'Your account is set to change its password at next sign-in. '
          'Set a new one in a browser, then come back and sign in here.',
          style: TextStyle(height: 1.4),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(dialogContext), child: const Text('Close')),
          FilledButton(
            onPressed: () async {
              Navigator.pop(dialogContext);
              final url = Uri.parse('${Api.instance.baseUrl}/accounts/password/change/');
              await launchUrl(url, mode: LaunchMode.externalApplication);
            },
            child: const Text('Open in browser'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).viewInsets.bottom;

    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: UnStyle.deepGradient),
        child: SafeArea(
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(24, 34, 24, 24 + bottom),
            child: AutofillGroup(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const _Brand(),
                  const SizedBox(height: 28),
                  Container(
                    padding: const EdgeInsets.fromLTRB(20, 22, 20, 16),
                    decoration: UnStyle.card(),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        const Text(
                          'Sign in to UN PASS',
                          style: TextStyle(
                            fontSize: 22,
                            fontWeight: FontWeight.w700,
                            color: UnColors.navy,
                          ),
                        ),
                        const SizedBox(height: 5),
                        const Text(
                          'Use your UN PASS username or registered email address.',
                          style: TextStyle(
                            color: UnColors.muted,
                            height: 1.4,
                          ),
                        ),
                        const SizedBox(height: 20),
                        TextField(
                          controller: _identifier,
                          autofillHints: const [
                            AutofillHints.username,
                            AutofillHints.email,
                          ],
                          keyboardType: TextInputType.emailAddress,
                          textInputAction: TextInputAction.next,
                          autocorrect: false,
                          enableSuggestions: false,
                          decoration: const InputDecoration(
                            labelText: 'Username or email',
                            hintText: 'name or name@example.org',
                            prefixIcon: Icon(Icons.alternate_email),
                          ),
                        ),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _password,
                          obscureText: !_showPassword,
                          autofillHints: const [AutofillHints.password],
                          textInputAction: TextInputAction.done,
                          onSubmitted: (_) => _busy ? null : _submit(),
                          decoration: InputDecoration(
                            labelText: 'Password',
                            prefixIcon: const Icon(Icons.lock_outline),
                            suffixIcon: IconButton(
                              icon: Icon(
                                _showPassword
                                    ? Icons.visibility_off
                                    : Icons.visibility,
                              ),
                              onPressed: () => setState(
                                () => _showPassword = !_showPassword,
                              ),
                              tooltip:
                                  _showPassword ? 'Hide password' : 'Show password',
                            ),
                          ),
                        ),
                        if (_error != null) ...[
                          const SizedBox(height: 14),
                          ErrorNote(_error!),
                        ],
                        const SizedBox(height: 18),
                        FilledButton.icon(
                          onPressed: _busy ? null : _submit,
                          icon: _busy
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                    color: Colors.white,
                                  ),
                                )
                              : const Icon(Icons.login),
                          label: Text(_busy ? 'Signing in…' : 'Sign in securely'),
                        ),
                        const SizedBox(height: 8),
                        ExpansionTile(
                          tilePadding: EdgeInsets.zero,
                          childrenPadding: EdgeInsets.zero,
                          shape: const Border(),
                          collapsedShape: const Border(),
                          leading: const Icon(
                            Icons.settings_outlined,
                            color: UnColors.muted,
                            size: 21,
                          ),
                          title: const Text(
                            'Connection settings',
                            style: TextStyle(
                              fontSize: 14,
                              color: UnColors.muted,
                            ),
                          ),
                          children: [
                            TextField(
                              controller: _site,
                              keyboardType: TextInputType.url,
                              autocorrect: false,
                              decoration: const InputDecoration(
                                labelText: 'UN PASS address',
                                hintText: 'https://unpass.gm',
                                prefixIcon: Icon(Icons.public),
                              ),
                            ),
                            const SizedBox(height: 8),
                            const Text(
                              'Only change this address if ICT instructs you to.',
                              style: TextStyle(
                                fontSize: 12,
                                color: UnColors.muted,
                              ),
                            ),
                            TextButton.icon(
                              onPressed: () async {
                                if (_site.text.trim() != Api.instance.baseUrl) {
                                  await Api.instance.setBaseUrl(_site.text);
                                }
                                if (context.mounted) await showConnectionCheck(context);
                              },
                              icon: const Icon(Icons.network_check, size: 18),
                              label: const Text('Check the connection'),
                            ),
                            const SizedBox(height: 4),
                          ],
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 18),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      const Icon(
                        Icons.verified_user_outlined,
                        size: 15,
                        color: Color(0xFF8FB3CC),
                      ),
                      const SizedBox(width: 6),
                      Flexible(
                        child: Text(
                          'Secure device: ${Api.instance.deviceName}',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Color(0xFF8FB3CC),
                          ),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _Brand extends StatelessWidget {
  const _Brand();

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Container(
          height: 68,
          width: 68,
          decoration: BoxDecoration(
            color: UnColors.blue,
            borderRadius: BorderRadius.circular(20),
            boxShadow: const [
              BoxShadow(
                color: Color(0x33009EDB),
                blurRadius: 24,
                offset: Offset(0, 8),
              ),
            ],
          ),
          child: const Icon(
            Icons.shield_outlined,
            color: Colors.white,
            size: 36,
          ),
        ),
        const SizedBox(height: 14),
        const Text(
          'UN PASS',
          style: TextStyle(
            fontSize: 30,
            fontWeight: FontWeight.w700,
            color: Colors.white,
            letterSpacing: 0.6,
          ),
        ),
        const SizedBox(height: 4),
        const Text(
          'Secure access • eSign • assets',
          style: TextStyle(
            color: Color(0xFFCFE6F5),
            fontSize: 15,
          ),
        ),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// OTP verification
// ─────────────────────────────────────────────────────────────────────────────

class OtpScreen extends StatefulWidget {
  const OtpScreen({
    super.key,
    required this.identifier,
    required this.password,
    required this.sentTo,
  });

  final String identifier;
  final String password;
  final String sentTo;

  @override
  State<OtpScreen> createState() => _OtpScreenState();
}

class _OtpScreenState extends State<OtpScreen> {
  final _code = TextEditingController();

  bool _busy = false;
  bool _resending = false;
  String? _error;

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  Future<void> _verify() async {
    final code = _code.text.replaceAll(RegExp(r'\D'), '');
    if (code.length != 6) {
      setState(() => _error = 'Enter the six-digit code from your email.');
      return;
    }

    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });

    try {
      final data = await Api.instance.verify(
        widget.identifier,
        widget.password,
        code,
      );

      if (mounted) {
        Navigator.of(context).pop(
          data['user'] as Map<String, dynamic>,
        );
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.message;
        _busy = false;
      });
    }
  }

  Future<void> _resend() async {
    setState(() => _resending = true);

    try {
      await Api.instance.resend(widget.identifier, widget.password);
      if (mounted) {
        showNote(context, 'A new verification code was sent.');
      }
    } on ApiException catch (e) {
      if (mounted) showNote(context, e.message, error: true);
    } finally {
      if (mounted) setState(() => _resending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: UnStyle.deepGradient),
        child: SafeArea(
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(
              24,
              16,
              24,
              24 + MediaQuery.of(context).viewInsets.bottom,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: IconButton(
                    icon: const Icon(Icons.arrow_back, color: Colors.white),
                    onPressed: () => Navigator.of(context).pop(),
                    tooltip: 'Back',
                  ),
                ),
                const SizedBox(height: 10),
                Container(
                  padding: const EdgeInsets.all(20),
                  decoration: UnStyle.card(),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Container(
                          height: 54,
                          width: 54,
                          decoration: BoxDecoration(
                            color: UnColors.lightBlue,
                            borderRadius: BorderRadius.circular(15),
                          ),
                          child: const Icon(
                            Icons.mark_email_read_outlined,
                            color: UnColors.darkBlue,
                            size: 29,
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                      const Text(
                        'Verify your sign-in',
                        style: TextStyle(
                          fontSize: 22,
                          fontWeight: FontWeight.w700,
                          color: UnColors.navy,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        'A six-digit verification code was sent to ${widget.sentTo}. '
                        'The code expires in 10 minutes.',
                        style: const TextStyle(
                          color: UnColors.muted,
                          height: 1.4,
                        ),
                      ),
                      const SizedBox(height: 20),
                      TextField(
                        controller: _code,
                        autofocus: true,
                        keyboardType: TextInputType.number,
                        textAlign: TextAlign.center,
                        maxLength: 6,
                        style: const TextStyle(
                          fontSize: 30,
                          fontWeight: FontWeight.w700,
                          letterSpacing: 12,
                          color: UnColors.navy,
                        ),
                        inputFormatters: [
                          FilteringTextInputFormatter.digitsOnly,
                        ],
                        autofillHints: const [AutofillHints.oneTimeCode],
                        decoration: const InputDecoration(
                          counterText: '',
                          hintText: '••••••',
                        ),
                        onChanged: (value) {
                          if (value.length == 6 && !_busy) _verify();
                        },
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: 8),
                        ErrorNote(_error!),
                      ],
                      const SizedBox(height: 14),
                      FilledButton.icon(
                        onPressed: _busy ? null : _verify,
                        icon: _busy
                            ? const SizedBox(
                                width: 18,
                                height: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                            : const Icon(Icons.verified_user_outlined),
                        label: Text(
                          _busy ? 'Verifying…' : 'Verify and sign in',
                        ),
                      ),
                      TextButton(
                        onPressed: _resending ? null : _resend,
                        child: Text(
                          _resending ? 'Sending…' : 'Resend code',
                        ),
                      ),
                      const Divider(height: 24),
                      const Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(
                            Icons.smartphone,
                            size: 16,
                            color: UnColors.muted,
                          ),
                          SizedBox(width: 8),
                          Expanded(
                            child: Text(
                              'After verification, this phone is remembered for 30 days. '
                              'Use “Sign out and forget this phone” when handing the device to someone else.',
                              style: TextStyle(
                                fontSize: 12,
                                color: UnColors.muted,
                                height: 1.35,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
