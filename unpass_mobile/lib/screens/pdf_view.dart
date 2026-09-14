import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_pdfview/flutter_pdfview.dart';
import 'package:path_provider/path_provider.dart';

import '../core/api.dart';
import '../widgets/common.dart';

/// Authenticated in-app PDF reader.
///
/// Important: protected UNPASS document URLs should not be opened in the
/// external browser because the browser does not share the app's Django session
/// cookie. This viewer fetches the PDF itself and validates the bytes before
/// handing them to Android's PDF renderer.
class PdfScreen extends StatefulWidget {
  const PdfScreen({
    super.key,
    required this.path,
    required this.title,
    this.subtitle = '',
  });

  final String path;
  final String title;
  final String subtitle;

  @override
  State<PdfScreen> createState() => _PdfScreenState();
}

class _PdfScreenState extends State<PdfScreen> {
  File? _file;
  String? _error;
  int _pages = 0;
  int _page = 0;
  bool _ready = false;
  PDFViewController? _controller;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    final file = _file;
    if (file != null) {
      file.delete().catchError((_) => file);
    }
    super.dispose();
  }

  bool _isPdf(List<int> bytes) {
    return bytes.length >= 5 &&
        bytes[0] == 0x25 && // %
        bytes[1] == 0x50 && // P
        bytes[2] == 0x44 && // D
        bytes[3] == 0x46 && // F
        bytes[4] == 0x2D;   // -
  }

  Future<void> _load() async {
    final old = _file;

    setState(() {
      _error = null;
      _ready = false;
      _pages = 0;
      _page = 0;
      _controller = null;
      _file = null;
    });

    if (old != null) {
      try {
        if (await old.exists()) await old.delete();
      } catch (_) {}
    }

    try {
      final bytes = await Api.instance.fetchPdf(widget.path);

      if (!_isPdf(bytes)) {
        throw ApiException(
          'UN PASS returned data that is not a valid PDF. '
          'The final document may need to be regenerated on the server.',
        );
      }

      final dir = await getTemporaryDirectory();
      final name = 'unpass-${DateTime.now().microsecondsSinceEpoch}.pdf';
      final file = File('${dir.path}/$name');
      await file.writeAsBytes(bytes, flush: true);

      if (!mounted) {
        try {
          await file.delete();
        } catch (_) {}
        return;
      }

      setState(() => _file = file);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) {
        setState(
          () => _error = 'The document could not be opened. $e',
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF2B3440),
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.title, overflow: TextOverflow.ellipsis),
            if (widget.subtitle.isNotEmpty)
              Text(
                widget.subtitle,
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w400,
                  color: Color(0xFFCFE6F5),
                ),
              ),
          ],
        ),
        actions: [
          if (_pages > 0)
            Center(
              child: Padding(
                padding: const EdgeInsets.only(right: 6),
                child: Text(
                  '${_page + 1} / $_pages',
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          IconButton(
            tooltip: 'Reload PDF',
            icon: const Icon(Icons.refresh),
            onPressed: _load,
          ),
        ],
      ),
      body: _buildBody(),
      bottomNavigationBar: (_pages > 1 && _error == null)
          ? SafeArea(
              child: Container(
                height: 56,
                color: const Color(0xFF1E2731),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    IconButton(
                      icon: const Icon(
                        Icons.first_page,
                        color: Colors.white70,
                      ),
                      tooltip: 'First page',
                      onPressed: () => _controller?.setPage(0),
                    ),
                    IconButton(
                      icon: const Icon(
                        Icons.chevron_left,
                        color: Colors.white,
                      ),
                      tooltip: 'Previous page',
                      onPressed: _page > 0
                          ? () => _controller?.setPage(_page - 1)
                          : null,
                    ),
                    Text(
                      'Page ${_page + 1} of $_pages',
                      style: const TextStyle(color: Colors.white),
                    ),
                    IconButton(
                      icon: const Icon(
                        Icons.chevron_right,
                        color: Colors.white,
                      ),
                      tooltip: 'Next page',
                      onPressed: _page < _pages - 1
                          ? () => _controller?.setPage(_page + 1)
                          : null,
                    ),
                    IconButton(
                      icon: const Icon(
                        Icons.last_page,
                        color: Colors.white70,
                      ),
                      tooltip: 'Last page',
                      onPressed: () => _controller?.setPage(_pages - 1),
                    ),
                  ],
                ),
              ),
            )
          : null,
    );
  }

  Widget _buildBody() {
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(
                Icons.picture_as_pdf_outlined,
                size: 44,
                color: Colors.white70,
              ),
              const SizedBox(height: 14),
              Text(
                _error!,
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white),
              ),
              const SizedBox(height: 20),
              OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
                  side: const BorderSide(color: Colors.white38),
                ),
                onPressed: _load,
                icon: const Icon(Icons.refresh),
                label: const Text('Try again'),
              ),
              const SizedBox(height: 10),
              const Text(
                'For protected signed documents, use this in-app reader. '
                'Opening the same URL in an external browser can lose the app session.',
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: Colors.white60,
                  fontSize: 12,
                  height: 1.35,
                ),
              ),
            ],
          ),
        ),
      );
    }

    if (_file == null) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(color: Colors.white),
            SizedBox(height: 14),
            Text(
              'Fetching the document…',
              style: TextStyle(color: Colors.white70),
            ),
          ],
        ),
      );
    }

    return Stack(
      children: [
        PDFView(
          key: ValueKey(_file!.path),
          filePath: _file!.path,
          swipeHorizontal: false,
          autoSpacing: true,
          pageFling: false,
          nightMode: false,
          onRender: (pages) {
            if (!mounted) return;
            setState(() {
              _pages = pages ?? 0;
              _ready = true;
            });
          },
          onViewCreated: (controller) => _controller = controller,
          onPageChanged: (page, _) {
            if (!mounted) return;
            setState(() => _page = page ?? 0);
          },
          onError: (error) {
            if (!mounted) return;
            setState(
              () => _error =
                  'This PDF was downloaded but Android could not render it. '
                  'The final PDF may be malformed. $error',
            );
          },
          onPageError: (page, error) {
            if (!mounted) return;
            setState(
              () => _error =
                  'Page ${(page ?? 0) + 1} could not be displayed.',
            );
          },
        ),
        if (!_ready)
          const Center(
            child: CircularProgressIndicator(color: Colors.white),
          ),
      ],
    );
  }
}

Future<void> openPdf(
  BuildContext context,
  String path, {
  required String title,
  String subtitle = '',
}) {
  return Navigator.of(context).push(
    MaterialPageRoute(
      builder: (_) => PdfScreen(
        path: path,
        title: title,
        subtitle: subtitle,
      ),
    ),
  );
}
