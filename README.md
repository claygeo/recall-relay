# Recall Relay

**Every recall, every pantry, with proof.**

Recall Relay is a Strands Agents application that runs a food bank's recall procedure across its partner pantries. It reads FDA recall notices, matches them against the food bank's receiving ledger and distribution log, builds the pull list, drafts the notices, asks a human for **one** decision, relays the notices to every affected pantry, chases confirmations from volunteer pantries that may not open for weeks, and files the audit packet a food-safety auditor asks for.

Built for the AWS **Agents for Humans** hackathon, Good Neighbor track, September 2026. Apache-2.0.

- Live demo: https://recall-relay.onrender.com (free instance; the first request after idle can take a minute)
- Video: added at submission
- Architecture diagram: [docs/architecture.png](docs/architecture.png)

---

## The problem

When the FDA posts a food recall, Feeding America's national office emails every member food bank. That part works. What happens next is one coordinator, a spreadsheet, and a network of 50 to 500 partner agencies: church pantries, soup kitchens, school pantries, shelters, most of them volunteer-run, some open once a month.

Someone has to cross-check the notice against receiving records that rarely captured a lot code, figure out which agencies took cases and when, email each one, chase confirmations, and produce the record a food-safety review and the annual mock-recall drill ask for. And the shelf sign for the next family through the pantry door never gets printed.

The demo is built on a real recall. On September 2, 2026, Frutas y Hortalizas del Sur S.A. expanded a recall to one lot of Great Value Organic Triple Berry Blend, 10 oz, sold at Walmart stores in 27 states including Florida, for possible E. coli O145. Walmart retail rescue is how a lot of frozen food reaches South Dade pantries. In the seeded ledger, 30 cases arrived August 24 with no lot code on the receipt, and 22 of them shipped to three pantries between August 26 and September 4. That last shipment went out two days after the press release, because nobody had cross-checked the ledger. That is the whole problem in one row.

## Who it is for

A regional or independent food bank's partner-agency network: one food-safety or agency-relations coordinator, the partner agencies, and the households those agencies serve. The demo cohort is a fictional South Dade Community Food Bank in Miami-Dade County with 12 partner agencies, a 60-row receiving ledger, and a 140-row distribution log. The recall notices are real. The food bank is not.

## What the agent does, end to end

1. **Intake** a recall from any source: a forwarded alert or firm letter pasted as text, a pasted FDA press-release URL, an uploaded PDF, the FDA press-release RSS feed (same day), or the weekly openFDA enforcement ledger. Each becomes a typed `RecallNotice`. Non-food items (drugs, devices, pet food) are filtered out.
2. **Match** it against the ledger. Deterministic scoring narrows the ledger to a handful of candidates; a Matcher agent adjudicates them into `MATCH`, `NO_MATCH`, or `NEEDS_HUMAN` with evidence. Every dismissal is logged. Nothing is pinged.
3. **Trace** the pull list: cases still on hand (tagged HOLD automatically), cases shipped to which agency on which date, with the missing-lot widening rule applied and explained.
4. **Ping** the coordinator once, with the pull list, the drafted agency notices, and the drafted client shelf signs. Approve or dismiss.
5. **Relay** on approval: a typed notice to each affected agency (recall number, product, lot, best-by, reason, class, the disposition instruction copied verbatim from the source) with four one-click responses: pulled, never received, already distributed to clients, need pickup. "Already distributed" triggers the client-facing shelf sign in English, Spanish, and Haitian Creole.
6. **Chase**: no response leads to a reminder at the class-appropriate cadence, then an escalation to the coordinator with the agency's phone contact and a call script. An agency is never auto-marked confirmed.
7. **Close and file**: an audit packet with the source, the matched rows, the pull list, every send and response with timestamps, the disposition, the approvals, and the elapsed time from notice to full trace.

## The one decision

This is the entire human workload for the demo recall. It is the ping the product generated in a live run on 2026-09-12, copied without edits:

