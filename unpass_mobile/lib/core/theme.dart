import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// The UN PASS palette, taken from the website's stylesheet so the app and the
/// browser look like one product.
class UnColors {
  static const blue = Color(0xFF009EDB); // --un-blue
  static const darkBlue = Color(0xFF005A8B); // --un-dark-blue
  static const lightBlue = Color(0xFFE8F4FD); // --un-light-blue
  static const navy = Color(0xFF0B2A45); // headings and the sign-in screen
  static const ink = Color(0xFF1E293B);
  static const muted = Color(0xFF64748B);
  static const line = Color(0xFFDCE3EA);
  static const canvas = Color(0xFFF6F8FA);

  /// Status colours, matching the badges on the website.
  static const amber = Color(0xFFD97706);
  static const green = Color(0xFF15803D);
  static const red = Color(0xFFB91C1C);
  static const purple = Color(0xFF7C3AED);
}

/// One place for the shapes and spacing, so screens stay consistent.
class UnStyle {
  static const radius = 14.0;
  static const gap = 16.0;

  static BoxDecoration card({Color? color, Color? border}) => BoxDecoration(
        color: color ?? Colors.white,
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(color: border ?? UnColors.line),
        boxShadow: const [
          BoxShadow(color: Color(0x0F0B2A45), blurRadius: 10, offset: Offset(0, 2)),
        ],
      );

  /// The dark blue wash used behind the sign-in screen and the scanner.
  static const deepGradient = LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [UnColors.navy, Color(0xFF123A5C)],
  );
}

ThemeData unpassTheme() {
  final base = ThemeData(useMaterial3: true, brightness: Brightness.light);
  final scheme = ColorScheme.fromSeed(
    seedColor: UnColors.blue,
    primary: UnColors.darkBlue,
    secondary: UnColors.blue,
    surface: Colors.white,
  );

  return base.copyWith(
    colorScheme: scheme,
    scaffoldBackgroundColor: UnColors.canvas,
    appBarTheme: const AppBarTheme(
      backgroundColor: UnColors.darkBlue,
      foregroundColor: Colors.white,
      elevation: 0,
      centerTitle: false,
      systemOverlayStyle: SystemUiOverlayStyle.light,
      titleTextStyle: TextStyle(fontSize: 19, fontWeight: FontWeight.w600, color: Colors.white),
    ),
    textTheme: base.textTheme.apply(bodyColor: UnColors.ink, displayColor: UnColors.navy),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: UnColors.darkBlue,
        foregroundColor: Colors.white,
        minimumSize: const Size.fromHeight(52), // a comfortable target on a phone
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: UnColors.darkBlue,
        minimumSize: const Size.fromHeight(52),
        side: const BorderSide(color: UnColors.line),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Colors.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 16),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: UnColors.line),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: UnColors.line),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: UnColors.blue, width: 2),
      ),
    ),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: UnColors.navy,
      contentTextStyle: const TextStyle(color: Colors.white),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
    ),
    dividerTheme: const DividerThemeData(color: UnColors.line, space: 1, thickness: 1),
  );
}

/// The colour the website gives each status, so a badge means the same thing
/// in both places.
Color statusColor(String status) {
  switch (status.toLowerCase()) {
    case 'completed':
    case 'signed':
    case 'approved':
    case 'available':
      return UnColors.green;
    case 'rejected':
    case 'declined':
    case 'retired':
      return UnColors.red;
    case 'assigned':
    case 'in_progress':
    case 'sent':
    case 'pending':
    case 'maintenance':
      return UnColors.amber;
    default:
      return UnColors.muted;
  }
}
