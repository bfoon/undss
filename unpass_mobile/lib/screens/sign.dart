import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'pdf_view.dart';

/// Draw a signature with a finger. Strokes are kept as points so the drawing
/// can be undone stroke by stroke and redrawn cleanly at export size.
class SignaturePad extends StatefulWidget {
  const SignaturePad({super.key, required this.controller, this.height = 200});
  final SignatureController controller;
  final double height;

  @override
  State<SignaturePad> createState() => _SignaturePadState();
}

class SignatureController extends ChangeNotifier {
  final List<List<Offset>> strokes = [];
  Size size = Size.zero;

  bool get isEmpty => strokes.every((s) => s.length < 2);

  void start(Offset p) {
    strokes.add([p]);
    notifyListeners();
  }

  void extend(Offset p) {
    if (strokes.isNotEmpty) {
      strokes.last.add(p);
      notifyListeners();
    }
  }

  void undo() {
    if (strokes.isNotEmpty) {
      strokes.removeLast();
      notifyListeners();
    }
  }

  void clear() {
    strokes.clear();
    notifyListeners();
  }

  /// The signature as a PNG data URL — what the server stamps onto the page.
  /// Drawn at three times the on-screen size so it stays crisp in the PDF.
  Future<String?> toDataUrl() async {
    if (isEmpty || size == Size.zero) return null;
    const scale = 3.0;
    final recorder = ui.PictureRecorder();
    final canvas = Canvas(recorder);
    canvas.scale(scale);
    final paint = Paint()
      ..color = const Color(0xFF0B2A45)
      ..strokeWidth = 2.6
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..style = PaintingStyle.stroke;
    for (final stroke in strokes) {
      for (var i = 0; i < stroke.length - 1; i++) {
        canvas.drawLine(stroke[i], stroke[i + 1], paint);
      }
    }
    final picture = recorder.endRecording();
    final image = await picture.toImage((size.width * scale).round(), (size.height * scale).round());
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    if (bytes == null) return null;
    return 'data:image/png;base64,${base64Encode(bytes.buffer.asUint8List())}';
  }
}

