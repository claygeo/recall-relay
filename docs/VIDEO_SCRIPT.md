# Recall Relay, video bullets (read these; the screen does the talking)

Target 4:30. Screen recording of the live app, one take per section, trim after. Voice: plain, unhurried. Say the numbers on screen, not numbers from memory.

## 0:00 Problem (screen: the real FDA press page for Great Value Organic Triple Berry Blend)
- This is a real recall. September 2. Great Value frozen berries, one lot, E. coli, sold at Walmart in 27 states including Florida.
- I live in Cutler Bay, south of Miami. Walmart retail rescue is how a lot of frozen food reaches the pantries near me. Some of them open once a month.
- Feeding America already emails every food bank when this happens. That part works.
- What happens next is one person, a spreadsheet, and 50 to 500 partner pantries. Nobody reaches the family that already took the bag home.

## 0:30 Who it's for (screen: Ledger tab, scroll slowly)
- Recall Relay is for the coordinator at a food bank and the volunteer pantries downstream of them.
- This is the receiving ledger. Sixty rows. Store brands, blank UPCs, lots on about a third of the rows. This is what real intake looks like.
- Receipt 17: thirty cases of those berries, received August 24. No lot code on the receipt. Twenty-two cases already went out to three pantries.
- One of those shipments went out September 4. Two days after the press release. Because nobody cross-checked. That is the whole problem.

## 1:00 The run (screen: Run tab, click Run daily scan, let it stream)
- Every morning it walks the FDA feed and the weekly openFDA ledger and scores every notice against the ledger.
- Watch the tally. Most recalls are dismissed with a reason. Non-food is skipped. Nothing pings a human.
- The Matcher is a Strands agent with a typed verdict: match, no match, or needs a human. It only ever sees the top candidates, never the raw ledger.
- Same brand, different product: no match. Same product, different size: no match. An unbranded blueberry receipt against the same firm's July recall: needs a human. That one waits for me.
- One case is ready for a decision.

## 1:45 The one decision (screen: the case page, read the ping)
- This is the entire human workload. Read it off the card.
- No lot on the receipt, so all thirty cases are treated as affected. Eight on hand, already on hold. Twenty-two shipped to three pantries. One of them distributes the same day it receives.
- Pull list, three notices, three shelf signs, all drafted. The disposition line is copied word for word from the FDA notice. The agent is not allowed to invent it.
- Click Approve.

## 2:20 The relay (screen: Inbox tab)
- This is what the pantries see. Every message the system sends is mirrored here.
- Each notice has the recall number, the product, the lot, the reason, what to do, and four one-click replies.
- Click "We pulled it" on the first pantry. Enter six. Back on the case, the grid flips.
- Click "Already distributed" on the same-day pantry. That triggers the shelf sign, in English, Spanish, and Haitian Creole. That sign is the hop nobody serves: the household that already took the box home.

## 3:00 The chase (screen: case page, demo clock)
- The third pantry hasn't answered. Advance the demo clock 24 hours and run follow-ups: a reminder goes out.
- Advance to 48 hours: the coordinator gets an escalation with the pantry's phone number and a call script.
- It never marks a pantry confirmed on its own. Silence is not a yes.

## 3:30 Close, audit, memory (screen: close case, open the packet)
- Close the case. This is the audit packet: source, matched rows, pull list, every send and reply with timestamps, the approval, and the elapsed time. This is what the auditor asks for.
- Back on the blueberry case: tell it that row was Driscoll's, not the recalled firm. Run again. It doesn't ask twice.

## 4:00 How it's built (screen: architecture diagram, then hooks.py)
- Strands Agents: one orchestrator with tools, two agents-as-tools with structured outputs, hooks.
- This file is the part I want you to see. Twelve lines. If a case has no approval timestamp, the send tool is cancelled before it runs. A prompt can be talked out of a rule. A hook can't.
- Deployed on Amazon Bedrock AgentCore Runtime with the CodeZip build, no container. The dashboard is the system of record; the runtime is stateless and calls it over an authenticated API.
- Honesty: the FDA data is live, the food bank is seeded and says so on screen. openFDA lags a median of 33 days, so the press feed leads. Meat and poultry recalls come in by paste. All of it is in the README.

## 4:25 Close (screen: README top)
- Drop in your receiving and distribution CSVs. Every recall, every pantry, with proof.
- Repo, live link, Apache license. Thanks.
