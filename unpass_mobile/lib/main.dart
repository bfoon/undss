import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'core/api.dart';
import 'core/theme.dart';
import 'screens/home.dart';
import 'widgets/common.dart';

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

/// Decides where to start: straight into the app if the session is still good,
/// otherwise to sign-in.
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
      setState(() {
        _user = data['user'] as Map<String, dynamic>;
        _checking = false;
      });
    } on ApiException {
      setState(() => _checking = false); // the session lapsed; sign in again
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_checking) {
      return const Scaffold(
        body: DecoratedBox(
          decoration: BoxDecoration(gradient: UnStyle.deepGradient),
          child: Center(child: CircularProgressIndicator(color: Colors.white)),
        ),
      );
    }
    if (_user != null) {
      return HomeScreen(user: _user!, onSignedOut: () => setState(() => _user = null));
    }
    return SignInScreen(onSignedIn: (u) => setState(() => _user = u));
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Signing in
// ─────────────────────────────────────────────────────────────────────────────

class SignInScreen extends StatefulWidget {
  const SignInScreen({super.key, required this.onSignedIn});
  final void Function(Map<String, dynamic>) onSignedIn;

  @override
  State<SignInScreen> createState() => _SignInScreenState();
}

class _SignInScreenState extends State<SignInScreen> {
  final _user = TextEditingController();
  final _pass = TextEditingController();
  final _site = TextEditingController(text: Api.instance.baseUrl);
  bool _busy = false;
  bool _showPassword = false;
  String? _error;

