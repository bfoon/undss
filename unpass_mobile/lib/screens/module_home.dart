import 'package:flutter/material.dart';

import '../core/api.dart';
import '../core/theme.dart';
import '../widgets/common.dart';
import 'asset_result.dart';
import 'home.dart';
import 'scanner.dart';
import 'work.dart';

/// The first screen: a greeting, then one tile per module this person's office
/// has switched on.
///
/// The tiles come from the server, not from a list baked into the app, so
/// nobody is offered a module they cannot open — and when an office turns a
/// feature on, it appears here without a new build.
class ModuleHome extends StatelessWidget {
  const ModuleHome({super.key, required this.user, required this.onOpenModule});

  final Map<String, dynamic> user;
  final void Function(String key) onOpenModule;

  static const _icons = {
    'draw': Icons.draw_outlined,
    'edit_note': Icons.edit_note_outlined,
    'account_tree': Icons.account_tree_outlined,
    'inventory': Icons.inventory_2_outlined,
    'badge': Icons.badge_outlined,
    'event': Icons.event_outlined,
    'people': Icons.people_outline,
    'package': Icons.local_shipping_outlined,
    'warning': Icons.warning_amber_outlined,
  };

  String get _greeting {
    final hour = DateTime.now().hour;
    if (hour < 12) return 'Good morning,';
    if (hour < 17) return 'Good afternoon,';
    return 'Good evening,';
  }

  @override
  Widget build(BuildContext context) {
    final modules = (user['modules'] as List<dynamic>? ?? []);
    return ListView(
      padding: const EdgeInsets.fromLTRB(UnStyle.gap, 8, UnStyle.gap, 28),
      children: [
        _Greeting(greeting: _greeting, user: user),
        const SizedBox(height: 20),
        if (modules.isEmpty)
          const EmptyState(
            icon: Icons.apps_outlined,
            title: 'No modules yet',
            detail: 'Your office has not switched any modules on for you. '
                'ICT can enable them, and they will appear here.',
          )
        else
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 12,
            crossAxisSpacing: 12,
            childAspectRatio: 0.92,
            children: [
              for (final m in modules)
                _ModuleTile(
                  title: '${(m as Map)['title']}',
                  subtitle: '${m['subtitle']}',
                  icon: _icons['${m['icon']}'] ?? Icons.widgets_outlined,
                  inApp: m['in_app'] == true,
                  onTap: () => m['in_app'] == true
                      ? onOpenModule('${m['key']}')
                      : _explainWebOnly(context, '${m['title']}'),
                ),
            ],
          ),
        const SizedBox(height: 20),
        _QuickRow(onOpenModule: onOpenModule, modules: modules),
      ],
    );
  }

  void _explainWebOnly(BuildContext context, String title) {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.laptop_mac, size: 40, color: UnColors.blue),
              const SizedBox(height: 14),
              Text('$title is on the website',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 8),
              const Text(
                'Your office has this module, but it is not in the app yet. '
                'Open UN PASS in a browser to use it.',
                textAlign: TextAlign.center,
                style: TextStyle(color: UnColors.muted, height: 1.4),
              ),
              const SizedBox(height: 18),
              FilledButton(
                  onPressed: () => Navigator.pop(sheetContext), child: const Text('Close')),
            ],
          ),
        ),
      ),
    );
  }
}

class _Greeting extends StatelessWidget {
  const _Greeting({required this.greeting, required this.user});
  final String greeting;
  final Map<String, dynamic> user;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [UnColors.darkBlue, Color(0xFF0C6FA0)],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        borderRadius: BorderRadius.circular(UnStyle.radius),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(greeting, style: const TextStyle(color: Color(0xFFCFE6F5), fontSize: 14)),
                const SizedBox(height: 2),
                Text('${user['name']}',
                    style: const TextStyle(
                        color: Colors.white, fontSize: 21, fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Text(
                  [user['office'], user['agency']]
                      .where((x) => '${x ?? ''}'.isNotEmpty)
                      .join(' · '),
                  style: const TextStyle(color: Color(0xFFB9DCF0), fontSize: 12.5),
                ),
              ],
            ),
          ),
          CircleAvatar(
            radius: 24,
            backgroundColor: Colors.white24,
            child: Text('${user['initials'] ?? '?'}',
                style: const TextStyle(
                    color: Colors.white, fontWeight: FontWeight.w700, fontSize: 16)),
          ),
        ],
      ),
    );
  }
}

