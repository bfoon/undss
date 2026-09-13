import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'asset_result.dart';
import 'scanner.dart';
import 'task_detail.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.user, required this.onSignedOut});
  final Map<String, dynamic> user;
  final VoidCallback onSignedOut;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _tab = 0;
  final _inboxKey = GlobalKey<_InboxTabState>();

  @override
  Widget build(BuildContext context) {
    final pages = [
      InboxTab(key: _inboxKey, user: widget.user),
      const _ScanTab(),
      const MyAssetsTab(),
    ];
    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('UN PASS'),
            Text(
              widget.user['office']?.toString().isNotEmpty == true
                  ? '${widget.user['name']} · ${widget.user['office']}'
                  : '${widget.user['name']}',
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w400, color: Color(0xFFCFE6F5)),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Account',
            icon: CircleAvatar(
              radius: 15,
              backgroundColor: UnColors.blue,
              child: Text('${widget.user['initials'] ?? '?'}',
                  style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: Colors.white)),
            ),
            onPressed: () => _accountSheet(context),
          ),
          const SizedBox(width: 6),
        ],
      ),
      body: IndexedStack(index: _tab, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) {
          setState(() => _tab = i);
          if (i == 0) _inboxKey.currentState?.refresh();
        },
        backgroundColor: Colors.white,
        indicatorColor: UnColors.lightBlue,
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.inbox_outlined), selectedIcon: Icon(Icons.inbox), label: 'Waiting on me'),
          NavigationDestination(
              icon: Icon(Icons.qr_code_scanner), selectedIcon: Icon(Icons.qr_code_scanner), label: 'Scan'),
          NavigationDestination(
              icon: Icon(Icons.devices_other_outlined), selectedIcon: Icon(Icons.devices_other), label: 'My assets'),
        ],
      ),
    );
  }

  void _accountSheet(BuildContext context) {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: CircleAvatar(
                backgroundColor: UnColors.darkBlue,
                child: Text('${widget.user['initials'] ?? '?'}',
                    style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700)),
              ),
              title: Text('${widget.user['name']}',
                  style: const TextStyle(fontWeight: FontWeight.w700)),
              subtitle: Text([widget.user['email'], widget.user['agency']]
                  .where((x) => (x?.toString() ?? '').isNotEmpty)
                  .join(' · ')),
            ),
            const Divider(),
            ListTile(
              leading: const Icon(Icons.smartphone, color: UnColors.muted),
              title: const Text('This phone'),
              subtitle: Text('${Api.instance.deviceName}\nRemembered for 30 days after verifying'),
              isThreeLine: true,
            ),
            ListTile(
              leading: const Icon(Icons.public, color: UnColors.muted),
              title: const Text('Site'),
              subtitle: Text(Api.instance.baseUrl),
            ),
            const Divider(),
            ListTile(
              leading: const Icon(Icons.logout, color: UnColors.darkBlue),
              title: const Text('Sign out'),
              subtitle: const Text('Keeps this phone remembered'),
              onTap: () async {
                Navigator.pop(sheetContext);
                await Api.instance.logout();
                widget.onSignedOut();
              },
            ),
            ListTile(
              leading: const Icon(Icons.person_off_outlined, color: UnColors.red),
              title: const Text('Sign out and forget this phone',
                  style: TextStyle(color: UnColors.red)),
              subtitle: const Text('Ask for a code next time — use this if handing the phone on'),
              onTap: () async {
                Navigator.pop(sheetContext);
                await Api.instance.logout(forgetDevice: true);
                widget.onSignedOut();
              },
            ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Waiting on me
// ─────────────────────────────────────────────────────────────────────────────

class InboxTab extends StatefulWidget {
  const InboxTab({super.key, required this.user});
  final Map<String, dynamic> user;

  @override
  State<InboxTab> createState() => _InboxTabState();
}

class _InboxTabState extends State<InboxTab> {
  late Future<List<dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _future = Api.instance.inbox();
  }

  void refresh() => setState(() => _future = Api.instance.inbox());

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async => refresh(),
      child: FutureBuilder<List<dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading(message: 'Fetching what needs you…');
          }
          if (snap.hasError) {
            return ListView(children: [
              SizedBox(
                height: MediaQuery.of(context).size.height * 0.6,
                child: FailureState(message: '${snap.error}', onRetry: refresh),
              )
            ]);
          }
          final items = snap.data ?? [];
          if (items.isEmpty) {
            return ListView(children: [
              SizedBox(
                height: MediaQuery.of(context).size.height * 0.65,
                child: const EmptyState(
                  icon: Icons.task_alt,
                  title: 'Nothing waiting on you',
                  detail: 'Approvals and signatures will appear here as soon as they are sent to you.',
                ),
              )
            ]);
          }
          return ListView.separated(
            padding: const EdgeInsets.all(UnStyle.gap),
            itemCount: items.length,
            separatorBuilder: (_, __) => const SizedBox(height: 10),
            itemBuilder: (context, i) => _InboxCard(item: items[i] as Map<String, dynamic>, onDone: refresh),
          );
        },
      ),
    );
  }
}

class _InboxCard extends StatelessWidget {
  const _InboxCard({required this.item, required this.onDone});
  final Map<String, dynamic> item;
  final VoidCallback onDone;

