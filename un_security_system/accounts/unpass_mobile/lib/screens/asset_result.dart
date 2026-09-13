import 'dart:async';

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';

/// What you see straight after a scan: a plain verdict at the top, then the
/// detail. The verdict is the point — someone standing in a store room wants to
/// know "is this the right thing, and is it where it should be?" at a glance.
class AssetResultScreen extends StatelessWidget {
  const AssetResultScreen({super.key, required this.asset, this.heldByMe = false, this.hint});
  final Map<String, dynamic> asset;
  final bool heldByMe;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    final status = '${asset['status']}';
    final colour = statusColor(status);
    final holder = '${asset['holder'] ?? ''}';

    return Scaffold(
      appBar: AppBar(title: const Text('Asset')),
      body: ListView(
        padding: const EdgeInsets.all(UnStyle.gap),
        children: [
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(18),
            decoration: BoxDecoration(
              color: colour.withOpacity(0.10),
              borderRadius: BorderRadius.circular(UnStyle.radius),
              border: Border.all(color: colour.withOpacity(0.35)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      height: 46, width: 46,
                      decoration: BoxDecoration(color: colour, borderRadius: BorderRadius.circular(13)),
                      child: Icon(_iconFor(status), color: Colors.white, size: 26),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('${asset['name']}',
                              style: const TextStyle(
                                  fontSize: 19, fontWeight: FontWeight.w700, color: UnColors.navy)),
                          const SizedBox(height: 4),
                          StatusPill('${asset['status_label']}', color: colour),
                        ],
                      ),
                    ),
                  ],
                ),
                if (heldByMe || (hint ?? '').isNotEmpty) ...[
                  const SizedBox(height: 14),
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(heldByMe ? Icons.person_pin_circle : Icons.info_outline, size: 18, color: colour),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(heldByMe ? 'This one is assigned to you.' : '$hint',
                            style: TextStyle(color: colour, fontWeight: FontWeight.w600)),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: UnStyle.gap),
          SectionCard(
            title: 'IDENTIFICATION',
            icon: Icons.qr_code_2,
            child: Column(
              children: [
                DetailRow('Asset tag', '${asset['tag'] ?? ''}', strong: true),
                DetailRow('Serial number', '${asset['serial'] ?? ''}'),
                DetailRow('Category', '${asset['category'] ?? ''}'),
              ],
            ),
          ),
          SectionCard(
            title: 'WHERE IT BELONGS',
            icon: Icons.place_outlined,
            child: Column(
              children: [
                DetailRow('Held by', holder.isEmpty ? 'Nobody' : holder, strong: holder.isNotEmpty),
                if ('${asset['holder_email'] ?? ''}'.isNotEmpty)
                  DetailRow('Contact', '${asset['holder_email']}'),
                DetailRow('Unit', '${asset['unit'] ?? ''}'),
                DetailRow('Agency', '${asset['agency'] ?? ''}'),
              ],
            ),
          ),
          if (asset['acquired'] != null || asset['retired'] != null)
            SectionCard(
              title: 'DATES',
              icon: Icons.event_outlined,
              child: Column(
                children: [
                  DetailRow('Acquired', '${asset['acquired'] ?? ''}'),
                  if (asset['retired'] != null) DetailRow('Retired', '${asset['retired']}'),
                ],
              ),
            ),
          OutlinedButton.icon(
            onPressed: () async {
              final url = Uri.parse(Api.instance.webUrl('${asset['detail_url']}'));
              if (!await launchUrl(url, mode: LaunchMode.externalApplication)) {
                if (context.mounted) showNote(context, "Couldn't open the asset page.", error: true);
              }
            },
            icon: const Icon(Icons.open_in_new),
            label: const Text('Open the full record'),
          ),
          const SizedBox(height: 10),
          FilledButton.icon(
            onPressed: () => Navigator.of(context).pop(),
            icon: const Icon(Icons.qr_code_scanner),
            label: const Text('Scan the next one'),
          ),
        ],
      ),
    );
  }

  IconData _iconFor(String status) {
    switch (status) {
      case 'available':
        return Icons.check_circle_outline;
      case 'assigned':
        return Icons.person_outline;
      case 'maintenance':
        return Icons.build_outlined;
      case 'retired':
        return Icons.do_not_disturb_on_outlined;
      default:
        return Icons.inventory_2_outlined;
    }
  }
}

/// The way out when a label is damaged: type a tag, serial or name.
class AssetSearchScreen extends StatefulWidget {
  const AssetSearchScreen({super.key});

  @override
  State<AssetSearchScreen> createState() => _AssetSearchScreenState();
}

class _AssetSearchScreenState extends State<AssetSearchScreen> {
  final _query = TextEditingController();
  Timer? _debounce;
  List<dynamic> _results = [];
  bool _busy = false;
  String? _error;
  bool _searched = false;

  @override
  void dispose() {
    _debounce?.cancel();
    _query.dispose();
    super.dispose();
  }

  void _onChanged(String value) {
    _debounce?.cancel();
    if (value.trim().length < 2) {
      setState(() {
        _results = [];
        _searched = false;
      });
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 350), () => _search(value.trim()));
  }

  Future<void> _search(String q) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final rows = await Api.instance.searchAssets(q);
      setState(() {
        _results = rows;
        _searched = true;
      });
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Find an asset')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(UnStyle.gap),
            child: TextField(
              controller: _query,
              autofocus: true,
              textInputAction: TextInputAction.search,
              onChanged: _onChanged,
              onSubmitted: (v) => v.trim().length >= 2 ? _search(v.trim()) : null,
              decoration: InputDecoration(
                hintText: 'Asset tag, serial number or name',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: _busy
                    ? const Padding(
                        padding: EdgeInsets.all(14),
                        child: SizedBox(
                            height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2)))
                    : null,
              ),
            ),
          ),
          if (_error != null)
            Padding(padding: const EdgeInsets.symmetric(horizontal: UnStyle.gap), child: ErrorNote(_error!)),
          Expanded(
            child: _results.isEmpty
                ? EmptyState(
                    icon: _searched ? Icons.search_off : Icons.search,
                    title: _searched ? 'Nothing found' : 'Search your agency',
                    detail: _searched
                        ? 'No asset matches that. Try part of the tag, or the equipment name.'
                        : 'Type at least two characters. Useful when a QR label is torn or faded.',
                  )
                : ListView.separated(
                    padding: const EdgeInsets.all(UnStyle.gap),
                    itemCount: _results.length,
                    separatorBuilder: (_, __) => const SizedBox(height: 10),
                    itemBuilder: (context, i) {
                      final a = _results[i] as Map<String, dynamic>;
                      return InkWell(
                        borderRadius: BorderRadius.circular(UnStyle.radius),
                        onTap: () => Navigator.of(context).push(
                          MaterialPageRoute(builder: (_) => AssetResultScreen(asset: a)),
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
                                    Text('${a['name']}',
                                        style: const TextStyle(
                                            fontWeight: FontWeight.w700, color: UnColors.navy)),
                                    const SizedBox(height: 3),
                                    Text(
                                      [a['tag'], a['serial'], a['holder']]
                                          .where((x) => '${x ?? ''}'.isNotEmpty)
                                          .join(' · '),
                                      style: const TextStyle(fontSize: 12.5, color: UnColors.muted),
                                    ),
                                  ],
                                ),
                              ),
                              StatusPill('${a['status_label']}'),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}