class _ModuleTile extends StatelessWidget {
  const _ModuleTile({
    required this.title,
    required this.subtitle,
    required this.icon,
    required this.onTap,
    required this.inApp,
  });

  final String title;
  final String subtitle;
  final IconData icon;
  final VoidCallback onTap;
  final bool inApp;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(UnStyle.radius),
      onTap: onTap,
      child: Opacity(
        opacity: inApp ? 1 : 0.72,
        child: Container(
          padding: const EdgeInsets.all(14),
          decoration: UnStyle.card(),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Container(
                    height: 44,
                    width: 44,
                    decoration: BoxDecoration(
                        color: UnColors.lightBlue, borderRadius: BorderRadius.circular(12)),
                    child: Icon(icon, color: UnColors.darkBlue, size: 24),
                  ),
                  const Spacer(),
                  if (!inApp)
                    const Tooltip(
                      message: 'Available on the website',
                      child: Icon(Icons.laptop_mac, size: 15, color: UnColors.muted),
                    ),
                ],
              ),
              const Spacer(),
              Text(title,
                  style: const TextStyle(
                      fontSize: 15.5, fontWeight: FontWeight.w700, color: UnColors.navy)),
              const SizedBox(height: 4),
              Text(subtitle,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 12, color: UnColors.muted, height: 1.25)),
            ],
          ),
        ),
      ),
    );
  }
}

/// Two things people do constantly, kept one tap from the home screen.
class _QuickRow extends StatelessWidget {
  const _QuickRow({required this.onOpenModule, required this.modules});
  final void Function(String key) onOpenModule;
  final List<dynamic> modules;

  bool _has(String key) => modules.any((m) => (m as Map)['key'] == key && m['in_app'] == true);

  @override
  Widget build(BuildContext context) {
    final tiles = <Widget>[];
    if (_has('assets')) {
      tiles.add(Expanded(
        child: OutlinedButton.icon(
          onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const ScannerScreen()),
          ),
          icon: const Icon(Icons.qr_code_scanner),
          label: const Text('Scan'),
        ),
      ));
    }
    if (_has('assets')) {
      if (tiles.isNotEmpty) tiles.add(const SizedBox(width: 10));
      tiles.add(Expanded(
        child: OutlinedButton.icon(
          onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(builder: (_) => const AssetSearchScreen()),
          ),
          icon: const Icon(Icons.search),
          label: const Text('Find asset'),
        ),
      ));
    }
    if (tiles.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text('Quick actions',
            style: TextStyle(
                fontSize: 12, fontWeight: FontWeight.w700, color: UnColors.muted, letterSpacing: 0.6)),
        const SizedBox(height: 10),
        Row(children: tiles),
      ],
    );
  }
}

/// Opened from a tile: the module's own screen, with a title bar to come back from.
class ModuleScreen extends StatelessWidget {
  const ModuleScreen({super.key, required this.moduleKey, required this.title});
  final String moduleKey;
  final String title;

  @override
  Widget build(BuildContext context) {
    final Widget body;
    switch (moduleKey) {
      case 'esign':
        body = const EnvelopesTab();
        break;
      case 'forms':
        body = const FormsTab();
        break;
      case 'flows':
        body = const FlowsTab();
        break;
      case 'assets':
        body = const MyAssetsTab();
        break;
      default:
        body = const EmptyState(
          icon: Icons.laptop_mac,
          title: 'On the website',
          detail: 'This module is not in the app yet. Open UN PASS in a browser to use it.',
        );
    }
    return Scaffold(appBar: AppBar(title: Text(title)), body: body);
  }
}
