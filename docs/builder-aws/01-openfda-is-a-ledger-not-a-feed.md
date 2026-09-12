# openFDA is a ledger, not a watch feed: what I measured before building a recall agent

*Part 1 of 3 on building Recall Relay for the AWS Agents for Humans hackathon with Strands Agents and Amazon Bedrock AgentCore.*

I am building an agent that runs a food bank's recall procedure across its partner pantries. The first design question was simple: where does the agent learn about a recall? The obvious answer is openFDA, the FDA's public JSON API. It is keyless, well documented, and returns structured recall records with a recall number, a classification, lot codes, and a distribution pattern.

It is also the wrong place to watch.

## What I measured

On September 11, 2026 I queried the food enforcement endpoint:

```
https://api.fda.gov/food/enforcement.json?search=status:"Ongoing"&limit=1
```

The response carried `meta.last_updated: 2026-09-02`. Nine days stale on the day I checked, because the dataset refreshes weekly.

Then I pulled the 100 most recent records and compared `recall_initiation_date` with `report_date`, which is when the record became public in openFDA:

| Lag from initiation to openFDA | Days |
|---|---|
| Minimum | 11 |
| Median | 33 |
| Maximum | 196 |

A median of 33 days. For a food bank, that is a month of distributing product that is already recalled.

The clearest example is the recall my demo is built around. On September 2, Frutas y Hortalizas del Sur S.A. expanded an earlier recall to include one lot of Great Value Organic Triple Berry Blend, 10 oz, sold at Walmart stores in 27 states including Florida, for possible E. coli O145. The FDA press release went up September 3. On September 11, openFDA returned zero rows for it:

```
search=product_description:"Great Value Frozen Organic Triple Berry"  ->  HTTP 404
```

openFDA returns 404 for zero results, which your client has to treat as "nothing yet", not as an error.

The same firm's July recall of Organic Whole Blueberries was in openFDA as `H-1181-2026`, Class I, initiated July 3, reported July 22, with `code_info` "Lot 6040 01 Best by Date Feb 09 2028". Same firm, same lot family as the September expansion. That link is real and useful. It just arrived weeks after the moment it mattered.

## Two more things the data does not give you

**UPCs are not a join key.** In the 100 sampled records, `openfda.upc` was empty in 100. The press release for the Great Value recall prints the UPC as `7874211226`, ten digits, no leading zero, no check digit. Another press release the same week printed `8 38796 00105 1` with spaces. Matching on UPC is matching on how a press officer typed a number.

**Lot codes are prose.** `code_info` is a free-text field. "Lot 6040 01 Best by Date Feb 09 2028" is one string, and the ledger at a food bank rarely captured a lot at all for retail-rescue donations.

## What the agent does instead

Recall Relay orders its sources by freshness, and each one has one job:

1. **The notice the food bank already has.** Feeding America's national office emails Class I and II recalls to member banks. A forwarded alert, a firm letter, or a pasted URL creates the case the moment it exists.
2. **The FDA press-release RSS feed**, polled daily. Same-day. Title, link, and date only, so the agent fetches the press page and parses the header block and the body for products, lots, best-by dates, states, and the disposition sentence.
3. **openFDA, weekly, as enrichment.** It attaches the recall number, the classification, and the distribution pattern when they arrive. It never delays action, and the case records which source created it and when each source was seen.

That ordering is rule 1 of 15 encoded rules in the project, and it exists because of the table above.

The matching side follows from the same measurements: brand plus product line plus size decide, the receipt-date window narrows, and a lot code only ever narrows a match, never widens a miss. When a ledger row has no lot, every case of that product line is treated as affected, and the notice to the pantry says so in one sentence.

## Try it yourself

The measurements above are reproducible with three curl commands and no API key. If you are building anything that reacts to recalls, run them first. openFDA is an excellent ledger. Your watch feed is somewhere else.

*Next: the fda.gov abuse wall, and why a browser User-Agent and a cache belong in an agent's fetch tool.*

Project: Recall Relay, built with Strands Agents on Amazon Bedrock AgentCore for the AWS Agents for Humans hackathon. Repository and demo linked from the submission.
