# Agentic flows — AI cards on the Flow Studio canvas

Flow Studio moves a document between **people**. Agentic flows move it between
**AI steps**: read it, summarise it, check it against a policy page, convert it,
decide where it goes, and tell the right people. People can still step in: a
**Human check** card pauses the run until someone approves (or corrects) the
AI's work.

Find it under **NES → Agentic flows** (next to Workflows), or from the
**Agentic flows** button on the Workflows page.

---

## 1. Install

```bash
docker compose build web
docker compose exec web python manage.py makemigrations accounts
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
docker compose exec web python manage.py test accounts.tests_esign_agents   # 47 tests, no network needed
```

Nothing else is required: with no AI provider set, agents run on the
**built-in offline engine** (see §3), so you can try everything immediately.

The module is in the tenancy catalogue as **NES Agentic flows**
(`esign_agents`, on by default, needs NES). A country-office admin can switch it
off for their office; the gate covers every `accounts:esign_agent*` URL and the
envelope triggers.

## 2. Connect an AI provider (optional, recommended)

Set these in the environment (`docker-compose.yml` → `web.environment`) or in
`settings.py`:

| Setting | Meaning |
|---|---|
| `AGENT_AI_PROVIDER` | `anthropic` (Claude), `openai` (OpenAI **or any OpenAI-compatible server** — Azure OpenAI, Ollama, vLLM, LM Studio), or `offline`. Empty = Claude when a key is present, otherwise offline. |
| `AGENT_AI_API_KEY` | The key. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are also read. |
| `AGENT_AI_MODEL` | Default `claude-sonnet-5-5` for Claude, `gpt-4.1-mini` for OpenAI. |
| `AGENT_AI_BASE_URL` | For regional, proxied or self-hosted endpoints, e.g. `http://ollama:11434/v1`. |
| `AGENT_AI_AUTH_HEADER` | `bearer` (default) or `api-key` (Azure OpenAI). |
| `AGENT_AI_TIMEOUT` | Seconds per AI call (default 90). |
| `AGENT_AI_MAX_INPUT_CHARS` | How much of a document is sent per call (default 60 000 characters). |

Examples:

```yaml
# Claude
- AGENT_AI_PROVIDER=anthropic
- AGENT_AI_API_KEY=sk-ant-…

# A model running inside your own network (no data leaves it)
- AGENT_AI_PROVIDER=openai
- AGENT_AI_BASE_URL=http://ollama:11434/v1
- AGENT_AI_MODEL=llama3.1:8b
```

Calls use Python's standard library, so the server's `HTTPS_PROXY` is honoured
and no new package is needed. The active provider is shown on every agent page.

## 3. The offline engine

When no provider is set, cards still work — deterministically, and every result
is labelled **built-in**:

| Card | Offline behaviour |
|---|---|
| Summarize | Extractive: the most informative sentences, the document's own labelled lines ("Vendor: …"), amounts, dates, obligations and deadlines. |
| Extract facts | "Label: value" lines, then patterns for the kind of value asked (dates, amounts, emails, references). Nothing is guessed. |
| Classify | Keyword scoring against each category's name and hint. |
| If (ask AI) | Key-term match; "more than 50,000"-style questions compare against amounts found. |
| Policy check | Lists requirement sentences ("must", "shall", "not exceed"…) from the policy that the document never addresses. |
| Mask personal data | Always local (emails, phones, ID and bank numbers). Names need a provider. |
| Translate / Custom AI step | Skipped with a clear message (they need a provider). |
| Build from a description | A built-in parser turns plain English into cards. |

## 4. The cards