  @override
  Widget build(BuildContext context) {
    final isEnvelope = item['kind'] == 'envelope';
    final due = item['due'] as String?;
    return InkWell(
      borderRadius: BorderRadius.circular(UnStyle.radius),
      onTap: () async {
        if (isEnvelope) {
          // Signing needs the full page — hand it to the browser, still signed in.
          final url = Uri.parse(Api.instance.webUrl(item['url'] as String));
          if (!await launchUrl(url, mode: LaunchMode.externalApplication)) {
            if (context.mounted) showNote(context, "Couldn't open the signing page.", error: true);
          }
          return;
        }
        final changed = await Navigator.of(context).push<bool>(
          MaterialPageRoute(builder: (_) => TaskDetailScreen(token: item['id'] as String)),
        );
        if (changed == true) onDone();
      },
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: UnStyle.card(),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              height: 44, width: 44,
              decoration: BoxDecoration(
                color: (isEnvelope ? UnColors.blue : UnColors.amber).withOpacity(0.12),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Icon(isEnvelope ? Icons.draw_outlined : Icons.verified_outlined,
                  color: isEnvelope ? UnColors.blue : UnColors.amber),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('${item['title']}',
                      style: const TextStyle(fontSize: 15.5, fontWeight: FontWeight.w700, color: UnColors.navy)),
                  const SizedBox(height: 3),
                  Text('${item['subtitle']}', style: const TextStyle(color: UnColors.muted, fontSize: 13)),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 6,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Text('${item['reference']}',
                          style: const TextStyle(
                              fontFamily: 'monospace', fontSize: 11.5, color: UnColors.muted)),
                      if ((item['started_by'] as String?)?.isNotEmpty == true)
                        Text('from ${item['started_by']}',
                            style: const TextStyle(fontSize: 11.5, color: UnColors.muted)),
                      if (due != null) StatusPill('Due $due', color: UnColors.amber),
                    ],
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_right, color: UnColors.muted),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Scan
// ─────────────────────────────────────────────────────────────────────────────

class _ScanTab extends StatelessWidget {
  const _ScanTab();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            height: 96, width: 96,
            decoration: BoxDecoration(color: UnColors.lightBlue, borderRadius: BorderRadius.circular(28)),
            child: const Icon(Icons.qr_code_scanner, size: 48, color: UnColors.darkBlue),
          ),
          const SizedBox(height: 22),
          const Text('Verify an asset',
              style: TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: UnColors.navy)),
          const SizedBox(height: 8),
          const Text(
            'Point the camera at the QR label on the equipment. You will see what it is, '
            'who holds it and whether it is still in service.',
            textAlign: TextAlign.center,
            style: TextStyle(color: UnColors.muted, height: 1.4),
          ),
          const SizedBox(height: 26),
          FilledButton.icon(
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const ScannerScreen()),
            ),
            icon: const Icon(Icons.camera_alt_outlined),
            label: const Text('Open the scanner'),
          ),
          const SizedBox(height: 10),
          OutlinedButton.icon(
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const AssetSearchScreen()),
            ),
            icon: const Icon(Icons.search),
            label: const Text('Search instead'),
          ),
          const SizedBox(height: 14),
          const Text('Use search when a label is torn, faded or missing.',
              style: TextStyle(fontSize: 12, color: UnColors.muted)),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// My assets
// ─────────────────────────────────────────────────────────────────────────────

class MyAssetsTab extends StatefulWidget {
  const MyAssetsTab({super.key});
  @override
  State<MyAssetsTab> createState() => _MyAssetsTabState();
}

class _MyAssetsTabState extends State<MyAssetsTab> {
  late Future<List<dynamic>> _future = Api.instance.myAssets();

  void _refresh() => setState(() => _future = Api.instance.myAssets());

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async => _refresh(),
      child: FutureBuilder<List<dynamic>>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState == ConnectionState.waiting) {
            return const Loading(message: 'Fetching your equipment…');
          }
          if (snap.hasError) {
            return ListView(children: [
              SizedBox(height: 400, child: FailureState(message: '${snap.error}', onRetry: _refresh))
            ]);
          }
          final assets = snap.data ?? [];
          if (assets.isEmpty) {
            return ListView(children: const [
              SizedBox(
                height: 420,
                child: EmptyState(
                  icon: Icons.devices_other,
                  title: 'Nothing assigned to you',
                  detail: 'Equipment issued to you will be listed here, ready to check off during a count.',
                ),
              )
            ]);
          }
          return ListView.separated(
            padding: const EdgeInsets.all(UnStyle.gap),
            itemCount: assets.length + 1,
            separatorBuilder: (_, __) => const SizedBox(height: 10),
            itemBuilder: (context, i) {
              if (i == 0) {
                return Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Text('${assets.length} item${assets.length == 1 ? '' : 's'} assigned to you',
                      style: const TextStyle(color: UnColors.muted)),
                );
              }
              return AssetTile(asset: assets[i - 1] as Map<String, dynamic>);
            },
          );
        },
      ),
    );
  }
}

class AssetTile extends StatelessWidget {
  const AssetTile({super.key, required this.asset});
  final Map<String, dynamic> asset;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(UnStyle.radius),
      onTap: () => Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => AssetResultScreen(asset: asset)),
      ),
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: UnStyle.card(),
        child: Row(
          children: [
            Container(
              height: 42, width: 42,
              decoration: BoxDecoration(
                  color: statusColor('${asset['status']}').withOpacity(0.12),
                  borderRadius: BorderRadius.circular(11)),
              child: Icon(Icons.inventory_2_outlined, color: statusColor('${asset['status']}')),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('${asset['name']}',
                      style: const TextStyle(fontWeight: FontWeight.w700, color: UnColors.navy)),
                  const SizedBox(height: 2),
                  Text('${asset['tag']}'.isEmpty ? '${asset['serial']}' : '${asset['tag']}',
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.muted)),
                ],
              ),
            ),
            StatusPill('${asset['status_label']}'),
          ],
        ),
      ),
    );
  }
}
