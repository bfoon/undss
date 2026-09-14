import 'dart:convert';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'pdf_view.dart';

/// Draw a signature with a finger.
class SignaturePad extends StatefulWidget {
  const SignaturePad({
    super.key,
    required this.controller,
    this.height = 200,
  });

  final SignatureController controller;
  final double height;

  @override
  State<SignaturePad> createState() => _SignaturePadState();
}

class SignatureController extends ChangeNotifier {
  final List<List<Offset>> strokes = <List<Offset>>[];
  Size size = Size.zero;

  bool get isEmpty => strokes.every((stroke) => stroke.length < 2);

  void start(Offset point) {
    strokes.add(<Offset>[point]);
    notifyListeners();
  }

  void extend(Offset point) {
    if (strokes.isEmpty) return;
    strokes.last.add(point);
    notifyListeners();
  }

  void undo() {
    if (strokes.isEmpty) return;
    strokes.removeLast();
    notifyListeners();
  }

  void clear() {
    strokes.clear();
    notifyListeners();
  }

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
    final image = await picture.toImage(
      (size.width * scale).round(),
      (size.height * scale).round(),
    );
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
                left: 16,
                right: 16,
                bottom: 34,
                child: Container(height: 1, color: UnColors.line),
              ),
              Positioned(
                left: 16,
                bottom: 12,
                child: Text(
                  'Sign above the line',
                  style: TextStyle(
                    fontSize: 11.5,
                    color: UnColors.muted.withOpacity(0.9),
                  ),
                ),
              ),
              GestureDetector(
                behavior: HitTestBehavior.opaque,
                onPanStart: (details) =>
                    widget.controller.start(details.localPosition),
                onPanUpdate: (details) =>
                    widget.controller.extend(details.localPosition),
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
  bool shouldRepaint(covariant _InkPainter oldDelegate) => true;
}

/// Mobile signing screen.
///
/// In addition to signature/initial fields, this version renders the sender's
/// checkbox, text, full-name, email, title and date fields so they can actually
/// be completed on a phone. The resulting values are sent through the existing
/// `fields` payload that the Django mobile signing endpoint already supports.
class SignScreen extends StatefulWidget {
  const SignScreen({
    super.key,
    required this.token,
    required this.subject,
  });

  final String token;
  final String subject;

  @override
  State<SignScreen> createState() => _SignScreenState();
}

class _SignScreenState extends State<SignScreen> {
  late Future<Map<String, dynamic>> _future =
      Api.instance.signSheet(widget.token);

  final SignatureController _pad = SignatureController();
  final Map<String, TextEditingController> _fieldControllers =
      <String, TextEditingController>{};
  final Map<String, bool> _checkboxValues = <String, bool>{};

  bool _consent = false;
  bool _save = true;
  bool _busy = false;
  bool _seeded = false;
  Map<String, dynamic>? _chosen;
  bool _drawInstead = false;

  static const Set<String> _signatureKinds = <String>{
    'signature',
    'initials',
  };