  @override
  void dispose() {
    _user.dispose();
    _pass.dispose();
    _site.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (_site.text.trim() != Api.instance.baseUrl) {
        await Api.instance.setBaseUrl(_site.text);
      }
      final data = await Api.instance.login(_user.text.trim(), _pass.text);
      if (!mounted) return;
      if (data['otp_required'] == true) {
        final user = await Navigator.of(context).push<Map<String, dynamic>>(
          MaterialPageRoute(
            builder: (_) => OtpScreen(
              username: _user.text.trim(),
              password: _pass.text,
              sentTo: (data['sent_to'] as String?) ?? 'your email',
            ),
          ),
        );
        if (user != null) widget.onSignedIn(user);
      } else {
        widget.onSignedIn(data['user'] as Map<String, dynamic>);
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).viewInsets.bottom;
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(gradient: UnStyle.deepGradient),
        child: SafeArea(
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(24, 40, 24, 24 + bottom),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const _Brand(),
                const SizedBox(height: 34),
                Container(
                  padding: const EdgeInsets.all(20),
                  decoration: UnStyle.card(),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Text('Sign in',
                          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: UnColors.navy)),
                      const SizedBox(height: 4),
                      const Text('Use the same details as the website.',
                          style: TextStyle(color: UnColors.muted)),
                      const SizedBox(height: 18),
                      TextField(
                        controller: _user,
                        autofillHints: const [AutofillHints.username],
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(
                            labelText: 'Username', prefixIcon: Icon(Icons.person_outline)),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: _pass,
                        obscureText: !_showPassword,
                        autofillHints: const [AutofillHints.password],
                        onSubmitted: (_) => _busy ? null : _submit(),
                        decoration: InputDecoration(
                          labelText: 'Password',
                          prefixIcon: const Icon(Icons.lock_outline),
                          suffixIcon: IconButton(
                            icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
                            onPressed: () => setState(() => _showPassword = !_showPassword),
                            tooltip: _showPassword ? 'Hide password' : 'Show password',
                          ),
                        ),
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: 14),
                        ErrorNote(_error!),
                      ],
                      const SizedBox(height: 18),
                      FilledButton(
                        onPressed: _busy ? null : _submit,
                        child: _busy
                            ? const SizedBox(
                                height: 20, width: 20,
                                child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                            : const Text('Continue'),
                      ),
                      const SizedBox(height: 8),
                      ExpansionTile(
                        tilePadding: EdgeInsets.zero,
                        shape: const Border(),
                        collapsedShape: const Border(),
                        title: const Text('Site address',
                            style: TextStyle(fontSize: 14, color: UnColors.muted)),
                        children: [
                          TextField(
                            controller: _site,
                            keyboardType: TextInputType.url,
                            decoration: const InputDecoration(
                              hintText: 'https://unpass.gm',
                              prefixIcon: Icon(Icons.public),
                            ),
                          ),
                          const SizedBox(height: 8),
                          const Text('Only change this if ICT tells you to.',
                              style: TextStyle(fontSize: 12, color: UnColors.muted)),
                          const SizedBox(height: 8),
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 18),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(Icons.verified_user_outlined, size: 15, color: Color(0xFF8FB3CC)),
                    const SizedBox(width: 6),
                    Flexible(
                      child: Text(
                        'This phone is recognised as ${Api.instance.deviceName}',
                        style: const TextStyle(fontSize: 12, color: Color(0xFF8FB3CC)),
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
          height: 64,
          width: 64,
          decoration: BoxDecoration(
            color: UnColors.blue,
            borderRadius: BorderRadius.circular(18),
            boxShadow: const [BoxShadow(color: Color(0x33009EDB), blurRadius: 22, offset: Offset(0, 8))],
          ),
          child: const Icon(Icons.shield_outlined, color: Colors.white, size: 34),
        ),
        const SizedBox(height: 14),
        const Text('UN PASS',
            style: TextStyle(fontSize: 30, fontWeight: FontWeight.w700, color: Colors.white, letterSpacing: 0.5)),
        const SizedBox(height: 4),
        const Text('eSign approvals and asset checks',
            style: TextStyle(color: Color(0xFFCFE6F5), fontSize: 15)),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// The one-time code
// ─────────────────────────────────────────────────────────────────────────────

class OtpScreen extends StatefulWidget {
  const OtpScreen({super.key, required this.username, required this.password, required this.sentTo});
  final String username;
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
      setState(() => _error = 'Enter the six digits from the email.');
      return;
    }
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final data = await Api.instance.verify(widget.username, widget.password, code);
      if (mounted) Navigator.of(context).pop(data['user'] as Map<String, dynamic>);
    } on ApiException catch (e) {
      setState(() {
        _error = e.message;
        _busy = false;
      });
    }
  }

  Future<void> _resend() async {
    setState(() => _resending = true);
    try {
      await Api.instance.resend(widget.username, widget.password);
      if (mounted) showNote(context, 'A new code is on its way.');
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
            padding: EdgeInsets.fromLTRB(24, 16, 24, 24 + MediaQuery.of(context).viewInsets.bottom),
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
                      Container(
                        height: 52, width: 52,
                        decoration: BoxDecoration(
                            color: UnColors.lightBlue, borderRadius: BorderRadius.circular(14)),
                        child: const Icon(Icons.mark_email_read_outlined, color: UnColors.darkBlue, size: 28),
                      ),
                      const SizedBox(height: 16),
                      const Text('Check your email',
                          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: UnColors.navy)),
                      const SizedBox(height: 6),
                      Text('We sent a six-digit code to ${widget.sentTo}. It lasts ten minutes.',
                          style: const TextStyle(color: UnColors.muted)),
                      const SizedBox(height: 18),
                      TextField(
                        controller: _code,
                        autofocus: true,
                        keyboardType: TextInputType.number,
                        textAlign: TextAlign.center,
                        maxLength: 6,
                        style: const TextStyle(
                            fontSize: 30, fontWeight: FontWeight.w700, letterSpacing: 12, color: UnColors.navy),
                        inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                        autofillHints: const [AutofillHints.oneTimeCode],
                        decoration: const InputDecoration(counterText: '', hintText: '••••••'),
                        onChanged: (v) {
                          if (v.length == 6 && !_busy) _verify();
                        },
                      ),
                      if (_error != null) ...[const SizedBox(height: 6), ErrorNote(_error!)],
                      const SizedBox(height: 14),
                      FilledButton(
                        onPressed: _busy ? null : _verify,
                        child: _busy
                            ? const SizedBox(
                                height: 20, width: 20,
                                child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                            : const Text('Verify and sign in'),
                      ),
                      TextButton(
                        onPressed: _resending ? null : _resend,
                        child: Text(_resending ? 'Sending…' : 'Send another code'),
                      ),
                      const Divider(height: 24),
                      Row(
                        children: [
                          const Icon(Icons.smartphone, size: 16, color: UnColors.muted),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Text(
                              'Once verified, this phone is remembered for 30 days and you won\'t need a code again.',
                              style: const TextStyle(fontSize: 12, color: UnColors.muted),
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
