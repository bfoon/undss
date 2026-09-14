import 'package:flutter/material.dart';

import '../core/api.dart';
import '../core/theme.dart';
import 'common.dart';

/// Asks the server what it is and shows the answer in plain words. When
/// something is wrong this is the fastest way to tell whether the site is
/// unreachable, the API isn't deployed, or the session has lapsed.
Future<void> showConnectionCheck(BuildContext context) async {
  showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    builder: (sheetContext) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        child: FutureBuilder<Map<String, String>>(
          future: Api.instance.checkConnection(),
          builder: (context, snap) {
            if (snap.connectionState == ConnectionState.waiting) {
              return const Padding(
                padding: EdgeInsets.symmetric(vertical: 40),
                child: Loading(message: 'Checking the connection…'),
              );
            }
            final r = snap.data ?? {'result': 'Check failed', 'detail': '', 'ok': 'no'};
            final good = r['ok'] == 'yes';
            return Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Icon(good ? Icons.check_circle_outline : Icons.error_outline,
                    size: 42, color: good ? UnColors.green : UnColors.amber),
                const SizedBox(height: 12),
                Text('${r['result']}',
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w700, color: UnColors.navy)),
                const SizedBox(height: 10),
                Text('${r['detail']}',
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: UnColors.muted, height: 1.4)),
                if (Api.instance.lastDiagnosis.isNotEmpty) ...[
                  const SizedBox(height: 14),
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                        color: UnColors.canvas, borderRadius: BorderRadius.circular(10)),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Last failure', style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700, color: UnColors.muted)),
                        const SizedBox(height: 4),
                        Text(Api.instance.lastDiagnosis,
                            style: const TextStyle(fontFamily: 'monospace', fontSize: 12, color: UnColors.ink)),
                      ],
                    ),
                  ),
                ],
                const SizedBox(height: 18),
                FilledButton(onPressed: () => Navigator.pop(sheetContext), child: const Text('Close')),
              ],
            );
          },
        ),
      ),
    ),
  );
}
