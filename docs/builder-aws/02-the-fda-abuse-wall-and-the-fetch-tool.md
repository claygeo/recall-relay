# The fda.gov abuse wall, and what an agent's fetch tool should do about it

*Part 2 of 3 on building Recall Relay for the AWS Agents for Humans hackathon with Strands Agents and Amazon Bedrock AgentCore.*

Part 1 showed why the FDA press-release feed, not openFDA, is the fresh path for recalls. The feed carries only a title, a link, and a date. Everything the agent needs to act on, the products, the lot codes, the best-by dates, the states, the sentence that tells consumers what to do, lives on the press page behind that link.

So the agent has to fetch fda.gov pages. Here is what happened the first time.

## The wall

Fetching a press release with Python's default `requests` User-Agent:

```
GET https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/frutas-y-hortalizas-del-sur-sa-expands-recall-...
User-Agent: python-requests/2.32

302 -> https://www.fda.gov/apology_objects/abuse-detection-apology.html
```

No 403, no error page you would notice in a log. A redirect to a polite apology page, and a 200 at the end of it. If your tool follows redirects and hands the body to a model, the model will happily summarize an apology.

The same URL with a browser User-Agent:

```
200, 43,658 bytes
```

Nothing else changed.

## What the fetch tool does now

The Strands tool that fetches press pages in Recall Relay is small, and each line is there because of something that went wrong:

1. **A real browser User-Agent on every request.** Configurable, defaulted to a current Chrome string.
2. **Abuse-wall detection.** After following redirects, if the final URL or the body contains `abuse-detection-apology`, the tool returns `blocked=True` instead of a body. It never raises, because a raised exception inside a tool becomes a retry loop inside an agent.
3. **A cache keyed by URL hash.** Successful HTML is written to disk and served from disk first. The press page for the demo recall is checked into the repository as a fixture, so a fetch failure during judging cannot take the demo down.
4. **A NEEDS_HUMAN path.** When a page is blocked and not cached, the case is created anyway with the link and the feed title, marked "needs human", and the coordinator is asked to paste the notice text. The agent does not guess a lot code because a page did not load.

That last point is a design rule for the whole project: ambiguity goes to a human, and the human's answer is remembered. It is cheaper than being wrong about a recall.

## Parsing the page without a model

The press pages have a stable header block: Company Announcement Date, FDA Publish Date, Product Type, Reason for Announcement, Company Name, Brand Name(s), Product Description. Below it, the announcement body. A regex pass over the stripped text pulls out:

- UPCs **as printed**, with their spaces and dashes preserved (`7874211226`, `8 38796 00105 1`). They are kept as evidence, never used as a join key.
- Lot and batch strings (`Lot Code 6040 01-6`, `lot S394260`).
- Best-by and use-by strings, verbatim (`Best If Used By February 9, 2028`).
- The distribution sentence, with full state names and two-letter codes normalized (`shipped to select Walmart stores in 27 states including Florida`).
- The disposition sentence (`return it to the place of purchase for a full refund`). This one is copied verbatim into every downstream notice, because inventing disposition instructions is how a recall procedure goes wrong.

Only when that pass leaves a gap, a missing firm, product, reason, or distribution, does an extractor agent run with a typed structured output, under instructions to copy and never invent. Most pages never reach it.

## Where the model belongs

It is tempting to hand the whole page to a model and ask for JSON. The model would do fine most days. But an agent that runs every morning for months will hit a page with three products in a table, a lot code split across a line break, or an apology page. Deterministic extraction with a typed model behind it fails loudly and cheaply. A model-first extractor fails quietly and expensively.

*Next: the twelve lines of Strands hook code that make the human-approval rule unbreakable.*

Project: Recall Relay, built with Strands Agents on Amazon Bedrock AgentCore for the AWS Agents for Humans hackathon.
