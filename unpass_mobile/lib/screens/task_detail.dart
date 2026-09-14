import 'package:flutter/material.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'pdf_view.dart';

/// One step of a workflow: what is being asked, the answers so far, and the
/// decision buttons. Deciding here is exactly what the website does.
class TaskDetailScreen extends StatefulWidget {
  const TaskDetailScreen({super.key, required this.token});

  final String token;

  @override
  State<TaskDetailScreen> createState() => _TaskDetailScreenState();
}

class _TaskDetailScreenState extends State<TaskDetailScreen> {
  late Future<Map<String, dynamic>> _future = Api.instance.task(widget.token);
  bool _busy = false;
  bool _changed = false;

  void _reload() => setState(() => _future = Api.instance.task(widget.token));

  Future<void> _decide(String action, {required String verb}) async {
    var comment = '';
    if (action == 'reject' || action == 'return') {
      comment = await _askForReason(verb) ?? '';
      if (comment.trim().isEmpty) return;
    } else {
      final sure = await _confirm(verb);
      if (sure != true) return;
      comment = _optionalComment;
    }

    setState(() => _busy = true);

    try {
      final message = await Api.instance.decide(
        widget.token,
        action,
        comment: comment,
      );
      _changed = true;

      if (!mounted) return;
      showNote(context, message);
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      showNote(context, e.message, error: true);

      if (e.stale) {
        _reload();
      }
    }
  }

  String _optionalComment = '';

