import 'package:flutter/material.dart';

import '../core/theme.dart';

/// A short message under a field or button. Red, but not shouting.
class ErrorNote extends StatelessWidget {
  const ErrorNote(this.message, {super.key});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: const Color(0xFFFDECEC),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.error_outline, size: 18, color: UnColors.red),
          const SizedBox(width: 8),
          Expanded(child: Text(message, style: const TextStyle(color: UnColors.red, fontSize: 13.5))),
        ],
      ),
    );
  }
}

void showNote(BuildContext context, String message, {bool error = false}) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(message),
      backgroundColor: error ? UnColors.red : UnColors.navy,
      duration: Duration(seconds: error ? 5 : 3),
    ));
}

/// The small coloured pill used for statuses, matching the website's badges.
class StatusPill extends StatelessWidget {
  const StatusPill(this.label, {super.key, this.color});
  final String label;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final c = color ?? statusColor(label);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(color: c.withOpacity(0.12), borderRadius: BorderRadius.circular(999)),
      child: Text(label,
          style: TextStyle(color: c, fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 0.2)),
    );
  }
}

/// Shown when a list has nothing in it — never a blank screen.
class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.icon, required this.title, required this.detail, this.action});
  final IconData icon;
  final String title;
  final String detail;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              height: 72, width: 72,
              decoration: BoxDecoration(color: UnColors.lightBlue, borderRadius: BorderRadius.circular(22)),
              child: Icon(icon, size: 34, color: UnColors.darkBlue),
            ),
            const SizedBox(height: 18),
            Text(title,
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700, color: UnColors.navy)),
            const SizedBox(height: 6),
            Text(detail, textAlign: TextAlign.center, style: const TextStyle(color: UnColors.muted)),
            if (action != null) ...[const SizedBox(height: 20), action!],
          ],
        ),
      ),
    );
  }
}

/// A row of label and value, as used on the task and asset pages.
class DetailRow extends StatelessWidget {
  const DetailRow(this.label, this.value, {super.key, this.strong = false});
  final String label;
  final String value;
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 118,
            child: Text(label, style: const TextStyle(color: UnColors.muted, fontSize: 13.5)),
          ),
          Expanded(
            child: Text(
              value.isEmpty ? '—' : value,
              style: TextStyle(
                fontSize: 14.5,
                fontWeight: strong ? FontWeight.w700 : FontWeight.w500,
                color: value.isEmpty ? UnColors.muted : UnColors.ink,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// A white card with a heading, used down the detail screens.
class SectionCard extends StatelessWidget {
  const SectionCard({super.key, required this.title, required this.child, this.icon, this.trailing});
  final String title;
  final Widget child;
  final IconData? icon;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: UnStyle.gap),
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 16),
      decoration: UnStyle.card(),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              if (icon != null) ...[Icon(icon, size: 18, color: UnColors.darkBlue), const SizedBox(width: 8)],
              Expanded(
                child: Text(title,
                    style: const TextStyle(
                        fontSize: 12.5, fontWeight: FontWeight.w700, color: UnColors.muted, letterSpacing: 0.6)),
              ),
              if (trailing != null) trailing!,
            ],
          ),
          const SizedBox(height: 10),
          child,
        ],
      ),
    );
  }
}

/// Full-screen spinner with a line of text, so a wait is never silent.
class Loading extends StatelessWidget {
  const Loading({super.key, this.message = 'Loading…'});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const CircularProgressIndicator(color: UnColors.blue),
          const SizedBox(height: 14),
          Text(message, style: const TextStyle(color: UnColors.muted)),
        ],
      ),
    );
  }
}

/// What we show when a call fails: the reason, and a way to try again.
class FailureState extends StatelessWidget {
  const FailureState({super.key, required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, size: 44, color: UnColors.muted),
            const SizedBox(height: 14),
            Text(message, textAlign: TextAlign.center, style: const TextStyle(color: UnColors.ink)),
            const SizedBox(height: 18),
            OutlinedButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Try again'),
            ),
          ],
        ),
      ),
    );
  }
}
