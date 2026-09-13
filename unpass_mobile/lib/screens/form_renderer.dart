import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/theme.dart';
import '../widgets/common.dart';

/// The browser's rule engine, ported.
///
/// This is the same shape as `esign_condition_engine.py` and the JavaScript in
/// `_rules.html`: a group of rules joined by and/or, with an optional `not`.
/// It has to agree with them, because the server checks the same rules again
/// when the form arrives — the app deciding differently would only produce a
/// rejected submission and a confused person.
class FormRules {
  static num? _num(dynamic v) {
    if (v == null) return null;
    if (v is num) return v;
    final s = v.toString().replaceAll(',', '').trim();
    if (s.isEmpty) return null;
    return num.tryParse(s);
  }

  static DateTime? _date(dynamic v) {
    final s = (v ?? '').toString().trim();
    if (!RegExp(r'^\d{4}-\d{2}-\d{2}').hasMatch(s)) return null;
    return DateTime.tryParse(s.substring(0, 10));
  }

  static String _text(dynamic v) {
    if (v == null) return '';
    if (v is List) return v.map((e) => '$e').join(', ');
    return '$v';
  }

  static List<String> _list(dynamic v) {
    if (v is List) return v.map((e) => '$e'.trim().toLowerCase()).where((e) => e.isNotEmpty).toList();
    if (v == null || '$v'.isEmpty) return [];
    return ['$v'.trim().toLowerCase()];
  }

  static bool _truthy(dynamic v) {
    if (v is bool) return v;
    if (v is num) return v != 0;
    if (v is List) return v.isNotEmpty;
    return ['1', 'true', 'yes', 'y', 'on', 'approved'].contains('${v ?? ''}'.trim().toLowerCase());
  }

  /// Values the rules can read, including the computed ones (`@sum:`, `@days:`).
  static Map<String, dynamic> derive(Map<String, dynamic> values, List<dynamic> elements) {
    final out = Map<String, dynamic>.from(values);
    final dates = <String>[];
    for (final raw in elements) {
      final el = raw as Map<String, dynamic>;
      final key = el['key'] as String?;
      if (key == null) continue;
      final v = out[key];
      switch (el['type']) {
        case 'table':
          final rows = (v is List ? v : const []).whereType<Map>().toList();
          out['@rows:$key'] = rows.length;
          for (final col in (el['columns'] as List<dynamic>? ?? [])) {
            final c = col as Map<String, dynamic>;
            if (c['kind'] != 'number') continue;
            final nums = rows.map((r) => _num(r[c['key']])).whereType<num>().toList();
            out['@sum:$key:${c['key']}'] = nums.fold<num>(0, (a, b) => a + b);
            if (nums.isNotEmpty) {
              out['@min:$key:${c['key']}'] = nums.reduce((a, b) => a < b ? a : b);
              out['@max:$key:${c['key']}'] = nums.reduce((a, b) => a > b ? a : b);
            }
          }
          break;
        case 'checkboxes':
          out['@count:$key'] = _list(v).length;
          break;
        case 'date':
          dates.add(key);
          break;
      }
    }
    for (final a in dates) {
      for (final b in dates) {
        if (a == b) continue;
        final start = _date(out[a]), end = _date(out[b]);
        if (start != null && end != null) out['@days:$a:$b'] = end.difference(start).inDays;
      }
    }
    return out;
  }

  static bool evaluate(Map<String, dynamic> values, Map<String, dynamic>? tree) {
    if (tree == null) return false;
    final rules = (tree['rules'] as List<dynamic>? ?? []);
    final results = rules.map((r) {
      final node = r as Map<String, dynamic>;
      return node['type'] == 'group' || node.containsKey('rules')
          ? evaluate(values, node)
          : _rule(values, node);
    }).toList();
    final result = results.isEmpty
        ? false
        : (tree['logic'] == 'or' ? results.any((x) => x) : results.every((x) => x));
    return tree['negate'] == true ? !result : result;
  }

