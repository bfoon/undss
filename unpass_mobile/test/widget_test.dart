import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:unpass_mobile/core/theme.dart';
import 'package:unpass_mobile/main.dart';
import 'package:unpass_mobile/widgets/common.dart';

/// These run on every push. They deliberately avoid anything that needs a real
/// phone — no secure storage, no camera, no network — so they stay fast and
/// never fail for reasons that have nothing to do with the code.
void main() {
  Widget wrap(Widget child) => MaterialApp(theme: unpassTheme(), home: child);

  testWidgets('the sign-in screen asks for the right things', (tester) async {
    await tester.pumpWidget(wrap(SignInScreen(onSignedIn: (_) {})));

    expect(find.text('Sign in to UN PASS'), findsOneWidget);
    expect(find.widgetWithText(TextField, 'Username or email'), findsOneWidget);
    expect(find.widgetWithText(TextField, 'Password'), findsOneWidget);
    expect(find.text('Sign in securely'), findsOneWidget);

    // The site address is tucked away — most people never touch it.
    expect(find.text('Connection settings'), findsOneWidget);
    await tester.tap(find.text('Connection settings'));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(TextField, 'UN PASS address'), findsOneWidget);
  });

  testWidgets('the password can be shown and hidden', (tester) async {
    await tester.pumpWidget(wrap(SignInScreen(onSignedIn: (_) {})));

    expect(find.byIcon(Icons.visibility), findsOneWidget);
    await tester.tap(find.byIcon(Icons.visibility));
    await tester.pump();
    expect(find.byIcon(Icons.visibility_off), findsOneWidget);
  });

  testWidgets('the code screen wants six digits and offers another code', (tester) async {
    await tester.pumpWidget(
      wrap(
        const OtpScreen(
          identifier: 'awa',
          password: 'x',
          sentTo: 'aw•••@undp.org',
        ),
      ),
    );

    expect(find.text('Verify your sign-in'), findsOneWidget);
    expect(find.textContaining('aw•••@undp.org'), findsOneWidget);
    expect(find.text('Resend code'), findsOneWidget);

    // Too few digits: it says so rather than calling the server.
    await tester.enterText(find.byType(TextField), '123');
    await tester.tap(find.text('Verify and sign in'));
    await tester.pump();
    expect(find.textContaining('six-digit'), findsOneWidget);
  });

  testWidgets('a status pill takes its colour from the status', (tester) async {
    await tester.pumpWidget(
      wrap(
        const Scaffold(
          body: Column(
            children: [
              StatusPill('Completed'),
              StatusPill('Rejected'),
            ],
          ),
        ),
      ),
    );

    expect(find.text('Completed'), findsOneWidget);
    expect(find.text('Rejected'), findsOneWidget);
  });

  test('statuses map to the same colours as the website', () {
    expect(statusColor('completed'), UnColors.green);
    expect(statusColor('rejected'), UnColors.red);
    expect(statusColor('assigned'), UnColors.amber);
    expect(statusColor('anything else'), UnColors.muted);
  });

  test('the palette matches the UN PASS stylesheet', () {
    expect(UnColors.blue, const Color(0xFF009EDB));
    expect(UnColors.darkBlue, const Color(0xFF005A8B));
  });
}
