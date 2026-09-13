import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../core/api.dart';
import '../core/theme.dart';
import 'asset_result.dart';

/// The camera view for reading an asset label.
///
/// Two things matter on a store-room floor: it must not fire twice on the same
/// label, and it must say something useful when a code isn't recognised rather
/// than just failing to react.
class ScannerScreen extends StatefulWidget {
  const ScannerScreen({super.key});

  @override
  State<ScannerScreen> createState() => _ScannerScreenState();
}

class _ScannerScreenState extends State<ScannerScreen> with WidgetsBindingObserver {
  final _controller = MobileScannerController(
    detectionSpeed: DetectionSpeed.noDuplicates,
    formats: const [BarcodeFormat.qrCode, BarcodeFormat.code128, BarcodeFormat.dataMatrix],
  );
  bool _looking = false;
  bool _torch = false;
  String? _lastCode;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _controller.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // Release the camera when the app goes to the background.
    if (state == AppLifecycleState.resumed) {
      _controller.start();
    } else if (state == AppLifecycleState.paused) {
      _controller.stop();
    }
  }

  Future<void> _onDetect(BarcodeCapture capture) async {
    if (_looking) return;
    final raw = capture.barcodes.isEmpty ? null : capture.barcodes.first.rawValue;
    if (raw == null || raw.isEmpty || raw == _lastCode) return;

    setState(() {
      _looking = true;
      _lastCode = raw;
    });
    await _controller.stop();

    try {
      final data = await Api.instance.lookupAsset(raw);
      if (!mounted) return;
      await Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => AssetResultScreen(
          asset: data['asset'] as Map<String, dynamic>,
          heldByMe: data['held_by_me'] == true,
          hint: data['hint'] as String?,
        ),
      ));
    } on ApiException catch (e) {
      if (!mounted) return;
      await _showUnknown(e.message, raw);
    } finally {
      if (mounted) {
        setState(() {
          _looking = false;
          _lastCode = null; // allow the same label to be scanned again
        });
        await _controller.start();
      }
    }
  }

  Future<void> _showUnknown(String message, String raw) {
    return showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(Icons.help_outline, size: 40, color: UnColors.amber),
              const SizedBox(height: 12),
              const Text(
                'Not recognised',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w700,
                  color: UnColors.navy,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                message,
                textAlign: TextAlign.center,
                style: const TextStyle(color: UnColors.muted),
              ),
              const SizedBox(height: 14),
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: UnColors.canvas,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  raw.length > 160 ? '${raw.substring(0, 160)}…' : raw,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 12,
                    color: UnColors.ink,
                  ),
                ),
              ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: () {
                  Navigator.pop(sheetContext);
                  Navigator.of(context).push(
                    MaterialPageRoute(builder: (_) => const AssetSearchScreen()),
                  );
                },
                icon: const Icon(Icons.search),
                label: const Text('Search by tag or name'),
              ),
              const SizedBox(height: 8),
              FilledButton(
                onPressed: () => Navigator.pop(sheetContext),
                child: const Text('Scan another'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: const Text('Scan an asset label'),
        actions: [
          IconButton(
            tooltip: _torch ? 'Turn the light off' : 'Turn the light on',
            icon: Icon(_torch ? Icons.flashlight_on : Icons.flashlight_off),
            onPressed: () {
              _controller.toggleTorch();
              setState(() => _torch = !_torch);
            },
          ),
          IconButton(
            tooltip: 'Switch camera',
            icon: const Icon(Icons.cameraswitch_outlined),
            onPressed: () => _controller.switchCamera(),
          ),
        ],
      ),
      extendBodyBehindAppBar: true,
      body: Stack(
        fit: StackFit.expand,
        children: [
          MobileScanner(
            controller: _controller,
            onDetect: _onDetect,
            errorBuilder: (context, error, child) => Center(
              child: Padding(
                padding: const EdgeInsets.all(28),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(
                      Icons.no_photography_outlined,
                      color: Colors.white70,
                      size: 44,
                    ),
                    const SizedBox(height: 14),
                    const Text(
                      'The camera is not available.',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Allow camera access for UN PASS in your phone settings, then come back.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: Colors.white70),
                    ),
                    const SizedBox(height: 18),
                    OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                        foregroundColor: Colors.white,
                        side: const BorderSide(color: Colors.white38),
                      ),
                      onPressed: () => Navigator.of(context).push(
                        MaterialPageRoute(builder: (_) => const AssetSearchScreen()),
                      ),
                      icon: const Icon(Icons.search),
                      label: const Text('Search instead'),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const _ScanFrame(),
          if (_looking)
            Container(
              color: Colors.black54,
              child: const Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(color: Colors.white),
                    SizedBox(height: 14),
                    Text(
                      'Looking it up…',
                      style: TextStyle(color: Colors.white, fontSize: 16),
                    ),
                  ],
                ),
              ),
            ),
          Positioned(
            left: 0,
            right: 0,
            bottom: 0,
            child: Container(
              padding: EdgeInsets.fromLTRB(
                24,
                18,
                24,
                24 + MediaQuery.of(context).padding.bottom,
              ),
              color: Colors.black.withOpacity(0.55),
              child: const Text(
                'Hold the label inside the frame. It scans on its own — no button to press.',
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white, height: 1.4),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// The cut-out with UN blue corners, so people know where to aim.
class _ScanFrame extends StatelessWidget {
  const _ScanFrame();

  @override
  Widget build(BuildContext context) {
    final side = MediaQuery.of(context).size.width * 0.72;
    return Center(
      child: SizedBox(
        height: side,
        width: side,
        child: Stack(
          children: [
            for (final corner in const [
              Alignment.topLeft,
              Alignment.topRight,
              Alignment.bottomLeft,
              Alignment.bottomRight,
            ])
              Align(
                alignment: corner,
                child: Container(
                  height: 44,
                  width: 44,
                  decoration: BoxDecoration(
                    border: Border(
                      top: corner.y < 0
                          ? const BorderSide(color: UnColors.blue, width: 4)
                          : BorderSide.none,
                      bottom: corner.y > 0
                          ? const BorderSide(color: UnColors.blue, width: 4)
                          : BorderSide.none,
                      left: corner.x < 0
                          ? const BorderSide(color: UnColors.blue, width: 4)
                          : BorderSide.none,
                      right: corner.x > 0
                          ? const BorderSide(color: UnColors.blue, width: 4)
                          : BorderSide.none,
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