class _SignaturePadState extends State<SignaturePad> {
  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        widget.controller.size = Size(constraints.maxWidth, widget.height);
        return Container(
          height: widget.height,
          decoration: BoxDecoration(
            color: Colors.white,
            border: Border.all(color: UnColors.line),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Stack(
            children: [
              Positioned(
                left: 16, right: 16, bottom: 34,
                child: Container(height: 1, color: UnColors.line),
              ),
              Positioned(
                left: 16, bottom: 12,
                child: Text('Sign above the line',
                    style: TextStyle(fontSize: 11.5, color: UnColors.muted.withOpacity(0.9))),
              ),
              GestureDetector(
                onPanStart: (d) => widget.controller.start(d.localPosition),
                onPanUpdate: (d) => widget.controller.extend(d.localPosition),
                child: AnimatedBuilder(
                  animation: widget.controller,
                  builder: (_, __) => CustomPaint(
                    painter: _InkPainter(widget.controller.strokes),
                    size: Size(constraints.maxWidth, widget.height),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _InkPainter extends CustomPainter {
  _InkPainter(this.strokes);
  final List<List<Offset>> strokes;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = UnColors.navy
      ..strokeWidth = 2.6
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round
      ..style = PaintingStyle.stroke;
    for (final stroke in strokes) {
      for (var i = 0; i < stroke.length - 1; i++) {
        canvas.drawLine(stroke[i], stroke[i + 1], paint);
      }
    }
  }

  @override
  bool shouldRepaint(covariant _InkPainter old) => true;
}

/// Read the document, accept the consent, sign. The server stamps the drawing
/// into the fields placed for this person, exactly as the website does.
class SignScreen extends StatefulWidget {
  const SignScreen({super.key, required this.token, required this.subject});
  final String token;
  final String subject;

  @override
  State<SignScreen> createState() => _SignScreenState();
}

class _SignScreenState extends State<SignScreen> {
  late Future<Map<String, dynamic>> _future = Api.instance.signSheet(widget.token);
  final _pad = SignatureController();
  bool _consent = false;
  bool _save = true;
  bool _busy = false;

  @override
  void dispose() {
    _pad.dispose();
    super.dispose();
  }

  Future<void> _sign() async {
    if (!_consent) {
      showNote(context, 'Accept the electronic record consent first.', error: true);
      return;
    }
    final png = await _pad.toDataUrl();
    if (png == null) {
      showNote(context, 'Draw your signature in the box first.', error: true);
      return;
    }
    setState(() => _busy = true);
    try {
      final message = await Api.instance.sign(widget.token, signature: png, saveSignature: _save);
      if (!mounted) return;
      showNote(context, message);
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, e.message, error: true);
    }
  }

  Future<void> _decline() async {
    final controller = TextEditingController();
    final reason = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Decline to sign'),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLines: 3,
          decoration: const InputDecoration(labelText: 'Reason', hintText: 'The sender will see this'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(dialogContext), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: UnColors.red),
            onPressed: () => Navigator.pop(dialogContext, controller.text.trim()),
            child: const Text('Decline'),
          ),
        ],
      ),
    );
    if (reason == null || reason.isEmpty) return;
    setState(() => _busy = true);
    try {
      await Api.instance.decline(widget.token, reason);
      if (!mounted) return;
      showNote(context, 'Declined. The sender has been told.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, e.message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Sign')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) return const Loading();
          if (snap.hasError) {
            final e = snap.error;
            if (e is ApiException && e.status == 409) {
              return FailureState(
                  message: '${e.message}\n\nOpen it in the browser to continue.',
                  onRetry: () async {
                    final url = Uri.parse(Api.instance.webUrl('/accounts/esign/sign/${widget.token}/'));
                    await launchUrl(url, mode: LaunchMode.externalApplication);
                  });
            }
            return FailureState(
                message: '$e', onRetry: () => setState(() => _future = Api.instance.signSheet(widget.token)));
          }
          final data = snap.data!;
          if (data['already_done'] == true) {
            return const EmptyState(
              icon: Icons.check_circle_outline,
              title: 'Already done',
              detail: 'You have already responded to this envelope.',
            );
          }
          final env = data['envelope'] as Map<String, dynamic>;
          final docs = (data['documents'] as List<dynamic>? ?? []);
          final fields = (data['fields'] as List<dynamic>? ?? []);

          return ListView(
            padding: const EdgeInsets.all(UnStyle.gap),
            children: [
              Text('${env['subject']}',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 4),
              Text('${env['envelope_id']}',
                  style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
              if ('${env['message'] ?? ''}'.isNotEmpty) ...[
                const SizedBox(height: 14),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: UnStyle.card(color: UnColors.lightBlue, border: const Color(0x33009EDB)),
                  child: Text('${env['message']}', style: const TextStyle(height: 1.4)),
                ),
              ],
              const SizedBox(height: UnStyle.gap),
              SectionCard(
                title: 'READ IT FIRST',
                icon: Icons.picture_as_pdf_outlined,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final d in docs)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: OutlinedButton.icon(
                          onPressed: () => openPdf(context, '\${d['url']}',
                              title: '\${d['name']}', subtitle: '\${env['envelope_id']}'),
                          icon: const Icon(Icons.menu_book_outlined),
                          label: Text('\${d['name']}', overflow: TextOverflow.ellipsis),
                        ),
                      ),
                    Text('${fields.length} field${fields.length == 1 ? '' : 's'} are waiting for you. '
                        'Your signature is placed where the sender put it.',
                        style: const TextStyle(fontSize: 12.5, color: UnColors.muted)),
                  ],
                ),
              ),
              SectionCard(
                title: 'YOUR SIGNATURE',
                icon: Icons.draw_outlined,
                trailing: TextButton.icon(
                  onPressed: () => setState(() => _pad.clear()),
                  icon: const Icon(Icons.refresh, size: 16),
                  label: const Text('Clear'),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    SignaturePad(controller: _pad),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        TextButton.icon(
                          onPressed: () => setState(() => _pad.undo()),
                          icon: const Icon(Icons.undo, size: 18),
                          label: const Text('Undo stroke'),
                        ),
                      ],
                    ),
                    CheckboxListTile(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      controlAffinity: ListTileControlAffinity.leading,
                      value: _save,
                      onChanged: (v) => setState(() => _save = v ?? false),
                      title: const Text('Save this signature for next time'),
                    ),
                  ],
                ),
              ),
              Container(
                padding: const EdgeInsets.all(14),
                decoration: UnStyle.card(color: const Color(0xFFFFF9E8), border: const Color(0x33D97706)),
                child: CheckboxListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  controlAffinity: ListTileControlAffinity.leading,
                  value: _consent,
                  onChanged: (v) => setState(() => _consent = v ?? false),
                  title: const Text(
                    'I agree to sign electronically, and that my electronic signature is as binding as one on paper.',
                    style: TextStyle(fontSize: 13.5, height: 1.35),
                  ),
                ),
              ),
              const SizedBox(height: UnStyle.gap),
              if (_busy)
                const Center(child: Padding(padding: EdgeInsets.all(12), child: CircularProgressIndicator()))
              else ...[
                FilledButton.icon(
                  style: FilledButton.styleFrom(backgroundColor: UnColors.green),
                  onPressed: _sign,
                  icon: const Icon(Icons.check_circle_outline),
                  label: const Text('Sign the document'),
                ),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(foregroundColor: UnColors.red),
                  onPressed: _decline,
                  icon: const Icon(Icons.cancel_outlined),
                  label: const Text('Decline to sign'),
                ),
              ],
              const SizedBox(height: 24),
            ],
          );
        },
      ),
    );
  }
}
