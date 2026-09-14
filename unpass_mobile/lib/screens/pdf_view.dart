import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_pdfview/flutter_pdfview.dart';
import 'package:path_provider/path_provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../core/api.dart';
import '../widgets/common.dart';

/// Read a document without leaving the app.
///
/// The file is fetched with the app's own session — the phone's browser has no
/// session cookie, so sending people there met a sign-in page instead of the
/// document. It is written to the app's cache directory, shown with Android's
/// own PDF renderer, and deleted when the screen closes.
class PdfScreen extends StatefulWidget {
  const PdfScreen({super.key, required this.path, required this.title, this.subtitle = ''});

  /// A path on the site, e.g. `/accounts/esign/runs/12/pdf/final/`.
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
    // The document may be confidential; don't leave it on the phone.
    _file?.delete().catchError((_) => File(''));
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _error = null;
      _ready = false;
    });
    try {
      final bytes = await Api.instance.fetchPdf(widget.path);
      final dir = await getTemporaryDirectory();
      final name = 'unpass-${DateTime.now().millisecondsSinceEpoch}.pdf';
      final file = File('${dir.path}/$name');
      await file.writeAsBytes(bytes, flush: true);
      if (!mounted) return;
      setState(() => _file = file);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = 'The document could not be opened. $e');
    }
  }

  Future<void> _openOutside() async {
    final url = Uri.parse(Api.instance.webUrl(widget.path));
    if (!await launchUrl(url, mode: LaunchMode.externalApplication)) {
      if (mounted) showNote(context, "Couldn't hand it to another app.", error: true);
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
              Text(widget.subtitle,
                  style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w400, color: Color(0xFFCFE6F5))),
          ],
        ),
        actions: [
          if (_pages > 0)
            Center(
              child: Padding(
                padding: const EdgeInsets.only(right: 6),
                child: Text('${_page + 1} / $_pages',
                    style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600)),
              ),
            ),
          IconButton(
            tooltip: 'Open in another app',
            icon: const Icon(Icons.open_in_new),
            onPressed: _openOutside,
          ),
        ],
      ),
      body: _error != null
          ? Center(
              child: Padding(
                padding: const EdgeInsets.all(28),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.picture_as_pdf_outlined, size: 44, color: Colors.white70),
                    const SizedBox(height: 14),
                    Text(_error!,
                        textAlign: TextAlign.center, style: const TextStyle(color: Colors.white)),
                    const SizedBox(height: 20),
                    OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                          foregroundColor: Colors.white, side: const BorderSide(color: Colors.white38)),
                      onPressed: _load,
                      icon: const Icon(Icons.refresh),
                      label: const Text('Try again'),
                    ),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: _openOutside,
                      child: const Text('Open on the website instead',
                          style: TextStyle(color: Color(0xFF7FD3F7))),
                    ),
                  ],
                ),
              ),
            )
          : _file == null
              ? const Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      CircularProgressIndicator(color: Colors.white),
                      SizedBox(height: 14),
                      Text('Fetching the document…', style: TextStyle(color: Colors.white70)),
                    ],
                  ),
                )
              : Stack(
                  children: [
                    PDFView(
                      filePath: _file!.path,
                      swipeHorizontal: false,
                      autoSpacing: true,
                      pageFling: false,
                      nightMode: false,
                      onRender: (pages) => setState(() {
                        _pages = pages ?? 0;
                        _ready = true;
                      }),
                      onViewCreated: (c) => _controller = c,
                      onPageChanged: (page, _) => setState(() => _page = page ?? 0),
                      onError: (e) => setState(() => _error = 'This document could not be displayed. $e'),
                      onPageError: (page, e) =>
                          setState(() => _error = 'Page ${(page ?? 0) + 1} could not be displayed.'),
                    ),
                    if (!_ready)
                      const Center(child: CircularProgressIndicator(color: Colors.white)),
                  ],
                ),
      bottomNavigationBar: (_pages > 1 && _error == null)
          ? SafeArea(
              child: Container(
                height: 56,
                color: const Color(0xFF1E2731),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    IconButton(
                      icon: const Icon(Icons.first_page, color: Colors.white70),
                      tooltip: 'First page',
                      onPressed: () => _controller?.setPage(0),
                    ),
                    IconButton(
                      icon: const Icon(Icons.chevron_left, color: Colors.white),
                      tooltip: 'Previous page',
                      onPressed: _page > 0 ? () => _controller?.setPage(_page - 1) : null,
                    ),
                    Text('Page ${_page + 1} of $_pages', style: const TextStyle(color: Colors.white)),
                    IconButton(
                      icon: const Icon(Icons.chevron_right, color: Colors.white),
                      tooltip: 'Next page',
                      onPressed: _page < _pages - 1 ? () => _controller?.setPage(_page + 1) : null,
                    ),
                    IconButton(
                      icon: const Icon(Icons.last_page, color: Colors.white70),
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
}

/// Open a document in the reader above. Use this everywhere instead of handing
/// a URL to the browser.
Future<void> openPdf(BuildContext context, String path, {required String title, String subtitle = ''}) {
  return Navigator.of(context).push(
    MaterialPageRoute(builder: (_) => PdfScreen(path: path, title: title, subtitle: subtitle)),
  );
}