Every card has a **context box**. For AI cards it is the instruction for that
step; for others it is the opening text (email message, note intro, cover-page
intro, reviewer instructions). The **Trigger** card's context box holds
*standing instructions* that every AI card receives ("You work for UNDP The
Gambia. Use British English.").

| Group | Card | What it does | Exits |
|---|---|---|---|
| Start | **Trigger** | *When I run it* · *A document comes to me for signature* · *I send a document for signature* · *A document I sent is fully signed*. Answer language; mask personal data before AI. | Next |
| Start | **Policy** | Upload a policy (Word, PDF, text), paste it, link it or reuse one from another agent. It becomes a **rulebook inside the agent** and every document is checked against it. See §5. | Doesn't apply / Complies / Warning / Breach |
| Start | **Knowledge link** | Reads web pages / PDFs / Word files by URL, an **uploaded file**, or pasted reference text (ToR, checklist). Later cards use it. | Next |
| AI | **Summarize** | Executive brief, bullets, paragraph, or *what I need to do before signing*; brief/standard/detailed. | Next |
| AI | **Extract facts** | The facts you list, as a table. Feed it to Convert → Excel. | Next |
| AI | **Classify** | Your categories (up to 8) — each is its own exit — plus *Other*. | One per category + Other |
| AI | **Policy check** | Compares with everything the Knowledge links read (+ rules in the context box). | Passes / Issues found |
| AI | **Translate** | Into any language, keeping structure and figures. | Next |
| AI | **Mask personal data** | Emails, phones, IDs, bank details, names. Later cards work on the masked text. | Next |
| AI | **Custom AI step** | Anything — the context box is the instruction. | Next |
| Docs | **Convert** | To PDF, Word, Excel, CSV, text, Markdown or HTML — from the document (keeps layout where it can, via LibreOffice) or from an earlier card's result. File name template supported. | Next |
| Docs | **Summary cover page** | A one-page "AI-generated" summary sheet in front of the PDF. | Next |
| Docs | **Save to PDF tools** | Puts the run's PDFs in the person's PDF tools workbench. | Next |
| Decide | **If** | Ask the AI a yes/no question, or a rule on an earlier result (contains / is / more than…). | Yes / No |
| Decide | **Human check** | People approve, edit or reject the AI's work. Link by email, no sign-in needed. Optional due date. | Approved / Rejected |
| Deliver | **Email** | To colleagues from the directory **or any typed address**, plus *me*, *the sender*, *the signers*. Subject template, choose which results to include, attach the run's files and/or the original, or *let AI write the email*. | Next |
| Deliver | **Note on envelope** | Posts the result on the envelope (internal, or visible to every signer). Sender's own agents only. | Next |
| Deliver | **Teams / webhook** | Microsoft Teams (Adaptive Card via Workflows webhook), Slack, or plain JSON. | Next |
| Other | **Finish**, **Sticky note** | End of a path; a note on the canvas that never runs. | — |

**Data tokens** (the *Insert* menu next to a context box, subject or file name):
`{{document}}`, `{{filename}}`, `{{subject}}`, `{{sender}}`, `{{today}}`,
`{{reference}}`, `{{flow}}`, `{{me}}`, and any earlier card by name — e.g.
`{{Key contract terms}}`.

**What a card reads.** By default, the previous *content* card's result, or the
document if there is none. Decision cards (Classify, If) are see-through, so a
Summarize after a Classify summarises the document, not the category name. Any
card can be pointed at the document itself or a specific earlier card.

**If a card fails.** Each card chooses: stop the run and tell me (default), or
skip it and carry on (branching cards take Other / Issues / No / Rejected).

## 5. Policies — upload once, built into the agent

Drop a **Policy** card on the canvas and the **Policy studio** opens:

1. **Give it the policy** — upload a Word (.docx/.doc), PDF, text, Markdown or
   HTML file; paste it; link it; or **reuse** a policy from any agent you can
   use. Optional guidance: *"Only sections 4–7 apply to contracts."*
2. **Read it and build the rules.** The AI (or the built-in reader, without a
   provider) reads the policy and writes a rulebook: title, version, effective
   date, what it applies to, a summary, and up to 25 numbered rules with their
   section references. Permissions ("may be approved by…") are not turned into
   rules.
3. **Check and edit the rules.** Each rule has an on/off switch, a severity and
   a way of being checked:

   | Check | How | AI needed |
   |---|---|---|
   | **Amount limit** | The document's largest amount vs. a limit (e.g. *flag when more than 50,000 USD*). | No — exact |
   | **Must mention** | One of the words must appear (`performance guarantee/performance bond`). | No — exact |
   | **Must not mention** | None of the words may appear (`advance payment/prepayment`). | No — exact |
   | **AI judgement** | Anything else; the AI decides and quotes the evidence. Without a provider, the built-in checker looks for the rule's key terms and says *unclear* when unsure. | Yes (falls back) |

   A rule can be limited to documents that mention certain words, and can carry
   its own extra action — *"over 50,000 → start the Contracts Committee
   workflow"*.
4. **Save to the card.** The uploaded file is read and **discarded**; only the
   rulebook (and, unless you untick it, the policy text) is kept — **inside the
   agent**. Rebuilding from a new version of the policy shows what changed and
   keeps your switches, severities and actions on rules that still exist.

**Outcomes.** `Must` rules decide a **Breach**; `Should` rules a **Warning**;
*unclear* answers count as a Warning (or as a Breach with *Strict* on); *For
information* rules are only reported. With scope words set, a document that
mentions none of them leaves by **Doesn't apply**; without them the AI judges
the scope from "It applies to".

**What happens.** Each outcome can act before it leaves by its exit:

| Action | What it does |
|---|---|
| **Inform people** | Emails the findings to people from the directory, typed addresses, *me*, the sender or the signers. |
| **Warn the people involved** | A warning email, and optionally a note on the envelope (internal, or seen by every signer). |
| **Hold for a sign-off** | The run waits; the chosen people get a link. *Sign off* → it carries on along that outcome's exit; *don't sign off* → the agent stops. |
| **Run another agent** | Starts another agent on the same document for the same person (loops are refused). |
| **Start a Flow Studio workflow** | Sends the document into one of your workflows (e.g. an exception approval) with the findings as its message. Roles chosen at start go to the people you pick, or to the person the agent runs for. |
| **Stop the run** | Nothing after the card runs; the run is marked **Stopped by a policy**. |

Connect the exits to carry on as usual (e.g. Complies → summarise and email),
or leave them unconnected to end there.

**Sharing and export.** Because the rulebook lives in the agent, colleagues you
share it with — one person, the whole CO or the agency — and anyone who imports
the `.unpassagent` file get the policy built in; they never need the original
Word or PDF. On export, people and links to *your* agents and workflows are
removed and listed for the importer to choose again; the rules and text stay.
Untick *Keep the policy text inside the agent* to share the rules only.

The run page shows the outcome, every rule's result (✗ flagged, ? unclear,
✓ fine, – not applicable) with its evidence and whether it was an exact check, the
AI or the built-in checker, and every action taken, with links to started
agents and workflows.