  Future<bool?> _confirm(String verb) {
    final controller = TextEditingController();

    return showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text('$verb this request?'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: controller,
              maxLines: 3,
              decoration: const InputDecoration(
                labelText: 'Comment (optional)',
                hintText: 'Anything the others should know',
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              _optionalComment = controller.text.trim();
              Navigator.pop(dialogContext, true);
            },
            child: Text(verb),
          ),
        ],
      ),
    );
  }

  Future<String?> _askForReason(String verb) {
    final controller = TextEditingController();

    return showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text('$verb — why?'),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLines: 4,
          decoration: const InputDecoration(
            labelText: 'Reason',
            hintText: 'This is shown to the person who sent it',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: UnColors.red),
            onPressed: () => Navigator.pop(dialogContext, controller.text),
            child: Text(verb),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: true,
      onPopInvokedWithResult: (didPop, result) {},
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Your decision'),
          leading: IconButton(
            icon: const Icon(Icons.arrow_back),
            onPressed: () => Navigator.of(context).pop(_changed),
          ),
        ),
        body: FutureBuilder<Map<String, dynamic>>(
          future: _future,
          builder: (context, snap) {
            if (snap.connectionState == ConnectionState.waiting) {
              return const Loading();
            }

            if (snap.hasError) {
              return FailureState(
                message: '${snap.error}',
                onRetry: _reload,
              );
            }

            final t = snap.data!;
            final answers = t['answers'] as List<dynamic>? ?? [];
            final canDecide =
                t['can_decide'] == true && t['status'] == 'pending';

            return Stack(
              children: [
                ListView(
                  padding: const EdgeInsets.fromLTRB(
                    UnStyle.gap,
                    UnStyle.gap,
                    UnStyle.gap,
                    140,
                  ),
                  children: [
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(16),
                      decoration: BoxDecoration(
                        gradient: const LinearGradient(
                          colors: [
                            UnColors.lightBlue,
                            Color(0xFFF3F9FE),
                          ],
                          begin: Alignment.topLeft,
                          end: Alignment.bottomRight,
                        ),
                        borderRadius: BorderRadius.circular(UnStyle.radius),
                        border: Border.all(
                          color: const Color(0x33009EDB),
                        ),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${t['step']}'.toUpperCase(),
                            style: const TextStyle(
                              fontSize: 11.5,
                              fontWeight: FontWeight.w800,
                              color: UnColors.darkBlue,
                              letterSpacing: 0.8,
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            '${t['title']}',
                            style: const TextStyle(
                              fontSize: 20,
                              fontWeight: FontWeight.w700,
                              color: UnColors.navy,
                            ),
                          ),
                          const SizedBox(height: 8),
                          Wrap(
                            spacing: 10,
                            runSpacing: 6,
                            children: [
                              Text(
                                '${t['reference']}',
                                style: const TextStyle(
                                  fontFamily: 'monospace',
                                  fontSize: 12,
                                  color: UnColors.muted,
                                ),
                              ),
                              if ('${t['started_by']}'.isNotEmpty)
                                Text(
                                  'started by ${t['started_by']}',
                                  style: const TextStyle(
                                    fontSize: 12,
                                    color: UnColors.muted,
                                  ),
                                ),
                              if (t['due'] != null)
                                StatusPill(
                                  'Due ${t['due']}',
                                  color: UnColors.amber,
                                ),
                            ],
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: UnStyle.gap),
                    if ('${t['instructions']}'.isNotEmpty)
                      SectionCard(
                        title: 'WHAT YOU ARE ASKED TO DO',
                        icon: Icons.info_outline,
                        child: Text(
                          '${t['instructions']}',
                          style: const TextStyle(height: 1.4),
                        ),
                      ),
                    if ('${t['message']}'.isNotEmpty)
                      SectionCard(
                        title: 'MESSAGE FROM THE SENDER',
                        icon: Icons.chat_bubble_outline,
                        child: Text(
                          '${t['message']}',
                          style: const TextStyle(height: 1.4),
                        ),
                      ),
                    if (answers.isNotEmpty)
                      SectionCard(
                        title: 'THE REQUEST',
                        icon: Icons.list_alt,
                        child: Column(
                          children: [
                            for (final a in answers)
                              DetailRow(
                                '${(a as Map)['label']}',
                                '${a['value']}',
                              ),
                          ],
                        ),
                      ),
                    SectionCard(
                      title: 'THE DOCUMENT',
                      icon: Icons.picture_as_pdf_outlined,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          const Text(
                            'Open it in your browser to read every page before deciding.',
                            style: TextStyle(color: UnColors.muted),
                          ),
                          const SizedBox(height: 12),
                          OutlinedButton.icon(
                            onPressed: () => openPdf(context, '\${t['document_url']}',
                                title: 'Document', subtitle: '\${t['reference']}'),
                            icon: const Icon(Icons.menu_book_outlined),
                            label: const Text('Read the document'),
                          ),
                        ],
                      ),
                    ),
                    if (!canDecide)
                      Container(
                        padding: const EdgeInsets.all(14),
                        decoration: BoxDecoration(
                          color: const Color(0xFFFFF4E0),
                          borderRadius: BorderRadius.circular(UnStyle.radius),
                        ),
                        child: Row(
                          children: [
                            const Icon(
                              Icons.info_outline,
                              color: UnColors.amber,
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Text(
                                t['status'] == 'pending'
                                    ? 'This step is filled in or signed on the website. Tap “Open the PDF” to continue there.'
                                    : 'This step has already been dealt with.',
                                style: const TextStyle(
                                  color: Color(0xFF92400E),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                  ],
                ),
                if (canDecide)
                  Positioned(
                    left: 0,
                    right: 0,
                    bottom: 0,
                    child: Container(
                      padding: EdgeInsets.fromLTRB(
                        UnStyle.gap,
                        12,
                        UnStyle.gap,
                        12 + MediaQuery.of(context).padding.bottom,
                      ),
                      decoration: const BoxDecoration(
                        color: Colors.white,
                        border: Border(
                          top: BorderSide(color: UnColors.line),
                        ),
                        boxShadow: [
                          BoxShadow(
                            color: Color(0x14000000),
                            blurRadius: 12,
                            offset: Offset(0, -3),
                          ),
                        ],
                      ),
                      child: _busy
                          ? const Padding(
                              padding: EdgeInsets.symmetric(vertical: 14),
                              child: Center(
                                child: CircularProgressIndicator(),
                              ),
                            )
                          : Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                FilledButton.icon(
                                  style: FilledButton.styleFrom(
                                    backgroundColor: UnColors.green,
                                  ),
                                  onPressed: () => _decide(
                                    t['kind'] == 'review'
                                        ? 'acknowledge'
                                        : 'approve',
                                    verb: t['kind'] == 'review'
                                        ? 'Acknowledge'
                                        : 'Approve',
                                  ),
                                  icon: Icon(
                                    t['kind'] == 'review'
                                        ? Icons.done_all
                                        : Icons.check_circle_outline,
                                  ),
                                  label: Text(
                                    t['kind'] == 'review'
                                        ? "I've reviewed it"
                                        : 'Approve',
                                  ),
                                ),
                                const SizedBox(height: 8),
                                Row(
                                  children: [
                                    if (t['can_return'] == true) ...[
                                      Expanded(
                                        child: OutlinedButton.icon(
                                          style: OutlinedButton.styleFrom(
                                            foregroundColor: UnColors.amber,
                                          ),
                                          onPressed: () => _decide(
                                            'return',
                                            verb: 'Return for changes',
                                          ),
                                          icon: const Icon(Icons.reply),
                                          label: const Text('Return'),
                                        ),
                                      ),
                                      const SizedBox(width: 8),
                                    ],
                                    Expanded(
                                      child: OutlinedButton.icon(
                                        style: OutlinedButton.styleFrom(
                                          foregroundColor: UnColors.red,
                                        ),
                                        onPressed: () => _decide(
                                          'reject',
                                          verb: 'Reject',
                                        ),
                                        icon: const Icon(
                                          Icons.cancel_outlined,
                                        ),
                                        label: const Text('Reject'),
                                      ),
                                    ),
                                  ],
                                ),
                              ],
                            ),
                    ),
                  ),
              ],
            );
          },
        ),
      ),
    );
  }
}