> FDA press release 9/3: Great Value Organic Triple Berry Blend 10 oz, lot 6040 01-6, Possible E. Coli Contamination. Your ledger: receipt 17, 30 cases received 8/24. No lot on the receipt, so all 30 are treated as affected. 8 on hand (HOLD tag applied). 22 shipped to 3 agencies 8/26-9/4; one distributes same-day. Pull list, 3 agency notices, and 7 shelf signs drafted. Send?

Seven signs because each affected pantry gets one per language it serves. The case shows "recall pending" until openFDA publishes a recall number for the expansion; the July recall from the same firm (H-1181-2026) is a separate case that surfaces the ambiguous blueberry receipt as `NEEDS_HUMAN`.

## The 15 encoded rules

These are code paths in [`app/recall_relay/core/rules.py`](app/recall_relay/core/rules.py), not prompt lines. A judge or an auditor can read what the agent is not allowed to do.

1. **Intake precedence.** The earliest source creates the case. openFDA only enriches and never delays action. Every case records which source created it and when each source was seen. Once a case is approved, a later openFDA classification is recorded on the case but not applied to notices that already went out.
2. **Class-driven handling** (21 CFR 7.3(m)). Class I: pull, notify, client notice, reminder at 24 hours. Class II: pull, notify, reminder at 72 hours, client notice only on "already distributed". Class III and market withdrawals: HOLD on hand, agencies notified on a weekly follow-up cadence, no client notice. Unclassified press releases are handled as Class I until openFDA says otherwise.
3. **Match hierarchy.** Brand plus product line plus size decide; the receipt-date window narrows; a lot code only narrows a match, never widens a miss. UPC is evidence, never a join key.
4. **Missing lot widens, never drops.** A matched receipt with no lot means every case of that line is affected, and the notice says so.
5. **NEEDS_HUMAN is the default for ambiguity.** Never NO_MATCH by default. The coordinator's resolution is remembered.
6. **Regulatory scope is explicit.** The feeds cover FDA-regulated foods. USDA FSIS (meat, poultry, egg products) and USDA Foods hold-and-recall notices enter through paste or forward and are tagged by channel. The agent never claims FSIS coverage.
7. **Salvage and repack rows have no lot lineage.** They are flagged "physical sort required". The agent never computes a case count for them.
8. **Distribution-pattern gate.** Auto-dismiss silently only when the notice excludes the food bank's state and the brand was never received. Distribution patterns are routinely incomplete; the demo recall's own press release was corrected from 16 to 27 states.
9. **Closed agency response taxonomy.** Pulled (with count), never received, already distributed to clients, need pickup. Free text is attached, never parsed for status.
10. **Follow-up cadence is encoded in one table** (`FOLLOWUP_CADENCE_HOURS`). Class I: reminder at 24 hours, escalation at 48. Class II: 72 hours and 7 days. Class III and market withdrawals: weekly, no escalation. Escalation includes a call script. Nothing is ever auto-confirmed.
11. **Disposition is copied, not invented.** On-hand product is tagged HOLD and quarantined, not destroyed. The tag stays on the ledger until the coordinator releases it, which is meant to happen only after the disposition (return, destroy, or pickup) is recorded. The disposition instruction is copied verbatim into every agency notice.
12. **One human decision per case.** Notices never send without approval. An ambiguous match asks the coordinator one question first (which ledger row is it), then comes back for the same single approval. HOLD tags apply automatically because they can be released from the ledger.
13. **Audit packet on close.**
14. **Never re-ping; remember decisions.** A recall the scan has already seen is skipped on later runs, and an openFDA record for an existing case only enriches it. Coordinator decisions ("row 34 was Driscoll's, not the recalled firm") are stored and handed to the Matcher on every later run.
15. **Client-facing notice** on Class I or on "already distributed": plain language, in each language the agency serves (English, Spanish, and Haitian Creole in the demo), what to do, symptoms, the agency's contact.

## Try it (testing instructions)

