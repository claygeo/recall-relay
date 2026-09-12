# The Strands hook that makes human approval unbreakable

*Part 3 of 3 on building Recall Relay for the AWS Agents for Humans hackathon with Strands Agents and Amazon Bedrock AgentCore.*

Recall Relay relays recall notices from a food bank to its partner pantries. Rule 12 of the procedure is the one a coordinator cares about most: **nothing is sent to an agency until a human has approved the case.** The agent drafts everything, asks once, and stops.

You can write that rule into a system prompt. I did. Then I asked what happens on the day the model decides the pull list is obviously right and calls `send_notices` anyway. A prompt can be talked out of a rule. A hook cannot.

## Where Strands puts the seam

Strands Agents exposes the agent loop through hook events. Two matter here:

- `BeforeToolCallEvent` fires before any tool runs, with the tool name and its input. It has a writable `cancel_tool` field: set it to a message and the executor turns the call into an error result carrying that message, and the model sees the refusal.
- `AfterToolsEvent` fires after a batch of tools has run and lets a hook end the turn.

A `HookProvider` registers callbacks for those events and is passed to the `Agent` at construction. That is the whole mechanism.

## The guard

Here is the part of `ApprovalGuard` that enforces rule 12, trimmed only of logging:

```python
SEND_TOOLS = frozenset({"send_notices"})

class ApprovalGuard(HookProvider):
    def __init__(self, store, case_id=""):
        self.store, self.case_id = store, case_id

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.guard)

    def guard(self, event):
        name = (event.tool_use or {}).get("name", "")
        if name not in SEND_TOOLS:
            return
        case = self.store.get_case(self.case_id)
        if case is None or case.approved_at is None:
            event.cancel_tool = (
                f"blocked {name}: case has no approved_at. A human approves before "
                "anything is relayed (rule 12). Call request_approval and stop."
            )
```

Twelve lines that matter. The case register is the source of truth, `approved_at` is a timestamp the model cannot write, and the send tool cannot run without it. The audit trail records every blocked attempt, so if the model ever tries, the coordinator sees it.

## Ending the turn on purpose

The second half of rule 12 is "one decision per case". After the agent calls `request_approval`, the run is over. The model should not get another turn to reconsider, ping twice, or start sending. A second provider, `TerminalToolGuard`, watches `AfterToolCallEvent` for the terminal tools (`request_approval`, `dismiss_case`, `mark_needs_human`) and then sets `event.end_turn` on `AfterToolsEvent`. The system prompt says the same thing; the hook makes it true.

## Why not just trust the prompt

Three reasons, all from building this:

1. **The failure mode is silent.** A model that sends early does not raise an exception. It succeeds. You find out from a pantry.
2. **The guard is testable without a model.** The test builds a case with no `approved_at`, invokes the send tool through the agent with a fake model, and asserts zero rows in the outbox. Then it approves the case and asserts three. The same test with the hook removed goes red. A verifier that has never failed has never been tested.
3. **A judge can read it.** When someone asks "what stops this thing from emailing 400 pantries by mistake," the answer is a file, not a paragraph.

## Two things I got wrong first

I assumed cancelling meant setting `event.selected_tool = None`. It does not; that is the "tool lookup failed" path. The documented cancel is `cancel_tool`, and the difference is visible in `strands/tools/executors`. Read the installed source, not your memory of the docs.

I also put the audit row for a blocked send inside the guard only after a live run showed the block happening with no trace of it. Every refusal now lands in the case's audit trail under `send_blocked`, which is exactly the kind of line an auditor wants to find.

## The shape this gives the whole agent

With the guard in place, the rest of the design got simpler. The orchestrator can be given the send tool, which makes the demo honest: the tool exists, the agent could try, and the hook is what stands in the way. The service layer calls the send deterministically after the coordinator clicks approve, through the same tool, so the hook is exercised on the happy path too.

If you are building an agent that touches other people, find the one action that must never happen without a human, and put it behind a hook. Then write the test that proves the hook bites.

Project: Recall Relay, built with Strands Agents on Amazon Bedrock AgentCore for the AWS Agents for Humans hackathon. Parts 1 and 2 covered why the FDA press feed leads openFDA by a median of 33 days, and the fda.gov abuse wall.