  static bool _rule(Map<String, dynamic> values, Map<String, dynamic> rule) {
    final op = '${rule['op']}';
    final left = values[rule['field']];
    final right = rule['compare'] == 'field' ? values[rule['field2']] : rule['value'];
    final leftText = _text(left).trim(), rightText = _text(right).trim();
    final empty = (left is List || left is Map) ? !_truthy(left) : leftText.isEmpty;

    switch (op) {
      case 'empty':
        return empty;
      case 'not_empty':
        return !empty;
      case 'truthy':
        return _truthy(left);
      case 'falsy':
        return !_truthy(left);
      case 'in':
      case 'not_in':
      case 'includes_any':
      case 'includes_all':
        final wanted = rightText.split(',').map((e) => e.trim().toLowerCase()).where((e) => e.isNotEmpty).toList();
        if (wanted.isEmpty) return false;
        if (op == 'in') return wanted.contains(leftText.toLowerCase());
        if (op == 'not_in') return !wanted.contains(leftText.toLowerCase());
        final chosen = _list(left).toSet();
        return op == 'includes_any' ? wanted.any(chosen.contains) : wanted.every(chosen.contains);
      case 'in_past':
      case 'in_future':
      case 'within_days':
      case 'beyond_days':
        final d = _date(left);
        if (d == null) return false;
        final today = DateTime.now();
        final start = DateTime(today.year, today.month, today.day);
        if (op == 'in_past') return d.isBefore(start);
        if (op == 'in_future') return d.isAfter(start);
        final span = _num(right)?.toInt();
        if (span == null) return false;
        final edge = start.add(Duration(days: span));
        return op == 'within_days'
            ? !d.isBefore(start) && !d.isAfter(edge)
            : d.isAfter(edge);
      case 'between':
      case 'not_between':
        final a = _num(left), b = _num(right), c = _num(rule['value2']);
        if (a == null || b == null || c == null) return false;
        final lo = b < c ? b : c, hi = b < c ? c : b;
        final inside = a >= lo && a <= hi;
        return op == 'between' ? inside : !inside;
      case 'gt':
      case 'gte':
      case 'lt':
      case 'lte':
        final a = _num(left), b = _num(right);
        if (a == null || b == null) return false;
        return op == 'gt' ? a > b : op == 'gte' ? a >= b : op == 'lt' ? a < b : a <= b;
      case 'before':
      case 'after':
      case 'on_or_before':
      case 'on_or_after':
        final a = _date(left), b = _date(right);
        if (a == null || b == null) return false;
        return op == 'before'
            ? a.isBefore(b)
            : op == 'after'
                ? a.isAfter(b)
                : op == 'on_or_before'
                    ? !a.isAfter(b)
                    : !a.isBefore(b);
      case 'contains':
        return rightText.isNotEmpty && leftText.toLowerCase().contains(rightText.toLowerCase());
      case 'not_contains':
        return rightText.isEmpty || !leftText.toLowerCase().contains(rightText.toLowerCase());
      case 'starts_with':
        return rightText.isNotEmpty && leftText.toLowerCase().startsWith(rightText.toLowerCase());
      case 'ends_with':
        return rightText.isNotEmpty && leftText.toLowerCase().endsWith(rightText.toLowerCase());
      case 'neq':
        final a = _num(left), b = _num(right);
        return (a != null && b != null) ? a != b : leftText.toLowerCase() != rightText.toLowerCase();
      default:
        final a = _num(left), b = _num(right);
        return (a != null && b != null) ? a == b : leftText.toLowerCase() == rightText.toLowerCase();
    }
  }

  /// The state of every element: active, readonly, completed, disabled, hidden.
  /// A heading's rule covers everything under it, to the next heading.
  static Map<String, Map<String, dynamic>> states(
      List<dynamic> elements, Map<String, dynamic> values, String scope) {
    final vals = derive(values, elements);
    final out = <String, Map<String, dynamic>>{};
    Map<String, dynamic>? section;
    var sectionFill = 'submitter';

    for (final raw in elements) {
      final el = raw as Map<String, dynamic>;
      if (el['type'] == 'heading') {
        section = (el['state_rules'] as List<dynamic>? ?? []).isNotEmpty ? el : null;
        sectionFill = '${el['fill_by'] ?? 'submitter'}';
      }
      Map<String, dynamic>? found;
      for (final owner in [el, if (section != null && section != el) section]) {
        for (final r in (owner!['state_rules'] as List<dynamic>? ?? [])) {
          final rule = r as Map<String, dynamic>;
          if (evaluate(vals, rule['when'] as Map<String, dynamic>?)) {
            found = {
              'state': rule['state'],
              'message': rule['message'] ?? '',
              'from': owner == el ? 'own' : 'section',
            };
            break;
          }
        }
        if (found != null) break;
      }
      if (found == null) {
        final own = '${el['fill_by'] ?? 'inherit'}';
        final fill = el['type'] == 'heading' ? sectionFill : (own == 'inherit' ? sectionFill : own);
        final key = el['key'] as String?;
        final filled = key != null && _truthy(values[key] is List ? values[key] : '${values[key] ?? ''}');
        if (key != null && filled && fill != scope) {
          found = {'state': 'completed', 'message': 'Already filled', 'from': 'auto'};
        } else if (key != null && fill != scope) {
          found = {'state': 'readonly', 'message': 'Completed later in the workflow', 'from': 'auto'};
        } else {
          found = {'state': 'active', 'message': '', 'from': 'auto'};
        }
      }
      out['${el['id']}'] = found;
    }
    return out;
  }
}

