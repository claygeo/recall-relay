# Recall Relay, the 75-second video

Method: record the screen first with every click already in it, then read the lines over the playback in one sitting. Never narrate while clicking. Pause a full second at every period. 165 words at a calm pace lands at 70 to 80 seconds. Upload to YouTube as public.

Before recording: reset the demo, do one full dry run on https://recall-relay.onrender.com (the daily agent budget is small, so do not burn runs), and make sure the shelf sign actually renders on screen when you click "Already distributed".

| Screen | Say |
|---|---|
| The real FDA press page, scrolled to the "27 states" line | A real recall, September 2. Great Value frozen berries, E. coli, Walmart in 27 states including Florida. I live in Cutler Bay. Walmart retail rescue stocks the pantries near me. |
| Ledger tab, cursor on receipt 17, then the September 4 shipment row | The recall email reaches the food bank. Then it's one coordinator, a spreadsheet, and pantries that open once a month. In this seeded ledger, thirty cases arrived with no lot code, so every case is suspect. One shipment left two days after the press release. |
| Run tab, scan streaming, dismissals scrolling, one case surfacing | Recall Relay runs the procedure. It checks the FDA feed against the ledger, dismisses everything that never touched it, and stops at the one decision a human owes. |
| Case page, click Approve as the word lands, then hold one second | One decision. Approve. |
| Inbox: click "Already distributed", let the three-language sign render, then show the reminder and the call script | Every pantry gets one-click replies. Already distributed prints a shelf sign in English, Spanish, and Haitian Creole for the next family through the door. Silence gets a reminder, then a call script. Nothing is confirmed on its own. |
| Close the case, audit packet PDF open, scroll one page | It closes with the audit packet. |
| `app/recall_relay/agents/hooks.py`, the cancel branch highlighted | Built on Strands, packaged for Bedrock AgentCore. A hook cancels the send tool unless a human approved. Code, not a prompt. |
| README header with the live link | Every recall, every pantry, with proof. |

If the AgentCore Runtime is deployed before you record, line seven becomes "deployed on Bedrock AgentCore". Otherwise keep "packaged for". Do not say "every morning" or "live from the FDA" anywhere; the scan is a button that merges a pinned feed snapshot with the live feed.