The ready-made **Policy guard for documents I'm asked to sign** shows the whole
pattern with a small sample rulebook — replace it with your own policy.

## 6. Designing

- Drag cards from the palette, or **select a card and click a palette item** —
  the new card is placed after it and connected for you.
- Drag from a dot on the right of a card to connect it. `Del` deletes,
  `Ctrl+D` duplicates, `Ctrl+Z / Ctrl+Y` undo/redo, `Ctrl+S` saves, Tidy up
  arranges everything.
- **Build from a description**: type what the agent should do in plain English;
  it is drawn for you (by the AI if one is connected, otherwise by the built-in
  parser). Replace the canvas or add to it.
- Problems are checked on every save and shown on the cards. An automatic agent
  with problems is switched off until they are fixed.
- **Test run** → upload a file, paste text, pick a PDF tools file, or **pick one
  of your envelopes** to test an automatic agent. **Dry run** (on by default)
  does all the thinking but sends nothing — you see exactly what *would* have
  been emailed, posted or saved.

## 7. Running automatically

Automatic triggers fire after the envelope's status change is committed, so an
agent can never slow down or roll back a signature:

- *A document comes to me for signature* — for each signer/approver with an
  account, when the envelope is sent.
- *I send a document for signature* — for the sender.
- *A document I sent is fully signed* — for the sender, on the completed PDF.

Switch an automatic agent **on** from the designer (top right) or its card on
the Agentic flows page. Each envelope revision starts each agent once per person.
Runs happen on a background thread (`AGENT_RUN_MODE=thread`, the same approach
the app uses for email). If an automatic run fails, the person it ran for gets an
email. `AGENT_MAX_RUNS_PER_HOUR` (default 120 per person) caps AI cost.

The run page is live: cards light up on the map as they finish, and each card
shows its result, the model, tokens and timing.

## 8. Sharing

From **Share** in the designer:

- **Me and the people below / My whole country office / My whole agency.**
- **Specific people** — from the directory or any email address, as **Can use**
  or **Can edit**. Email-only shares attach to the account the first time that
  person signs in. New people can be emailed a notice.
- People an *automatic* agent is shared with can click **Use on my documents**:
  it then runs for their envelopes too, as them. One person builds an agent; the
  whole CO can switch it on.
- Only the owner can change sharing or delete. Editors can change the cards.

**Export / import** (`.unpassagent`): plain JSON with a SHA-256 checksum, like
Flow Studio's `.unpassflow`. People (directory ids and email addresses) and
webhook addresses (secrets) are **never exported**; the importer sees a preview
— the cards, the links it reads, and what they must fill in — and the agent
arrives private and switched off.

## 9. Safety and privacy

- **Documents are data, not instructions.** Every prompt puts the document,
  reference pages and earlier results inside tagged blocks, and the system prompt
  tells the model never to follow instructions found in them.
- **Mask personal data** can run before anything reaches the AI (Trigger option
  or the card). For full data sovereignty, point `AGENT_AI_BASE_URL` at a model
  inside your network.
- **Knowledge links and webhooks** only reach `http(s)` addresses; private,
  loopback, link-local and metadata addresses are refused (redirects are
  re-checked). Allow intranet hosts explicitly:
  `AGENT_LINK_ALLOWED_HOSTS=intranet.undp.org,policies.gm.undp.org`.
- AI output is labelled as AI-generated everywhere it goes: emails, envelope
  notes, cover pages, Teams posts.
- Runs keep their own copy of the graph, and every step records model, tokens,
  timing and who decided a Human check — visible on the run page and in the
  admin (read-only; there is a **Switch off** admin action for emergencies).

## 10. Files

```
accounts/models_esign_agents.py      AgentFlow, AgentFlowShare, AgentSubscription, AgentRun, AgentStep + envelope signals
accounts/agent_engine_esign.py       card catalogue, validation, runner, cards, triggers, emails, compose
accounts/agent_ai_esign.py           Claude / OpenAI-compatible / offline provider layer
accounts/agent_offline_esign.py      the built-in engine
accounts/agent_policy_esign.py       Policy card: rulebook builder, rule checks, outcomes
accounts/agent_docs_esign.py         reading files and links, conversions, cover page
accounts/agent_portability_esign.py  .unpassagent export / import
accounts/agent_library_esign.py      ready-made agents
accounts/views_esign_agents.py       pages and JSON endpoints
accounts/urls_esign_agents.py        URLs (included from accounts/urls.py)
accounts/admin_esign_agents.py       admin
accounts/tests_esign_agents.py       tests
templates/accounts/esign/agents/     home, designer, run, check, import pages
templates/accounts/esign/email/agent_message.html
```
