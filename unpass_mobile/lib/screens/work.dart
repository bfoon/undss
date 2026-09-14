import 'package:flutter/material.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'form_renderer.dart';
import 'pdf_view.dart';
import 'sign.dart';

// ─────────────────────────────────────────────────────────────────────────────
// Forms
// ─────────────────────────────────────────────────────────────────────────────

class FormsTab extends StatefulWidget {
  const FormsTab({super.key});
  @override
  State<FormsTab> createState() => _FormsTabState();
}

class _FormsTabState extends State<FormsTab> with SingleTickerProviderStateMixin {
  late Future<Map<String, dynamic>> _future = Api.instance.forms();
  late final _tabs = TabController(length: 2, vsync: this);

  void _refresh() => setState(() => _future = Api.instance.forms());

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Material(
          color: Colors.white,
          child: TabBar(
            controller: _tabs,
            labelColor: UnColors.darkBlue,
            indicatorColor: UnColors.blue,
            tabs: const [Tab(text: 'Fill in a form'), Tab(text: 'My submissions')],
          ),
        ),
        Expanded(
          child: FutureBuilder<Map<String, dynamic>>(
            future: _future,
            builder: (context, snap) {
              if (snap.connectionState == ConnectionState.waiting) return const Loading();
              if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: _refresh);
              final data = snap.data!;
              return TabBarView(
                controller: _tabs,
                children: [
                  _formList(data['fillable'] as List<dynamic>),
                  _submissionList(data['submissions'] as List<dynamic>),
                ],
              );
            },
          ),
        ),
      ],
    );
  }

  Widget _formList(List<dynamic> forms) {
    if (forms.isEmpty) {
      return const EmptyState(
        icon: Icons.description_outlined,
        title: 'No forms shared with you',
        detail: 'Forms your office publishes will appear here, ready to fill in.',
      );
    }
    return RefreshIndicator(
      onRefresh: () async => _refresh(),
      child: ListView.separated(
        padding: const EdgeInsets.all(UnStyle.gap),
        itemCount: forms.length,
        separatorBuilder: (_, __) => const SizedBox(height: 10),
        itemBuilder: (context, i) {
          final f = forms[i] as Map<String, dynamic>;
          return InkWell(
            borderRadius: BorderRadius.circular(UnStyle.radius),
            onTap: () async {
              final sent = await Navigator.of(context).push<bool>(
                MaterialPageRoute(builder: (_) => FillFormScreen(formId: f['id'] as int, title: '${f['name']}')),
              );
              if (sent == true) _refresh();
            },
            child: Container(
              padding: const EdgeInsets.all(14),
              decoration: UnStyle.card(),
              child: Row(
                children: [
                  Container(
                    height: 42, width: 42,
                    decoration: BoxDecoration(
                        color: UnColors.purple.withOpacity(0.12), borderRadius: BorderRadius.circular(11)),
                    child: const Icon(Icons.edit_note, color: UnColors.purple),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${f['name']}',
                            style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
                        if ('${f['description'] ?? ''}'.isNotEmpty) ...[
                          const SizedBox(height: 3),
                          Text('${f['description']}',
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(fontSize: 12.5, color: UnColors.muted)),
                        ],
                        if ('${f['workflow'] ?? ''}'.isNotEmpty) ...[
                          const SizedBox(height: 6),
                          StatusPill('Starts: ${f['workflow']}', color: UnColors.blue),
                        ],
                      ],
                    ),
                  ),
                  const Icon(Icons.chevron_right, color: UnColors.muted),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _submissionList(List<dynamic> subs) {
    if (subs.isEmpty) {
      return const EmptyState(
        icon: Icons.inbox_outlined,
        title: 'Nothing submitted yet',
        detail: 'Forms you send will be listed here with their reference and progress.',
      );
    }
    return RefreshIndicator(
      onRefresh: () async => _refresh(),
      child: ListView.separated(
        padding: const EdgeInsets.all(UnStyle.gap),
        itemCount: subs.length,
        separatorBuilder: (_, __) => const SizedBox(height: 10),
        itemBuilder: (context, i) {
          final s = subs[i] as Map<String, dynamic>;
          return InkWell(
            borderRadius: BorderRadius.circular(UnStyle.radius),
            onTap: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => SubmissionScreen(id: s['id'] as int)),
            ),
            child: Container(
              padding: const EdgeInsets.all(14),
              decoration: UnStyle.card(),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('${s['form']}',
                            style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
                        const SizedBox(height: 3),
                        Text('${s['reference']}',
                            style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
                      ],
                    ),
                  ),
                  StatusPill('${s['status_label']}'),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

/// Fill in and send a form. The rules run as you type, so a section that
/// doesn't apply greys itself out just as it does on the website.
class FillFormScreen extends StatefulWidget {
  const FillFormScreen({super.key, required this.formId, required this.title});
  final int formId;
  final String title;

  @override
  State<FillFormScreen> createState() => _FillFormScreenState();
}

class _FillFormScreenState extends State<FillFormScreen> {
  late Future<Map<String, dynamic>> _future = Api.instance.form(widget.formId);
  final _renderer = GlobalKey<FormRendererState>();
  Map<String, dynamic> _values = {};
  final Map<String, TextEditingController> _slots = {};
  bool _busy = false;

  @override
  void dispose() {
    for (final c in _slots.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _submit(Map<String, dynamic> form) async {
    final state = _renderer.currentState;
    if (state == null) return;
    final gaps = state.missing();
    if (gaps.isNotEmpty) {
      showNote(context, 'Still needed: ${gaps.take(3).join(', ')}${gaps.length > 3 ? '…' : ''}', error: true);
      return;
    }
    final slots = <String, String>{};
    for (final entry in _slots.entries) {
      if (entry.value.text.trim().isEmpty) {
        showNote(context, 'Choose who handles "${entry.key}".', error: true);
        return;
      }
      slots[entry.key] = entry.value.text.trim();
    }

    setState(() => _busy = true);
    try {
      final data = await Api.instance.submitForm(widget.formId, values: state.answers(), slots: slots);
      if (!mounted) return;
      showNote(context, '${data['message']}');
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
      appBar: AppBar(title: Text(widget.title, overflow: TextOverflow.ellipsis)),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) return const Loading();
          if (snap.hasError) {
            return FailureState(
                message: '${snap.error}', onRetry: () => setState(() => _future = Api.instance.form(widget.formId)));
          }
          final form = snap.data!;
          final schema = form['schema'] as Map<String, dynamic>;
          final slots = (form['slots'] as List<dynamic>? ?? []).map((e) => '$e').toList();
          if (_values.isEmpty) {
            _values = Map<String, dynamic>.from(form['prefill'] as Map? ?? {});
          }

          return Column(
            children: [
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(UnStyle.gap, UnStyle.gap, UnStyle.gap, 24),
                  children: [
                    if ('${form['description'] ?? ''}'.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: Text('${form['description']}',
                            style: const TextStyle(color: UnColors.muted, height: 1.4)),
                      ),
                    FormRenderer(
                      key: _renderer,
                      schema: schema,
                      values: _values,
                      onChanged: (v) => _values = v,
                    ),
                    if (slots.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      SectionCard(
                        title: 'WHO SHOULD HANDLE THIS',
                        icon: Icons.people_outline,
                        child: Column(
                          children: [
                            for (final slot in slots)
                              Padding(
                                padding: const EdgeInsets.only(bottom: 10),
                                child: TextField(
                                  controller: _slots.putIfAbsent(slot, () => TextEditingController()),
                                  keyboardType: TextInputType.emailAddress,
                                  decoration: InputDecoration(
                                    labelText: slot,
                                    hintText: 'name@undp.org',
                                    prefixIcon: const Icon(Icons.person_outline),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
                  ],
                ),
              ),
              Container(
                padding: EdgeInsets.fromLTRB(
                    UnStyle.gap, 12, UnStyle.gap, 12 + MediaQuery.of(context).padding.bottom),
                decoration: const BoxDecoration(
                  color: Colors.white,
                  border: Border(top: BorderSide(color: UnColors.line)),
                ),
                child: _busy
                    ? const Center(child: Padding(padding: EdgeInsets.all(8), child: CircularProgressIndicator()))
                    : FilledButton.icon(
                        onPressed: () => _submit(form),
                        icon: const Icon(Icons.send),
                        label: const Text('Submit'),
                      ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class SubmissionScreen extends StatelessWidget {
  const SubmissionScreen({super.key, required this.id});
  final int id;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Submission')),
      body: FutureBuilder<Map<String, dynamic>>(
        future: Api.instance.submission(id),
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) return const Loading();
          if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: () {});
          final s = snap.data!;
          final answers = (s['answers'] as List<dynamic>? ?? []);
          final runs = (s['runs'] as List<dynamic>? ?? []);
          return ListView(
            padding: const EdgeInsets.all(UnStyle.gap),
            children: [
              Text('${s['form']}',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 4),
              Row(children: [
                Text('${s['reference']}',
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
                const SizedBox(width: 10),
                StatusPill('${s['status_label']}'),
              ]),
              const SizedBox(height: UnStyle.gap),
              if (runs.isNotEmpty)
                SectionCard(
                  title: 'PROGRESS',
                  icon: Icons.timeline,
                  child: Column(
                    children: [
                      for (final r in runs)
                        DetailRow('${(r as Map)['reference']}', '${r['status']}'),
                    ],
                  ),
                ),
              SectionCard(
                title: 'YOUR ANSWERS',
                icon: Icons.list_alt,
                child: Column(
                  children: [
                    for (final a in answers) DetailRow('${(a as Map)['label']}', '${a['value']}'),
                  ],
                ),
              ),
              OutlinedButton.icon(
                onPressed: () => openPdf(context, '${s['pdf_url']}',
                    title: '${s['form']}', subtitle: '${s['reference']}'),
                icon: const Icon(Icons.menu_book_outlined),
                label: const Text('Read the PDF'),
              ),
            ],
          );
        },
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Envelopes
// ─────────────────────────────────────────────────────────────────────────────

class EnvelopesTab extends StatefulWidget {
  const EnvelopesTab({super.key});
  @override
  State<EnvelopesTab> createState() => _EnvelopesTabState();
}

class _EnvelopesTabState extends State<EnvelopesTab> {
  late Future<List<dynamic>> _future = Api.instance.envelopes();
  String _status = '';

  void _refresh() => setState(() => _future = Api.instance.envelopes(status: _status));

  @override
  Widget build(BuildContext context) {
    const filters = {'': 'All', 'sent': 'Out for signature', 'completed': 'Completed', 'draft': 'Drafts'};
    return Column(
      children: [
        SizedBox(
          height: 56,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            children: [
              for (final entry in filters.entries)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(entry.value),
                    selected: _status == entry.key,
                    selectedColor: UnColors.lightBlue,
                    onSelected: (_) {
                      _status = entry.key;
                      _refresh();
                    },
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: RefreshIndicator(
            onRefresh: () async => _refresh(),
            child: FutureBuilder<List<dynamic>>(
              future: _future,
              builder: (context, snap) {
                if (snap.connectionState == ConnectionState.waiting) return const Loading();
                if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: _refresh);
                final rows = snap.data ?? [];
                if (rows.isEmpty) {
                  return const EmptyState(
                    icon: Icons.mail_outline,
                    title: 'No envelopes',
                    detail: 'Documents sent for signature appear here, with their progress.',
                  );
                }
                return ListView.separated(
                  padding: const EdgeInsets.all(UnStyle.gap),
                  itemCount: rows.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 10),
                  itemBuilder: (context, i) {
                    final e = rows[i] as Map<String, dynamic>;
                    final signed = (e['recipients'] as List<dynamic>).where((r) => (r as Map)['status'] == 'signed').length;
                    final total = (e['recipients'] as List<dynamic>).length;
                    return InkWell(
                      borderRadius: BorderRadius.circular(UnStyle.radius),
                      onTap: () async {
                        final changed = await Navigator.of(context).push<bool>(
                          MaterialPageRoute(builder: (_) => EnvelopeScreen(id: e['id'] as int)),
                        );
                        if (changed == true) _refresh();
                      },
                      child: Container(
                        padding: const EdgeInsets.all(14),
                        decoration: UnStyle.card(),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              children: [
                                Expanded(
                                  child: Text('${e['subject']}',
                                      style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
                                ),
                                StatusPill('${e['status_label']}'),
                              ],
                            ),
                            const SizedBox(height: 6),
                            Row(children: [
                              Text('${e['envelope_id']}',
                                  style: const TextStyle(
                                      fontFamily: 'monospace', fontSize: 11.5, color: UnColors.muted)),
                              const Spacer(),
                              Text('$signed of $total signed',
                                  style: const TextStyle(fontSize: 12, color: UnColors.muted)),
                            ]),
                          ],
                        ),
                      ),
                    );
                  },
                );
              },
            ),
          ),
        ),
      ],
    );
  }
}

class EnvelopeScreen extends StatefulWidget {
  const EnvelopeScreen({super.key, required this.id});
  final int id;
  @override
  State<EnvelopeScreen> createState() => _EnvelopeScreenState();
}

class _EnvelopeScreenState extends State<EnvelopeScreen> {
  late Future<Map<String, dynamic>> _future = Api.instance.envelope(widget.id);
  bool _changed = false;

  void _reload() => setState(() => _future = Api.instance.envelope(widget.id));

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Envelope'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.of(context).pop(_changed),
        ),
      ),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) return const Loading();
          if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: _reload);
          final e = snap.data!;
          final events = (e['events'] as List<dynamic>? ?? []);
          return ListView(
            padding: const EdgeInsets.all(UnStyle.gap),
            children: [
              Text('${e['subject']}',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 6),
              Row(children: [
                Text('${e['envelope_id']}',
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
                const SizedBox(width: 10),
                StatusPill('${e['status_label']}'),
              ]),
              const SizedBox(height: UnStyle.gap),
              if (e['can_sign'] == true)
                Padding(
                  padding: const EdgeInsets.only(bottom: UnStyle.gap),
                  child: FilledButton.icon(
                    style: FilledButton.styleFrom(backgroundColor: UnColors.green),
                    onPressed: () async {
                      final done = await Navigator.of(context).push<bool>(MaterialPageRoute(
                        builder: (_) => SignScreen(token: '${e['my_token']}', subject: '${e['subject']}'),
                      ));
                      if (done == true) {
                        _changed = true;
                        _reload();
                      }
                    },
                    icon: const Icon(Icons.draw_outlined),
                    label: const Text('Sign this document'),
                  ),
                ),
              SectionCard(
                title: 'WHO IS ON IT',
                icon: Icons.people_outline,
                child: Column(
                  children: [
                    for (final r in (e['recipients'] as List<dynamic>))
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 6),
                        child: Row(
                          children: [
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text('${(r as Map)['name']}${r['is_me'] == true ? ' (you)' : ''}',
                                      style: const TextStyle(fontWeight: FontWeight.w600)),
                                  Text('${r['email']} · ${r['role']}',
                                      style: const TextStyle(fontSize: 12, color: UnColors.muted)),
                                ],
                              ),
                            ),
                            StatusPill('${r['status']}'),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
              SectionCard(
                title: 'DOCUMENTS',
                icon: Icons.picture_as_pdf_outlined,
                child: Column(
                  children: [
                    for (final d in (e['documents'] as List<dynamic>))
                      DetailRow('${(d as Map)['name']}', '${d['pages']} page(s)'),
                  ],
                ),
              ),
              if (events.isNotEmpty)
                SectionCard(
                  title: 'AUDIT TRAIL',
                  icon: Icons.history,
                  child: Column(
                    children: [
                      for (final ev in events.take(12))
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 5),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Icon(Icons.circle, size: 7, color: UnColors.blue),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text('${(ev as Map)['note']}', style: const TextStyle(fontSize: 13.5)),
                                    Text('${ev['at']}'.split('T').first,
                                        style: const TextStyle(fontSize: 11.5, color: UnColors.muted)),
                                  ],
                                ),
                              ),
                            ],
                          ),
                        ),
                    ],
                  ),
                ),
              OutlinedButton.icon(
                onPressed: () => openPdf(context, '${e['final_url']}',
                    title: '${e['subject']}', subtitle: '${e['envelope_id']}'),
                icon: const Icon(Icons.menu_book_outlined),
                label: const Text('Read the signed PDF'),
              ),
            ],
          );
        },
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Flows and runs
// ─────────────────────────────────────────────────────────────────────────────

class FlowsTab extends StatefulWidget {
  const FlowsTab({super.key});
  @override
  State<FlowsTab> createState() => _FlowsTabState();
}

class _FlowsTabState extends State<FlowsTab> {
  late Future<List<dynamic>> _future = Api.instance.runs(status: 'open');
  String _status = 'open';

  void _refresh() => setState(() => _future = Api.instance.runs(status: _status));

  @override
  Widget build(BuildContext context) {
    const filters = {'open': 'In progress', 'completed': 'Completed', '': 'All'};
    return Column(
      children: [
        SizedBox(
          height: 56,
          child: ListView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            children: [
              for (final f in filters.entries)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(f.value),
                    selected: _status == f.key,
                    selectedColor: UnColors.lightBlue,
                    onSelected: (_) {
                      _status = f.key;
                      _refresh();
                    },
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: RefreshIndicator(
            onRefresh: () async => _refresh(),
            child: FutureBuilder<List<dynamic>>(
              future: _future,
              builder: (context, snap) {
                if (snap.connectionState == ConnectionState.waiting) return const Loading();
                if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: _refresh);
                final runs = snap.data ?? [];
                if (runs.isEmpty) {
                  return const EmptyState(
                    icon: Icons.account_tree_outlined,
                    title: 'No runs to show',
                    detail: 'Requests moving through a workflow appear here, with every step.',
                  );
                }
                return ListView.separated(
                  padding: const EdgeInsets.all(UnStyle.gap),
                  itemCount: runs.length,
                  separatorBuilder: (_, __) => const SizedBox(height: 10),
                  itemBuilder: (context, i) {
                    final r = runs[i] as Map<String, dynamic>;
                    return InkWell(
                      borderRadius: BorderRadius.circular(UnStyle.radius),
                      onTap: () async {
                        final changed = await Navigator.of(context).push<bool>(
                          MaterialPageRoute(builder: (_) => RunScreen(id: r['id'] as int)),
                        );
                        if (changed == true) _refresh();
                      },
                      child: Container(
                        padding: const EdgeInsets.all(14),
                        decoration: UnStyle.card(),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(children: [
                              Expanded(
                                child: Text('${r['subject']}',
                                    style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
                              ),
                              StatusPill('${r['status_label']}'),
                            ]),
                            const SizedBox(height: 6),
                            Text('${r['flow']} · ${r['reference']}',
                                style: const TextStyle(fontSize: 12, color: UnColors.muted)),
                          ],
                        ),
                      ),
                    );
                  },
                );
              },
            ),
          ),
        ),
      ],
    );
  }
}

class RunScreen extends StatefulWidget {
  const RunScreen({super.key, required this.id});
  final int id;
  @override
  State<RunScreen> createState() => _RunScreenState();
}

class _RunScreenState extends State<RunScreen> {
  late Future<Map<String, dynamic>> _future = Api.instance.run(widget.id);
  bool _changed = false;

  void _reload() => setState(() => _future = Api.instance.run(widget.id));

  Future<void> _cancel() async {
    final controller = TextEditingController();
    final reason = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Cancel this run?'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(labelText: 'Reason', hintText: 'Everyone waiting will be told'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(dialogContext), child: const Text('Keep it')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: UnColors.red),
            onPressed: () => Navigator.pop(dialogContext, controller.text.trim()),
            child: const Text('Cancel the run'),
          ),
        ],
      ),
    );
    if (reason == null) return;
    try {
      await Api.instance.cancelRun(widget.id, reason);
      _changed = true;
      if (!mounted) return;
      showNote(context, 'The run has been cancelled.');
      _reload();
    } on ApiException catch (e) {
      if (mounted) showNote(context, e.message, error: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Run'),
        leading: IconButton(
            icon: const Icon(Icons.arrow_back), onPressed: () => Navigator.of(context).pop(_changed)),
      ),
      body: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) return const Loading();
          if (snap.hasError) return FailureState(message: '${snap.error}', onRetry: _reload);
          final r = snap.data!;
          final steps = (r['steps'] as List<dynamic>? ?? []);
          return ListView(
            padding: const EdgeInsets.all(UnStyle.gap),
            children: [
              Text('${r['subject']}',
                  style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 6),
              Row(children: [
                Text('${r['reference']}',
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
                const SizedBox(width: 10),
                StatusPill('${r['status_label']}'),
              ]),
              const SizedBox(height: UnStyle.gap),
              SectionCard(
                title: 'STEPS AND PEOPLE',
                icon: Icons.account_tree_outlined,
                child: Column(
                  children: [
                    for (final s in steps)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 7),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Icon(
                              (s as Map)['status'] == 'approved' || s['status'] == 'done'
                                  ? Icons.check_circle
                                  : s['status'] == 'pending'
                                      ? Icons.schedule
                                      : Icons.circle_outlined,
                              size: 18,
                              color: statusColor('${s['status']}'),
                            ),
                            const SizedBox(width: 10),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text('${s['step']}', style: const TextStyle(fontWeight: FontWeight.w600)),
                                  Text('${s['who']}',
                                      style: const TextStyle(fontSize: 12.5, color: UnColors.muted)),
                                  if ('${s['comment'] ?? ''}'.isNotEmpty)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 4),
                                      child: Text('“${s['comment']}”',
                                          style: const TextStyle(
                                              fontSize: 12.5, fontStyle: FontStyle.italic, color: UnColors.ink)),
                                    ),
                                ],
                              ),
                            ),
                            StatusPill('${s['status']}'),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
              OutlinedButton.icon(
                onPressed: () => openPdf(context, '${r['document_url']}',
                    title: '${r['subject']}', subtitle: '${r['reference']}'),
                icon: const Icon(Icons.menu_book_outlined),
                label: const Text('Read the document'),
              ),
              if (r['can_cancel'] == true) ...[
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(foregroundColor: UnColors.red),
                  onPressed: _cancel,
                  icon: const Icon(Icons.stop_circle_outlined),
                  label: const Text('Cancel this run'),
                ),
              ],
            ],
          );
        },
      ),
    );
  }
}