/// Draws a UN PASS form from its schema and keeps the answers.
///
/// Every question type the designer offers is here, tables included, and the
/// rules are re-applied on every keystroke — so a section that doesn't apply
/// greys out on the phone exactly as it does in the browser.
class FormRenderer extends StatefulWidget {
  const FormRenderer({
    super.key,
    required this.schema,
    required this.values,
    required this.onChanged,
    this.scope = 'submitter',
    this.errors = const {},
  });

  final Map<String, dynamic> schema;
  final Map<String, dynamic> values;
  final void Function(Map<String, dynamic>) onChanged;
  final String scope;
  final Map<String, String> errors;

  @override
  State<FormRenderer> createState() => FormRendererState();
}

class FormRendererState extends State<FormRenderer> {
  late Map<String, dynamic> _values = Map<String, dynamic>.from(widget.values);
  final _controllers = <String, TextEditingController>{};

  List<dynamic> get _elements => widget.schema['elements'] as List<dynamic>? ?? [];

  @override
  void dispose() {
    for (final c in _controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  void _set(String key, dynamic value) {
    setState(() => _values[key] = value);
    widget.onChanged(Map<String, dynamic>.from(_values));
  }

  TextEditingController _controllerFor(String key, {String? seed}) =>
      _controllers.putIfAbsent(key, () => TextEditingController(text: seed ?? '${_values[key] ?? ''}'));

  /// Answers to send: anything the rules switched off is cleared, matching
  /// what the server does when it checks the same rules again.
  Map<String, dynamic> answers() {
    final states = FormRules.states(_elements, _values, widget.scope);
    final out = <String, dynamic>{};
    for (final raw in _elements) {
      final el = raw as Map<String, dynamic>;
      final key = el['key'] as String?;
      if (key == null) continue;
      final state = states['${el['id']}']?['state'];
      if (state == 'hidden' || state == 'disabled') continue;
      if (_values.containsKey(key)) out[key] = _values[key];
    }
    return out;
  }

  /// Anything required and still empty, so the app can say so before sending.
  List<String> missing() {
    final states = FormRules.states(_elements, _values, widget.scope);
    final gaps = <String>[];
    for (final raw in _elements) {
      final el = raw as Map<String, dynamic>;
      final key = el['key'] as String?;
      if (key == null || el['required'] != true) continue;
      final state = states['${el['id']}']?['state'];
      if (state != 'active') continue;
      final v = _values[key];
      final empty = v == null || (v is String && v.trim().isEmpty) || (v is List && v.isEmpty);
      if (empty) gaps.add('${el['label'] ?? key}');
    }
    return gaps;
  }

  @override
  Widget build(BuildContext context) {
    final states = FormRules.states(_elements, _values, widget.scope);
    final children = <Widget>[];

    for (final raw in _elements) {
      final el = raw as Map<String, dynamic>;
      final state = states['${el['id']}'] ?? const {'state': 'active', 'message': ''};
      if (state['state'] == 'hidden') continue;
      children.add(_element(el, state));
    }
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: children);
  }

  Widget _element(Map<String, dynamic> el, Map<String, dynamic> state) {
    final type = '${el['type']}';
    final off = state['state'] == 'disabled';
    final readOnly = state['state'] == 'readonly' || state['state'] == 'completed';
    final note = '${state['message'] ?? ''}';

    Widget body;
    switch (type) {
      case 'heading':
        return Padding(
          padding: const EdgeInsets.only(top: 22, bottom: 10),
          child: Row(
            children: [
              Flexible(
                child: Text('${el['text']}',
                    style: TextStyle(
                        fontSize: 17,
                        fontWeight: FontWeight.w700,
                        color: off ? UnColors.muted : UnColors.darkBlue)),
              ),
              if (off && note.isNotEmpty) ...[const SizedBox(width: 8), Flexible(child: StatusPill(note))],
            ],
          ),
        );
      case 'paragraph':
        return Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Text('${el['text']}', style: const TextStyle(color: UnColors.muted, height: 1.4)),
        );
      case 'divider':
        return const Padding(padding: EdgeInsets.symmetric(vertical: 14), child: Divider());
      case 'spacer':
        return SizedBox(height: (el['height'] as num?)?.toDouble() ?? 16);
      case 'signature':
        return Padding(
          padding: const EdgeInsets.only(bottom: 14),
          child: Container(
            padding: const EdgeInsets.all(14),
            decoration: UnStyle.card(color: UnColors.canvas),
            child: Row(children: [
              const Icon(Icons.draw_outlined, color: UnColors.blue),
              const SizedBox(width: 10),
              Expanded(
                child: Text('${el['label'] ?? 'Signature'} — signed later in the workflow',
                    style: const TextStyle(color: UnColors.muted)),
              ),
            ]),
          ),
        );
      default:
        body = _input(el, off || readOnly);
    }

    final key = '${el['key'] ?? ''}';
    final error = widget.errors[key];
    return Opacity(
      opacity: off ? 0.55 : 1,
      child: Padding(
        padding: const EdgeInsets.only(bottom: 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Flexible(
                  child: RichText(
                    text: TextSpan(
                      style: const TextStyle(fontSize: 14.5, fontWeight: FontWeight.w600, color: UnColors.ink),
                      children: [
                        TextSpan(text: '${el['label'] ?? ''}'),
                        if (el['required'] == true && state['state'] == 'active')
                          const TextSpan(text: ' *', style: TextStyle(color: UnColors.red)),
                      ],
                    ),
                  ),
                ),
                if (note.isNotEmpty && state['from'] != 'section') ...[
                  const SizedBox(width: 8),
                  StatusPill(note),
                ],
              ],
            ),
            const SizedBox(height: 6),
            body,
            if (error != null) ...[const SizedBox(height: 6), ErrorNote(error)],
            if ('${el['help'] ?? ''}'.isNotEmpty && error == null) ...[
              const SizedBox(height: 4),
              Text('${el['help']}', style: const TextStyle(fontSize: 12, color: UnColors.muted)),
            ],
          ],
        ),
      ),
    );
  }

  Widget _input(Map<String, dynamic> el, bool locked) {
    final key = '${el['key']}';
    final type = '${el['type']}';
    final options = (el['options'] as List<dynamic>? ?? []).map((e) => '$e').toList();

    switch (type) {
      case 'textarea':
        return TextField(
          controller: _controllerFor(key),
          enabled: !locked,
          maxLines: (el['rows'] as num?)?.toInt() ?? 4,
          decoration: InputDecoration(hintText: '${el['placeholder'] ?? ''}'),
          onChanged: (v) => _set(key, v),
        );
      case 'number':
        return TextField(
          controller: _controllerFor(key),
          enabled: !locked,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.,\-]'))],
          decoration: InputDecoration(hintText: '${el['placeholder'] ?? ''}'),
          onChanged: (v) => _set(key, v),
        );
      case 'email':
        return TextField(
          controller: _controllerFor(key),
          enabled: !locked,
          keyboardType: TextInputType.emailAddress,
          decoration: InputDecoration(hintText: '${el['placeholder'] ?? 'name@undp.org'}'),
          onChanged: (v) => _set(key, v),
        );
      case 'date':
        final shown = '${_values[key] ?? ''}';
        return InkWell(
          onTap: locked
              ? null
              : () async {
                  final now = DateTime.now();
                  final picked = await showDatePicker(
                    context: context,
                    initialDate: DateTime.tryParse(shown) ?? now,
                    firstDate: DateTime(now.year - 5),
                    lastDate: DateTime(now.year + 5),
                  );
                  if (picked != null) {
                    _set(key, picked.toIso8601String().substring(0, 10));
                  }
                },
          child: InputDecorator(
            decoration: const InputDecoration(suffixIcon: Icon(Icons.calendar_today, size: 18)),
            child: Text(shown.isEmpty ? 'Choose a date' : shown,
                style: TextStyle(color: shown.isEmpty ? UnColors.muted : UnColors.ink)),
          ),
        );
      case 'select':
        return DropdownButtonFormField<String>(
          value: options.contains('${_values[key] ?? ''}') ? '${_values[key]}' : null,
          items: options.map((o) => DropdownMenuItem(value: o, child: Text(o))).toList(),
          onChanged: locked ? null : (v) => _set(key, v ?? ''),
          decoration: InputDecoration(hintText: '${el['placeholder'] ?? 'Choose…'}'),
          isExpanded: true,
        );
      case 'radio':
        return Column(
          children: options
              .map((o) => RadioListTile<String>(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    value: o,
                    groupValue: '${_values[key] ?? ''}',
                    onChanged: locked ? null : (v) => _set(key, v ?? ''),
                    title: Text(o),
                  ))
              .toList(),
        );
      case 'checkboxes':
        final chosen = (_values[key] is List ? List<String>.from(_values[key] as List) : <String>[]);
        return Column(
          children: options
              .map((o) => CheckboxListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    controlAffinity: ListTileControlAffinity.leading,
                    value: chosen.contains(o),
                    onChanged: locked
                        ? null
                        : (on) {
                            final next = List<String>.from(chosen);
                            on == true ? next.add(o) : next.remove(o);
                            _set(key, next);
                          },
                    title: Text(o),
                  ))
              .toList(),
        );
      case 'yesno':
        final v = '${_values[key] ?? ''}';
        return Row(
          children: [
            for (final option in const ['yes', 'no'])
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: ChoiceChip(
                  label: Text(option == 'yes' ? 'Yes' : 'No'),
                  selected: v == option,
                  onSelected: locked ? null : (_) => _set(key, option),
                  selectedColor: UnColors.lightBlue,
                ),
              ),
          ],
        );
      case 'table':
        return _table(el, locked);
      default:
        return TextField(
          controller: _controllerFor(key),
          enabled: !locked,
          decoration: InputDecoration(hintText: '${el['placeholder'] ?? ''}'),
          onChanged: (v) => _set(key, v),
        );
    }
  }

  /// A table on a phone: each row is a small card rather than a grid you have
  /// to scroll sideways to read.
  Widget _table(Map<String, dynamic> el, bool locked) {
    final key = '${el['key']}';
    final columns = (el['columns'] as List<dynamic>? ?? []).cast<Map<String, dynamic>>();
    final rows = (_values[key] is List ? List<Map<String, dynamic>>.from((_values[key] as List).map((r) => Map<String, dynamic>.from(r as Map))) : <Map<String, dynamic>>[]);
    if (rows.isEmpty) rows.add({for (final c in columns) '${c['key']}': ''});

    num total = 0;
    for (final r in rows) {
      for (final c in columns) {
        if (c['kind'] == 'number') total += FormRules._num(r['${c['key']}']) ?? 0;
      }
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (var i = 0; i < rows.length; i++)
          Container(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 4),
            decoration: UnStyle.card(color: UnColors.canvas),
            child: Column(
              children: [
                Row(
                  children: [
                    Text('Row ${i + 1}',
                        style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: UnColors.muted)),
                    const Spacer(),
                    if (!locked && rows.length > 1)
                      IconButton(
                        icon: const Icon(Icons.close, size: 18, color: UnColors.red),
                        tooltip: 'Remove this row',
                        onPressed: () {
                          final next = List<Map<String, dynamic>>.from(rows)..removeAt(i);
                          _set(key, next);
                        },
                      ),
                  ],
                ),
                for (final c in columns)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: TextField(
                      controller: _controllerFor('$key.$i.${c['key']}', seed: '${rows[i]['${c['key']}'] ?? ''}'),
                      enabled: !locked,
                      keyboardType: c['kind'] == 'number'
                          ? const TextInputType.numberWithOptions(decimal: true)
                          : TextInputType.text,
                      decoration: InputDecoration(
                        labelText: '${c['label']}',
                        isDense: true,
                      ),
                      onChanged: (v) {
                        final next = List<Map<String, dynamic>>.from(rows);
                        next[i] = Map<String, dynamic>.from(next[i])..['${c['key']}'] = v;
                        _set(key, next);
                      },
                    ),
                  ),
              ],
            ),
          ),
        Row(
          children: [
            if (!locked && el['allow_add'] != false)
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(minimumSize: const Size(0, 40)),
                onPressed: () {
                  final next = List<Map<String, dynamic>>.from(rows)
                    ..add({for (final c in columns) '${c['key']}': ''});
                  _set(key, next);
                },
                icon: const Icon(Icons.add, size: 18),
                label: const Text('Add row'),
              ),
            const Spacer(),
            if (el['show_total'] == true)
              Text('Total  ${total.toStringAsFixed(total % 1 == 0 ? 0 : 2)}',
                  style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
          ],
        ),
      ],
    );
  }
}