  @override
  void dispose() {
    _pad.dispose();
    for (final controller in _fieldControllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  String _fieldId(Map<String, dynamic> field) => '${field['id']}';

  String _displayLabel(Map<String, dynamic> field) {
    final explicit = '${field['label'] ?? ''}'.trim();
    if (explicit.isNotEmpty) return explicit;

    switch ('${field['kind']}') {
      case 'full_name':
        return 'Full name';
      case 'date_signed':
        return 'Date signed';
      case 'job_title':
        return 'Job title';
      case 'email':
        return 'Email';
      case 'text':
        return 'Text';
      case 'checkbox':
        return 'Checkbox';
      case 'initials':
        return 'Initials';
      default:
        return 'Signature';
    }
  }

  String _todayForSignature() {
    const months = <String>[
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
    ];
    final now = DateTime.now();
    return '${now.day.toString().padLeft(2, '0')} '
        '${months[now.month - 1]} ${now.year}';
  }

  Map<String, dynamic>? _myRecipient(Map<String, dynamic> envelope) {
    final recipients = envelope['recipients'] as List<dynamic>? ?? const [];
    for (final raw in recipients) {
      if (raw is Map && raw['is_me'] == true) {
        return Map<String, dynamic>.from(raw);
      }
    }
    return null;
  }

  void _seedFields(
    List<dynamic> rawFields,
    Map<String, dynamic> envelope,
  ) {
    if (_seeded) return;
    _seeded = true;

    final me = _myRecipient(envelope);

    for (final raw in rawFields) {
      if (raw is! Map) continue;
      final field = Map<String, dynamic>.from(raw);
      final id = _fieldId(field);
      final kind = '${field['kind']}';

      if (kind == 'checkbox') {
        _checkboxValues.putIfAbsent(id, () => false);
        continue;
      }

      if (_signatureKinds.contains(kind)) continue;

      var initial = '';
      switch (kind) {
        case 'full_name':
          initial = '${me?['name'] ?? ''}'.trim();
          break;
        case 'email':
          initial = '${me?['email'] ?? ''}'.trim();
          break;
        case 'date_signed':
          initial = _todayForSignature();
          break;
      }

      _fieldControllers.putIfAbsent(
        id,
        () => TextEditingController(text: initial),
      );
    }
  }

  Map<String, dynamic> _fieldPayload(List<dynamic> rawFields) {
    final payload = <String, dynamic>{};

    for (final raw in rawFields) {
      if (raw is! Map) continue;
      final field = Map<String, dynamic>.from(raw);
      final id = _fieldId(field);
      final kind = '${field['kind']}';

      if (_signatureKinds.contains(kind)) continue;

      if (kind == 'checkbox') {
        payload[id] = _checkboxValues[id] == true ? '1' : '0';
        continue;
      }

      final value = _fieldControllers[id]?.text.trim() ?? '';
      // Do not send an empty auto-fill field. The server already knows how to
      // fill full name, email, date and job title from the recipient account.
      if (value.isNotEmpty || kind == 'text') {
        payload[id] = value;
      }
    }

    return payload;
  }

  String? _validateFields(List<dynamic> rawFields) {
    for (final raw in rawFields) {
      if (raw is! Map) continue;
      final field = Map<String, dynamic>.from(raw);
      if (field['required'] != true) continue;

      final id = _fieldId(field);
      final kind = '${field['kind']}';
      final label = _displayLabel(field);

      if (kind == 'checkbox' && _checkboxValues[id] != true) {
        return 'Please tick "$label".';
      }

      if (kind == 'text' &&
          (_fieldControllers[id]?.text.trim().isEmpty ?? true)) {
        return 'Please complete "$label".';
      }

      // full_name/email/date/job_title can still be filled by the backend if
      // the phone leaves them blank, so do not reject those here.
    }

    return null;
  }

  Future<void> _sign(List<dynamic> fields) async {
    if (!_consent) {
      showNote(
        context,
        'Accept the electronic record consent first.',
        error: true,
      );
      return;
    }

    final fieldError = _validateFields(fields);
    if (fieldError != null) {
      showNote(context, fieldError, error: true);
      return;
    }

    String? signature;
    var saveIt = false;

    if (!_drawInstead && _chosen != null) {
      signature = '${_chosen!['ref']}';
    } else {
      signature = await _pad.toDataUrl();
      saveIt = _save;
      if (signature == null) {
        if (!mounted) return;
        showNote(
          context,
          'Draw your signature in the box first.',
          error: true,
        );
        return;
      }
    }

    setState(() => _busy = true);
    try {
      final message = await Api.instance.sign(
        widget.token,
        signature: signature,
        saveSignature: saveIt,
        fields: _fieldPayload(fields),
      );

      if (!mounted) return;
      showNote(context, message);
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, e.message, error: true);
    } catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, 'The document could not be signed. $e', error: true);
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
          decoration: const InputDecoration(
            labelText: 'Reason',
            hintText: 'The sender will see this',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: UnColors.red),
            onPressed: () =>
                Navigator.pop(dialogContext, controller.text.trim()),
            child: const Text('Decline'),
          ),
        ],
      ),
    );
    controller.dispose();

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

  Widget _fieldEditor(Map<String, dynamic> field) {
    final id = _fieldId(field);
    final kind = '${field['kind']}';
    final label = _displayLabel(field);
    final required = field['required'] == true;

    if (kind == 'checkbox') {
      return CheckboxListTile(
        value: _checkboxValues[id] ?? false,
        onChanged: _busy
            ? null
            : (value) => setState(
                  () => _checkboxValues[id] = value ?? false,
                ),
        contentPadding: EdgeInsets.zero,
        controlAffinity: ListTileControlAffinity.leading,
        title: Text('$label${required ? ' *' : ''}'),
        subtitle: Text(
          'Document ${field['document_id']} · page ${field['page']}',
          style: const TextStyle(fontSize: 11.5, color: UnColors.muted),
        ),
      );
    }

    final controller = _fieldControllers.putIfAbsent(
      id,
      () => TextEditingController(),
    );

    final isDate = kind == 'date_signed';
    final keyboard = kind == 'email'
        ? TextInputType.emailAddress
        : kind == 'text'
            ? TextInputType.multiline
            : TextInputType.text;

    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: controller,
        enabled: !_busy,
        keyboardType: keyboard,
        minLines: kind == 'text' ? 1 : null,
        maxLines: kind == 'text' ? 3 : 1,
        decoration: InputDecoration(
          labelText: '$label${required ? ' *' : ''}',
          helperText:
              'Document ${field['document_id']} · page ${field['page']}'
              '${isDate ? ' · you can edit the date if needed' : ''}',
          border: const OutlineInputBorder(),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Sign')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading();
          }

          if (snap.hasError) {
            final error = snap.error;
            if (error is ApiException && error.status == 409) {
              return FailureState(
                message:
                    '${error.message}\n\nOpen it in the browser to continue.',
                onRetry: () async {
                  final url = Uri.parse(
                    Api.instance.webUrl(
                      '/accounts/esign/s/${widget.token}/',
                    ),
                  );
                  await launchUrl(
                    url,
                    mode: LaunchMode.externalApplication,
                  );
                },
              );
            }

            return FailureState(
              message: '$error',
              onRetry: () => setState(
                () => _future = Api.instance.signSheet(widget.token),
              ),
            );
          }

          final data = snap.data!;
          if (data['already_done'] == true) {
            return const EmptyState(
              icon: Icons.check_circle_outline,
              title: 'Already done',
              detail: 'You have already responded to this envelope.',
            );
          }

          final envelope =
              Map<String, dynamic>.from(data['envelope'] as Map);
          final documents = data['documents'] as List<dynamic>? ?? const [];
          final fields = data['fields'] as List<dynamic>? ?? const [];
          final editableFields = fields
              .where((raw) =>
                  raw is Map &&
                  !_signatureKinds.contains('${(raw as Map)['kind']}'))
              .toList();

          _seedFields(fields, envelope);

          return ListView(
            padding: const EdgeInsets.all(UnStyle.gap),
            children: [
              Text(
                '${envelope['subject']}',
                style: const TextStyle(
                  fontSize: 20,
                  fontWeight: FontWeight.w700,
                  color: UnColors.navy,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                '${envelope['envelope_id']}',
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 12,
                  color: UnColors.muted,
                ),
              ),
              if ('${envelope['message'] ?? ''}'.isNotEmpty) ...[
                const SizedBox(height: 14),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: UnStyle.card(
                    color: UnColors.lightBlue,
                    border: const Color(0x33009EDB),
                  ),
                  child: Text(
                    '${envelope['message']}',
                    style: const TextStyle(height: 1.4),
                  ),
                ),
              ],
              const SizedBox(height: UnStyle.gap),
              SectionCard(
                title: 'READ IT FIRST',
                icon: Icons.picture_as_pdf_outlined,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    for (final raw in documents)
                      if (raw is Map)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: OutlinedButton.icon(
                            onPressed: () => openPdf(
                              context,
                              '${raw['url']}',
                              title: '${raw['name']}',
                              subtitle: '${envelope['envelope_id']}',
                            ),
                            icon: const Icon(Icons.menu_book_outlined),
                            label: Text(
                              '${raw['name']}',
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                        ),
                    Text(
                      '${fields.length} field'
                      '${fields.length == 1 ? '' : 's'} '
                      'are waiting for you.',
                      style: const TextStyle(
                        fontSize: 12.5,
                        color: UnColors.muted,
                      ),
                    ),
                  ],
                ),
              ),
              if (editableFields.isNotEmpty) ...[
                const SizedBox(height: UnStyle.gap),
                SectionCard(
                  title: 'COMPLETE DOCUMENT FIELDS',
                  icon: Icons.fact_check_outlined,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Text(
                        'These are the fields the sender placed for you. '
                        'They will be stamped into the exact positions on the PDF.',
                        style: TextStyle(
                          fontSize: 12.5,
                          height: 1.35,
                          color: UnColors.muted,
                        ),
                      ),
                      const SizedBox(height: 14),
                      for (final raw in editableFields)
                        _fieldEditor(
                          Map<String, dynamic>.from(raw as Map),
                        ),
                    ],
                  ),
                ),
              ],
              const SizedBox(height: UnStyle.gap),
              _signatureSection(data),
              const SizedBox(height: UnStyle.gap),
              Container(
                padding: const EdgeInsets.all(14),
                decoration: UnStyle.card(
                  color: const Color(0xFFFFF9E8),
                  border: const Color(0x33D97706),
                ),
                child: CheckboxListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  controlAffinity: ListTileControlAffinity.leading,
                  value: _consent,
                  onChanged: _busy
                      ? null
                      : (value) =>
                          setState(() => _consent = value ?? false),
                  title: const Text(
                    'I agree to sign electronically, and that my electronic '
                    'signature is as binding as one on paper.',
                    style: TextStyle(fontSize: 13.5, height: 1.35),
                  ),
                ),
              ),
              const SizedBox(height: UnStyle.gap),
              if (_busy)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.all(12),
                    child: CircularProgressIndicator(),
                  ),
                )
              else ...[
                FilledButton.icon(
                  style: FilledButton.styleFrom(
                    backgroundColor: UnColors.green,
                  ),
                  onPressed: () => _sign(fields),
                  icon: const Icon(Icons.check_circle_outline),
                  label: const Text('Sign the document'),
                ),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                    foregroundColor: UnColors.red,
                  ),
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

  Widget _signatureSection(Map<String, dynamic> data) {
    final saved = data['saved_signatures'] as List<dynamic>? ?? const [];

    if (saved.isNotEmpty && _chosen == null && !_drawInstead) {
      final first = saved.first;
      if (first is Map) {
        _chosen = Map<String, dynamic>.from(first);
      }
    }

    final useSaved = saved.isNotEmpty && !_drawInstead;

    return SectionCard(
      title: 'YOUR SIGNATURE',
      icon: Icons.draw_outlined,
      trailing: useSaved
          ? null
          : TextButton.icon(
              onPressed: () => setState(_pad.clear),
              icon: const Icon(Icons.refresh, size: 16),
              label: const Text('Clear'),
            ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (useSaved) ...[
            for (final raw in saved)
              if (raw is Map)
                Builder(
                  builder: (context) {
                    final signature =
                        Map<String, dynamic>.from(raw);
                    final selected = _chosen != null &&
                        _chosen!['id'] == signature['id'];

                    return InkWell(
                      onTap: () => setState(() => _chosen = signature),
                      borderRadius: BorderRadius.circular(10),
                      child: Container(
                        margin: const EdgeInsets.only(bottom: 8),
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: selected
                              ? UnColors.lightBlue
                              : Colors.white,
                          border: Border.all(
                            color: selected
                                ? UnColors.blue
                                : UnColors.line,
                            width: selected ? 2 : 1,
                          ),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Row(
                          children: [
                            Icon(
                              selected
                                  ? Icons.radio_button_checked
                                  : Icons.radio_button_off,
                              size: 20,
                              color: selected
                                  ? UnColors.blue
                                  : UnColors.muted,
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  if ('${signature['image_url'] ?? ''}'.isNotEmpty)
                                    SizedBox(
                                      height: 46,
                                      child: Align(
                                        alignment: Alignment.centerLeft,
                                        child: Image.network(
                                          Api.instance.webUrl('${signature['image_url']}'),
                                          height: 46,
                                          fit: BoxFit.contain,
                                          errorBuilder: (_, __, ___) => const SizedBox.shrink(),
                                        ),
                                      ),
                                    ),
                                  Row(
                                    children: [
                                      Expanded(
                                        child: Text(
                                          '${signature['label'] ?? 'Saved signature'}',
                                          style: const TextStyle(
                                            fontWeight: FontWeight.w600,
                                          ),
                                        ),
                                      ),
                                      if (signature['is_default'] == true)
                                        const StatusPill('Default', color: UnColors.blue),
                                    ],
                                  ),
                                ],
                              ),
                            ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
            TextButton.icon(
              onPressed: () => setState(() => _drawInstead = true),
              icon: const Icon(Icons.gesture, size: 18),
              label: const Text('Draw a different one this time'),
            ),
          ] else ...[
            SignaturePad(controller: _pad),
            const SizedBox(height: 8),
            Row(
              children: [
                TextButton.icon(
                  onPressed: () => setState(_pad.undo),
                  icon: const Icon(Icons.undo, size: 18),
                  label: const Text('Undo stroke'),
                ),
                if (saved.isNotEmpty)
                  TextButton.icon(
                    onPressed: () => setState(() {
                      _drawInstead = false;
                      _pad.clear();
                    }),
                    icon: const Icon(Icons.bookmark_outline, size: 18),
                    label: const Text('Use saved'),
                  ),
              ],
            ),
            CheckboxListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              controlAffinity: ListTileControlAffinity.leading,
              value: _save,
              onChanged: (value) =>
                  setState(() => _save = value ?? false),
              title: const Text('Save this signature for next time'),
            ),
          ],
        ],
      ),
    );
  }
}
