# Decision log

Auto-decided by Claude under the operator's standing rules (six decision principles), with outside-voice
review. Recorded so the operator can audit them.

## 2026-09-11 (Fri evening)

1. **Concept: Recall Relay** (food bank recall procedure across partner pantries). 23-agent panel:
   unanimous #1 of 19 concepts across three judges; survived 3 adversarial refuters with design changes.
   Runner-up (civic agenda watcher) rejected: shipped prior art (ZoningAlert.ai, Muniwatch, CivicLens,
   Tribune/TreeHacks 2026) and only 3 of 86 agenda items carry an address.
2. **Outside voice: codex unavailable** (usage limit until after the deadline). Grok (xAI) and an
   independent Opus adversary used instead. Grok verdict: BUILD-WITH-CHANGE.
3. **Cut from MVP (grok):** drill mode, AgentCore Memory, EventBridge Scheduler -> Lambda lane.
   Coordinator decisions live in SQLite (rule 14 still holds). README states "schedule-ready" without
   claiming a scheduled run unless one is observed in CloudWatch.
4. **AgentCore lane = Runtime only** (CodeZip, no Docker), hard-capped, honest "deploy-ready" fallback.
5. **Dashboard is the system of record.** FastAPI owns SQLite. AgentCore Runtime data tools call the
   dashboard REST API with a shared secret; locally the same tools call the Store in-process.
   `AGENT_BACKEND` is a transport switch, never a storage switch.
6. **Fixture-pinned scan.** "Run daily scan" = pinned RSS snapshot (2026-09-11) merged with the live
   feed, so the hero MATCH reproduces on every run and on camera; live items are additive.
7. **Email path:** in-app agency inbox mirror is the primary demo path; SES sandbox to verified inboxes
   only if the AWS account verifies in time; Resend optional. Never production SES.
8. **UI budget raised:** four surfaces (ledger, run/stream, case with ping + agency grid + inbox mirror,
   audit packet) plus judge login; DESIGN.md written first; screenshots reviewed by eye.
9. **Integration + second video take** are budgeted lines, not slack.

## 2026-09-11 (late, after the Opus adversary review)

10. **builder.aws content lane added (3h):** up to +0.6 bonus (3 posts x 0.2) per the official rules, for
    submissions that advance to Stage Two. Needs only the free AWS Builder ID. Three short posts drafted
    from the build itself: the measured openFDA lag and why the press feed leads; the fda.gov abuse wall
    and the browser-UA fix; the hook-enforced human-approval invariant in Strands.
11. **Judges may score from materials alone** (rules: "not required to test the Project"). README,
    architecture diagram, and video get more hours than the brief gave them. Video moves to Sunday
    evening with Monday morning reserved for a re-record. Video must be PUBLIC on YouTube (rules), not unlisted.
12. **Public demo, no judge login.** Read is open; expensive actions are rate-limited and behind a daily
    LLM budget guard, plus a "Reset demo" control. Testing instructions in the Devpost text.
13. **Name the Strands primitives explicitly** in README and the video (Agent, agents-as-tools,
    structured_output_model, BeforeToolCallEvent.cancel_tool hook, stream_async). The 12-line approval
    hook is shown on camera as the technical artifact.
14. **Keep the 9/04 shipment** (two days after the 9/02 press release). It is not a narrative own-goal,
    it is the thesis: the alert reached the food bank; nobody cross-checked the ledger. Say it on camera.
15. **Do not script live-feed numbers** ("20 items, 14 food"); the UI shows whatever the feed says. Do
    not script the openFDA 404 as a hard beat; show the linked record whatever its state.
16. **Rule count is 15**, not 14. Registered participants: 9,358 (registrations, not entries).
17. **Bedrock quota state (2026-09-11 23:40 ET):** account "being verified"; Service Quotas shows applied
    tokens-per-day = 0 for every model (Claude and Nova alike), "not adjustable". Expected to lift with
    verification. If Bedrock is still unusable by the Sunday-noon gate, the deployed AgentCore Runtime
    runs Claude via OpenRouter through an AgentCore API-key credential (same OpenAIModel code path as
    local), and the README says so in one sentence. Never block the build on Bedrock.

## 2026-09-12 (Sat, ~03:45 ET): public host findings

18. **fda.gov walls Render's egress.** Every non-fixture press page came back blocked on the public host
    (the abuse wall the refuters predicted for datacenter IPs). Fix shipped: all 20 pinned-feed pages are
    committed fixtures (`data/fixtures/press/index.json`), so the demo scan never touches fda.gov; new
    live items that are blocked are counted as "blocked, paste to open", not as errors. Paste intake works.
19. **Render free tier is ephemeral.** A redeploy or a spin-down restart loses the SQLite file; the app
    self-seeds the ledger on boot, cases do not survive. A GitHub Actions ping every 14 minutes keeps the
    instance warm. Durable state needs the Starter plan plus a persistent disk (~$7.25/mo): the operator's call.
20. **The deployed model is Claude Sonnet via OpenRouter**, budget-capped at 4 agent runs per day until
    Bedrock quota is raised or the OpenRouter balance is topped up. Stated in the README.

## 2026-09-12 (Sat, ~10:30 ET): documentation audit

21. **Docs audited against the code by a fresh-context reviewer** (38 findings). Root cause: the README
    was written from the build brief before the code landed. Closed by making claims true where the
    change was small (AGENT_BACKEND now selects the Runtime's store transport, dead `prompt` mode and
    dead config removed, a HOLD release control on the ledger, a specific "fda.gov refused this host"
    error for blocked URL intake, the unused Class III digest helper deleted) and by rewording the rest
    to exactly what the code does (rules 2, 10, 11, 12, 14, 15; SES and Resend are stubs; "one decision"
    on the ambiguous path is one question plus one approval; the keep-alive cron is the only schedule;
    the mutation pass ran in a scratch directory). Every route, rate limit, threshold, and env var is
    now in the README. Standing rule: never claim in the README what a judge cannot find in the repo.