The live demo is public and read-only until you click something. Limits on the live host, shared by everyone who visits: 4 agent runs per day (a scan or a manual intake each count as one), 6 scans and 12 intakes per hour, one scan at a time, one demo reset per minute. The counters reset at midnight UTC.

0. If the case register is not empty, click **Reset demo** in the page footer first. A second scan skips every recall it has already seen (rule 14), so only a fresh register produces the ping.
1. Open the **Ledger** tab. Receipt 17 is the hero row: Great Value Organic Triple Berry Blend, 10 oz, no lot, 30 cases.
2. Open **Run** and click **Run daily scan**. The scan merges a pinned feed snapshot from 2026-09-11 with the live FDA feed and the weekly openFDA ledger, so the hero recall reproduces on every run. Watch the tally and the tool calls stream. Expect one `AWAITING_APPROVAL` case (Triple Berry) and one `NEEDS_HUMAN` case (an unbranded blueberry receipt against the same firm's July recall); the second is the Matcher's judgment call and has reproduced on every run so far. Everything else lands as dismissed, non-food, out of area, or enriched, each with a reason.
3. Open the Triple Berry case. Read the ping. Click **Approve and send** and confirm the dialog (rule 12: this is the one decision).
4. Open **Inbox**. Three agency notices are there. Click "We pulled it (enter case count)" on one and enter a count; click "Already distributed to clients" on the pantry marked same-day (Florida City Family Shelter); its inbox then holds the shelf sign in English, Spanish, and Haitian Creole.
5. Back on the case, click **Advance demo clock +24h** and **Run follow-ups now**: the silent agency gets a reminder. Advance again: the coordinator gets an escalation with a call script.
6. Click **Close case** and open the audit packet, or click **Packet PDF** (it opens in the browser).
7. Paste the text of any FDA recall notice, or a forwarded alert, into the intake box on the Run tab to run the whole thing on a recall of your choosing. URL intake works for the 21 cached demo pages and when you run the app on a residential connection; the public host cannot reach fda.gov, and a blocked URL says so and asks for the text.
8. On the Ledger, the HOLD tag on receipt 17 has a `release` link for when the disposition is recorded (rule 11).

## Architecture

![Architecture](docs/architecture.png)

### Strands Agents, by name

- **Orchestrator `Agent`** ([`agents/orchestrator.py`](app/recall_relay/agents/orchestrator.py)) with eleven `@tool` functions that run the procedure, registered under these names: `load_case`, `score_candidates` (deterministic scoring over the ledger; the model never sees the raw ledger, only the top candidates), `adjudicate_candidates`, `build_pull_list`, `draft_notices`, `request_approval`, `dismiss_case`, `mark_needs_human`, `remember_decision`, `recall_decisions`, `send_notices`. Tools receive the case register through Strands `ToolContext.invocation_state`, so the same tools run against a local SQLite file or a remote dashboard. Intake (feed polling, page parsing, the distribution gate) happens in deterministic code before the orchestrator is invoked.
- **Agents-as-tools.** The Matcher is a separate `Agent` that runs inside the `adjudicate_candidates` tool ([`agents/matcher.py`](app/recall_relay/agents/matcher.py)); the Notice writer is a separate `Agent` that runs inside `draft_notices` ([`agents/writer.py`](app/recall_relay/agents/writer.py)). Each has a narrow system prompt and returns a typed object, and the orchestrator only ever sees the typed result.
- **`structured_output_model`.** `MatchVerdict`, `AgencyNotice`, `ClientSign`, and the extractor's `ExtractedNotice` are Pydantic models passed as `structured_output_model` at invocation. The ping is gated by a typed field, not by prose. Code post-validates every typed output (matched receipt ids must be candidates; a notice's disposition must equal the source's, or it is overwritten and the correction audited).
- **Hooks** ([`agents/hooks.py`](app/recall_relay/agents/hooks.py)). `AuditHook` writes an audit row on `BeforeToolCallEvent` and `AfterToolCallEvent` (this is the trace the packet is built from). `ApprovalGuard` sets `cancel_tool` on `BeforeToolCallEvent` for `send_notices` unless the case carries `approved_at`. `TerminalToolGuard` sets `end_turn` on `AfterToolsEvent` once `request_approval`, `dismiss_case`, or `mark_needs_human` has run, so the run ends at the decision and the model cannot ping twice. The hook is what stops the agent from relaying early; the dashboard's approve button is the only thing that writes `approved_at`, and both paths relay through the same `relay_notices` function.
- **`stream_async`** feeds a server-sent-events endpoint; the Run tab renders tool calls and verdicts as they happen, and replays the last finished run after a refresh.
- **Model provider switch.** One environment variable selects `BedrockModel`, `AnthropicModel`, or an OpenAI-compatible endpoint (`agents/model_factory.py`). The same agent code runs locally and on the web host, and the same entrypoint is packaged for AgentCore Runtime.

### Amazon Bedrock AgentCore

- **Runtime.** [`app/runtime_main.py`](app/runtime_main.py) wraps the same service functions in `BedrockAgentCoreApp` with a small payload contract: `{"mode": "scan", "live": true, "snapshot": true}`, `{"mode": "intake", "url": ...}` or `{"mode": "intake", "text": ...}`, `{"mode": "approve", "case_id": ...}`, `{"mode": "followups"}`, `{"mode": "status"}`. Responses stream as newline-delimited JSON events. It is configured for the AgentCore CLI's CodeZip build (no container) in [`agentcore/agentcore.json`](agentcore/agentcore.json).
- **Status at submission: packaged, validated, not deployed.** `agentcore validate` passes and `agentcore package -r RecallRelay` produces the 65 MB CodeZip artifact on this repo. The hackathon AWS account was created on the final weekend and could not yet run Bedrock, so the Runtime was not launched. `agentcore deploy --yes` is the one remaining command. The public demo runs the same agent code on a conventional web host.
- **Stateless by design.** The dashboard (FastAPI plus SQLite) is the system of record. With `AGENT_BACKEND=remote`, the Runtime's tools reach it through an authenticated REST endpoint (`POST /api/data/rpc` at `AGENT_DATA_URL`, shared secret `AGENT_DATA_SECRET`, allow-listed store methods) via [`core/remote_store.py`](app/recall_relay/core/remote_store.py); with `AGENT_BACKEND=inprocess` (the default, and what the web host uses) the same tools call the SQLite store directly. The switch changes transport, never storage. The endpoint and the remote store are covered by the dashboard's tests; a deployed Runtime has not exercised them yet.
- **Schedule-ready, not scheduled.** A daily EventBridge Scheduler target invoking the Runtime with `{"mode": "scan"}` is the intended production shape. No agent run is scheduled anywhere today; the scan is a button and an API call. The only cron in the repo is a health ping that keeps the free demo host awake.
- **Model note.** The live demo runs Claude Sonnet 4.6 through an OpenAI-compatible endpoint (OpenRouter) because the new AWS account's Bedrock quota had not been raised from zero. The Bedrock path is the same `BedrockModel` code behind one environment variable (`MODEL_PROVIDER=bedrock`, default model `global.anthropic.claude-sonnet-4-6`).

## Data sources, and what was measured

All keyless and public. Measured on 2026-09-11.

| Source | What it gives | What we measured |
|---|---|---|
| FDA recalls RSS | Same-day press releases (title, link, date) | 20 items; mixes food, drug, device, and pet-food items, so the press page's Product Type decides `is_food` |
| FDA press pages | Products, UPCs as printed, lots, best-by, distribution sentence, disposition sentence | 200 with a browser User-Agent from a residential connection; a `python-requests` User-Agent is redirected to `/apology_objects/abuse-detection-apology.html` (a 404 at the final URL). Datacenter egress is walled too: every live fetch from the public host was blocked. The fetch tool detects the wall and never parses the apology page; the 20 pages of the pinned feed are committed fixtures, and a blocked live item is counted as "blocked, paste to open" so the coordinator can paste the notice text |
| openFDA food enforcement | Recall number, classification, `code_info`, distribution pattern | Refreshes weekly; `meta.last_updated` was nine days old; initiation-to-report lag on the 100 most recent records was min 11, median 33, max 196 days; `openfda.upc` was empty on 100 of 100. The demo recall returned zero rows nine days after its press release |
| USDA FSIS recall API | Meat, poultry, egg products | 403 behind a bot wall. Paste path only, tagged `FSIS` |

## What is live and what is seeded

- **Live:** the FDA feed, the press pages, openFDA. The two recalls the demo turns on (H-1181-2026 and the September 2 expansion) are real and can be checked on fda.gov.
- **Seeded:** the food bank, its 12 agencies, the 60-row ledger, and the 140-row distribution log. They are generated deterministically ([`core/seed.py`](app/recall_relay/core/seed.py)) and deliberately messy: store brands, blank UPCs, lots on about a third of rows, one salvage row.
- **Pinned:** the scan merges a snapshot of the feed from 2026-09-11 (and a snapshot of 27 openFDA records) with the live feed and a live openFDA query, so the hero recall reproduces after it scrolls off the 20-item window. The press pages for the pinned feed are committed fixtures (`data/fixtures/press/index.json`), which is also what keeps the demo working from a host that fda.gov walls.
- **Hosting:** the demo runs on a free web instance that sleeps when idle and loses its disk on every restart. The app seeds the ledger on boot, so a cold start shows the ledger and an empty case register; a GitHub Actions ping every 14 minutes ([`.github/workflows/keepalive.yml`](.github/workflows/keepalive.yml)) keeps it warm during judging. Agent runs are capped per day (`MAX_AGENT_RUNS_PER_DAY`) and scans are limited to one at a time.
- **Email:** the in-app agency inbox mirror is the only implemented mail backend, so a judge sees every notice without an email account. The `ses` and `resend` backends in `agents/mailer.py` are stubs that raise `NotImplementedError`; nothing sends real email.
- **Not covered:** FSIS and USDA Foods notices arrive only by paste or forward. openFDA lags; the press feed leads.
- **Incumbents named:** Feeding America's national alert, Ceres (the ERP some large banks run), and Recall InfoLink cover the alert and the upstream. Recall Relay starts where the alert stops.

## Run it locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
cp .env.example .env            # set MODEL_PROVIDER and a key (see below)
.venv/Scripts/python.exe scripts/seed_demo.py
.venv/Scripts/python.exe -m uvicorn recall_relay.web.app:app --port 8000 --app-dir app
```

Model providers: `MODEL_PROVIDER=bedrock` (uses your AWS credentials and `global.anthropic.claude-sonnet-4-6`), `anthropic` (`ANTHROPIC_API_KEY`), or `openrouter` (`OPENROUTER_API_KEY`).

Deploy to AgentCore Runtime (needs AWS credentials, Node 20, and the CLI: `npm i -g @aws/agentcore`):

```bash
agentcore validate
agentcore package -r RecallRelay
agentcore deploy --yes
agentcore invoke --prompt '{"mode": "status"}'
```

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

All tests run offline against fixtures. The core modules (rules, store, intake, agents, web) carry planted-defect tests: a corrupted hero row, a mutated press page, a case without approval, a reset that must keep the ledger, a release that must not clear a neighbor's hold. A verifier that has never failed has never been tested. During the build, an 18-mutant pass against the rules and the store turned the suite red every time; that harness ran in a scratch directory and is not part of the repo.

## Routes

| Route | What it does |
|---|---|
| `GET /`, `GET /ledger` | receiving ledger, distribution log, agencies; HOLD tags with a `release` link |
| `GET /run` | daily scan with the live log; manual intake (URL, text, PDF) |
| `GET /cases`, `GET /cases/{id}` | case register and the decision card, pull list, agency grid, drafted notices, signs |
| `GET /cases/{id}/packet`, `GET /cases/{id}/packet.pdf`, `GET /packet` | the audit packet as print-first HTML, as PDF, and the newest filed packet |
| `GET /inbox`, `GET /inbox/{agency_id}` | the agency inbox mirror: every message the system sent |
| `GET /r/{token}`, `POST /r/{token}` | an agency's one-click reply, signed token, count form for "pulled" |
| `POST /api/scan`, `GET /api/scan/stream?job=` | start the daily scan; server-sent events for the live log |
| `POST /api/intake` | open a case from a URL, pasted text, or a PDF |
| `POST /api/cases/{id}/approve`, `/dismiss`, `/resolve`, `/close` | the coordinator's controls |
| `POST /api/followups/run`, `POST /api/clock/advance` | run due reminders and escalations; move the labeled demo clock |
| `POST /api/holds/{receipt_id}/release` | release a HOLD after the disposition is recorded |
| `POST /api/demo/reset` | wipe cases, holds, mail, follow-ups, and events; keep the ledger; the footer's "Reset demo" |
| `POST /api/data/rpc` | the allow-listed store RPC the AgentCore Runtime uses (`X-Relay-Secret` header) |
| `GET /api/health` | counts, budget, clock |

## Thresholds and limits

Matching (`core/rules.py`): candidate window is 120 days before the announcement to 14 days after; composite score is 0.45 brand + 0.40 product + 0.15 size (rapidfuzz token-set ratios); out-of-window multiplies by 0.5; a lot mismatch multiplies by 0.6 and a missing lot never penalizes; candidates below 45 are dropped, the top 8 go to the Matcher, and 80 marks a strong candidate. Two openFDA records from one firm are treated as the same recall only above an 88 product-similarity ratio (`agents/service.py`).

Demo host (`web/security.py`): `MAX_AGENT_RUNS_PER_DAY` (4 on the live host, 60 by default in code, 10 in `.env.example`), 6 scans and 12 intakes per hour, one scan in flight at a time, one reset per minute. All in memory, per process, shared by every visitor.

## Repository layout

```
app/recall_relay/core/      models, rules (the 15), store (SQLite register), remote_store, intake (deterministic parsing), seed, config
app/recall_relay/agents/    Strands agents: orchestrator, matcher, writer, extractor, hooks, service façade, model_factory, mailer
app/recall_relay/web/       FastAPI dashboard: ledger, run (SSE), cases, inbox mirror, packet (HTML + PDF), data RPC, security
app/runtime_main.py         AgentCore Runtime entrypoint (BedrockAgentCoreApp)
app/pyproject.toml          the Runtime package's dependencies (what CodeZip bundles)
agentcore/                  AgentCore CLI project config (CodeZip) and its CDK scaffold
data/fixtures/              cached FDA pages (21) with a URL index, feed snapshot, openFDA payloads, the seeded ledger CSVs
docs/                       architecture diagram, decision log, Devpost text, video script, builder.aws posts
scripts/                    seed_demo, dev (uvicorn with reload), intake_smoke, agent_smoke (one live run)
tests/                      offline test suite (188)
.github/workflows/          keepalive ping for the free demo host
```

## Documentation

- [DESIGN.md](DESIGN.md): the design system the dashboard follows (paper and ink, one accent, no dark mode).
- [docs/DECISIONS.md](docs/DECISIONS.md): every scope and architecture call made during the build, with the reason, including what was cut and why.
- [docs/DEVPOST.md](docs/DEVPOST.md): the submission text.
- [docs/VIDEO_SCRIPT.md](docs/VIDEO_SCRIPT.md): the 75-second video script with screen cues.
- [docs/builder-aws/](docs/builder-aws/): three build-story posts (the openFDA lag, the fda.gov abuse wall, the approval hook).
- [.env.example](.env.example): every environment variable the app reads.

## Disclosure

All code was written during the submission period. Frameworks and libraries used: Strands Agents, bedrock-agentcore, FastAPI, Pydantic, rapidfuzz, feedparser, pypdf, reportlab. AI coding assistants were used throughout, as the rules allow. No pre-existing project code was incorporated.

## License

Apache-2.0. See [LICENSE](LICENSE).
