# Devpost submission text

**Project name:** Recall Relay
**Tagline:** Every recall, every pantry, with proof.
**Track:** Good Neighbor Agents
**Built with:** Strands Agents 1.55, bedrock-agentcore (BedrockAgentCoreApp entrypoint, packaged with the AgentCore CLI CodeZip build), Claude Sonnet 4.6 (Bedrock path in code; the live demo reaches it through an OpenAI-compatible endpoint), FastAPI, SQLite, Python 3.12

## Inspiration

When the FDA posts a food recall, Feeding America emails every member food bank. That part works. What happens next is one coordinator, a spreadsheet, and a network of volunteer pantries, some of which open once a month. The recall that this demo is built on is real: on September 2, 2026 a lot of Great Value frozen berries sold at Walmart in 27 states including Florida was recalled for E. coli. Walmart retail rescue is how a lot of frozen food reaches the pantries near me in South Dade. In the seeded ledger, a shipment of those berries went out to a pantry two days after the press release, because nobody cross-checked the ledger. Recall Relay exists so that never happens again, and so the shelf sign for the next family through the pantry door actually gets printed.

## What it does

Recall Relay runs a food bank's recall procedure across its partner pantries, end to end:

1. Intakes a recall from a forwarded alert, a pasted URL or text, a PDF, the FDA press-release feed, or the weekly openFDA ledger.
2. Matches it against the receiving ledger and distribution log. Deterministic scoring narrows the ledger; a Matcher agent returns a typed verdict: MATCH, NO_MATCH, or NEEDS_HUMAN. Every dismissal is logged, nothing is pinged.
3. Builds the pull list: cases on hand (tagged HOLD automatically) and cases shipped to which pantry on which date, with the missing-lot rule applied and explained.
4. Asks the coordinator for one decision, with the pull list, the agency notices, and the client shelf signs already drafted.
5. Relays the notices on approval, with four one-click replies for each pantry. "Already distributed" triggers a shelf sign in English, Spanish, and Haitian Creole.
6. Chases silence: a reminder, then an escalation with a call script. It never marks a pantry confirmed on its own.
7. Closes the case with an audit packet: source, matched rows, pull list, every send and reply with timestamps, approvals, elapsed time.

Fifteen domain rules are encoded as code paths, not prompt lines, from "a lot code only narrows a match, never widens a miss" to "the disposition instruction is copied verbatim, never invented".

## How we built it

- **Strands Agents.** One orchestrator `Agent` whose `@tool` functions run the procedure; the Matcher and the Notice writer are agents-as-tools with Pydantic `structured_output_model` outputs; `stream_async` feeds the live run log. Hooks enforce the invariants: an `AuditHook` writes a row for every tool call, and an `ApprovalGuard` cancels the send tool on `BeforeToolCallEvent` unless the case carries an approval timestamp. A prompt can be talked out of a rule; a hook cannot.
- **Packaged for Amazon Bedrock AgentCore Runtime**, CodeZip build, no container: the entrypoint, config, and package are in the repo and validated with the AgentCore CLI; the Runtime was not launched before the deadline because the hackathon AWS account was created on the final weekend. The runtime is stateless by design; the dashboard owns the SQLite register and the runtime reaches it over an authenticated API, so the same agent code runs locally, on the web host, and in the package.
- **Deterministic intake with no model in the loop:** the FDA press pages are parsed for products, UPCs as printed, lots, best-by dates, states, and the disposition sentence. An extractor agent runs only when that parse leaves a gap.
- **A dashboard made of paper.** The product's output is notices, shelf signs, and an audit packet, so the interface is cream paper, ink, hairlines, and one rationed accent for the decision.

## Challenges we ran into

- openFDA is a ledger, not a feed: we measured a median 33-day lag from recall initiation to the public record, and the demo recall returned zero rows nine days after its press release. The press feed leads; openFDA enriches.
- fda.gov redirects non-browser clients, and every request from a datacenter host, to an apology page. The fetch tool detects the wall and never parses the apology; the pinned feed's pages ship as fixtures, and a blocked live item is counted as blocked so the coordinator can paste the notice.
- UPCs in recall notices are printed six different ways. UPC is evidence, never a join key.
- Real ledgers rarely have lot codes on retail-rescue rows. Missing lot widens the match; it never drops it.

## Accomplishments

- A complete loop, not a chat box: intake, match, pull list, one approval, relay, chase, audit.
- Every judgment call is typed, every send is gated by a hook, every action is on the record.
- Tests with planted defects in every module; the seed builder ran 18 mutations against the rules and store and every one turned the suite red.

## What we learned

Where the model belongs. It adjudicates ambiguity and writes prose from typed fields. Everything that can be a rule is a rule.

## What's next

Case-label photo to ledger row (where a small food bank's lot data comes from), SES production sending, FSIS coverage when the API opens, and multi-tenant onboarding for a food bank's whole agency network.

## Honesty

The recalls are real. The food bank, its twelve agencies, and its ledger are seeded and say so on screen. The daily scan merges a pinned feed snapshot with the live feed so the demo recall reproduces after it scrolls off the FDA feed. Email is mirrored in-app; no production sending. Meat and poultry recalls arrive only by paste. Everything measured is in the README with the query that measured it.

## Testing instructions for judges

See the README section "Try it". The live demo is public; expensive actions are rate-limited and behind a daily budget. https://recall-relay.onrender.com (free instance; the first request after idle can take a minute).
