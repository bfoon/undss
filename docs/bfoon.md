# BFoon — the NES reading buddy

A small leaf in UNDP blue in the bottom-right corner of every NES page. Closed
by default; click the leaf to open it (Esc or the leaf closes it). Only the leaf
and the open panel take clicks, so it never blocks the page underneath.

| Where | What it does |
|---|---|
| Signing and review pages | **Reading**: a tree that grows a numbered leaf for every page you have actually looked at (on screen for over a second), a leaf stamp on each page, chips for pages not yet seen, and the fields still to fill. Before you sign with pages unread it reminds you once, in a bubble. |
| Envelope page | **Overview**: pages and files, with Summarise / Search / Ask. |
| Any NES page (signed in) | **Search all my documents**: titles, references, envelope ids, people, file names and the words inside every document you may see. Hover a result for sender, people, dates, files and a longer quote; click to open it at the page. **Summarise** or **Ask** about any result from anywhere. |

Search within the open document runs in the browser. Search across documents
runs on the server, over the same access rules as the envelope list, and only
returns what that person may see.

## Summaries and questions

    GEMINI_API_KEY=…                                   # aistudio.google.com
    BFOON_GEMINI_MODELS=gemini-flash-latest,gemini-flash-lite-latest
    BFOON_REDACT=True                                  # mask e-mails, +phones, account-like and long numbers
    BFOON_AI_ENABLED=True                              # False: never call Google
    BFOON_ASKS_PER_HOUR=30

Order: the first model, then the next (Flash-Lite has the larger free quota),
then a summary worked out on the server from the document's own sentences, with
the amounts and dates it mentions. Answers are cached for a day per document
and question, so the same summary is never sent twice.

**Data protection.** On Google's free (unpaid) tier, Google may use what is
sent to improve its products and people may review it. Enable billing on the
Google Cloud project behind the key to stop that; the cost of summaries is
small. BFoon only sends text when someone presses Summarise or asks, says so
in the panel, masks contact and account details first, and the document text
is fenced off so instructions written inside a document are ignored.

The `bfoon-index` job reads new documents every 30 minutes so search across
documents is instant.
