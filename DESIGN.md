# Design System — Recall Relay

## Product Context
- **What this is:** an operational dashboard for one food-safety coordinator at a food bank, plus the printed artifacts the agent produces (agency notices, client shelf signs, the audit packet).
- **Who it's for:** non-technical coordinators and volunteer pantry contacts. Judges will also click through it once.
- **Space:** food banks, nonprofits, public health. Civic trust, not startup gloss.
- **Project type:** server-rendered web app (FastAPI + Jinja + vanilla JS, one SSE stream). Printable pages must print well.

## Aesthetic Direction
- **Direction:** The recall binder. The product emits paper (notices, signs, an audit packet), so the interface is made of paper: warm cream ground, ink-black type, hairline rules, one rationed accent. A calm control room for people who are not technical.
- **Decoration level:** Minimal and typographic. No gradients, no glass, no glow, no icons-for-everything. Structure comes from rules, spacing, and type, the way a good ledger does.
- **Mood:** Studious, warm, unhurried, precise. The screen should feel like a well-kept binder, not a SaaS trial.
- **References (private taste archive):** Cereal Magazine archive index (cream paper, one serif, accession numbers as information scent, hairline rules); Fontshare (chrome tuned quieter than the content; counts as typographic elements); teenage engineering "Mr. Update" (spec-sheet rigor, one rationed industrial accent for functional marks only); Printable Mockups (skin the page in the material of the product's output).

## Typography
- **Text (headings and body):** Source Serif 4 (Google Fonts, variable, optical sizes). Legible at 14px, has character at 32px. One serif does headings, body, table text, and the printed artifacts.
- **Data, eyebrows, and codes:** IBM Plex Mono. Case ids (`RC-2026-0911-001`), recall numbers (`H-1181-2026`), lots, UPCs as printed, dates in tables, counts, the run log. `tabular-nums` always. Eyebrow labels: 11px, uppercase, 0.08em tracking.
- **Controls:** IBM Plex Sans 500 for buttons and form labels only. Nothing else uses the sans.
- **Loading:** `https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap`
- **Scale (1.25 major third, base 15px):** 11 (eyebrow/mono caption) · 13 (table cell, metadata) · 15 (body) · 19 (lead, card title) · 24 (section heading) · 30 (page heading) · 38 (one per page, the case headline). Line-height 1.45 body, 1.2 headings.

## Color
- **Approach:** Paper and ink. Color is rare and means something.
- **Ground (page):** `#F5F2E9` warm cream (paper).
- **Sheet (cards, tables, printed artifacts):** `#FDFCF7` near-white paper on the cream ground. Never pure `#FFFFFF`.
- **Ink (primary text):** `#1C1B18`.
- **Ink secondary:** `#5E5A50` (metadata, table secondary cells).
- **Ink muted:** `#9A958A` (placeholders, disabled, hairline captions).
- **Rule (hairlines):** `#D9D3C4` at 1px. Stronger rule `#B8B1A0` for table header bottoms and card edges.
- **Accent, one only:** `#C8471B` vermilion. Spent ONLY on: HOLD tags, the single approve control, the decision ping marker, "needs human" markers, and the live dot in the run log. Never on headings, never as a background fill larger than a tag.
- **Semantic (small marks, never fills):** confirmed `#2F6B3A` (ink green), warning `#9A6A00` (ochre), error `#8C2B1F` (dried red), info `#3D5A80` (slate).
- **Print:** black on white; accent prints as black; hairlines stay.
- **Dark mode:** none. This is paper.

## Spacing
- **Base unit:** 4px. Scale: 4, 8, 12, 16, 24, 32, 48, 64.
- **Density:** comfortable for cards, dense for tables (row height 32px, cell padding 6px 10px). Ledger tables should look like ledgers.
- **Page gutter:** 32px desktop, 16px mobile. **Max content width:** 1200px. Tables may scroll horizontally inside their sheet.
- **Buttons:** 36px height; the approve button 44px.
- **Section gap:** 48px.

## Layout
- **Approach:** a binder with tabs. A slim top bar (wordmark left in serif, tab-block nav: Ledger · Run · Cases · Packet · Inbox), then one column of sheets. No sidebar.
- **Sheets:** a sheet is a near-white rectangle with a 1px rule border and 2px radius, 24px padding. Sheets carry an eyebrow (mono) and a serif title. Content sits on the sheet like it was typed there.
- **Ledger view:** one wide sheet, dense table, sticky header, HOLD tags inline in the row, receipt id as the accession number in mono in the first column.
- **Run view:** a two-column sheet on desktop: left, the tally as large mono numbers with serif captions (scanned · skipped · dismissed · needs human · pinged); right, the live log as a mono list with a vermilion live dot while streaming. The tool calls stream in as they happen.
- **Case view:** the decision card is one sheet at 38px headline (the product), the verbatim ping paragraph in serif, then the pull list table, then the agency grid (agency · cases · shipped · status · last response · next follow-up), then the drafted notices as paper previews. The approve control is the only vermilion button on the page.
- **Inbox mirror:** the agency's view, styled as an email client would render plain text: subject line in serif, body in serif, the four one-click response links as underlined text, not buttons.
- **Packet:** a print-first page: title block, sources, matched rows, pull list, every send/response/follow-up in a mono timeline, approvals, elapsed time. `@media print` hides the top bar.
- **Breakpoints:** mobile (<720), desktop (>=720). Tables scroll; cards stack.
- **Border radius:** 2px on sheets and tags. Nothing rounder.

## Motion
- **Approach:** almost none. State changes: 150ms ease-out opacity/border. The run log appends without animation. The live dot pulses at 1.2s while a run is streaming and stops when done. Respect `prefers-reduced-motion` by disabling the pulse.
- No skeletons, no spinners, no entrance animations. Content is there when you arrive; the run log is the only thing that "loads".

## Component Patterns
- **Tags:** mono 11px uppercase, 1px border, 2px radius, 2px 6px padding. HOLD = vermilion border and text on sheet. Status tags in ink with a semantic-colored leading dot.
- **Tables:** hairline rows, header in mono eyebrow style, first column mono id, numbers right-aligned tabular. Zebra striping never; hairlines do the work.
- **Buttons:** primary = ink fill, cream text, 2px radius, Plex Sans 500. Approve = vermilion fill, cream text. Secondary = transparent, 1px ink border. Ghost = underlined text link.
- **Eyebrow + title:** every sheet starts with `EYEBROW` (mono) over a serif title. Counts are typographic: "Receipts 60", "Agencies 12".
- **The decision ping:** a sheet with a vermilion 4px left rule, the verbatim ping text in 19px serif, Approve / Edit / Dismiss beneath. This is the one place vermilion is allowed to be a rule rather than a tag.
- **Printed artifacts:** notices and signs render inside a sheet at document proportions (max 680px wide), serif body, mono header block (recall number, class, product, lots), a hairline between header and body. Shelf signs in three languages are three sheets side by side on desktop.
- **Empty states:** one serif sentence, no illustration.

## Anti-patterns (never do these)
- Pure white backgrounds, pure black text, or any gradient.
- Purple, violet, or blue accents. Colored card fills. Badges in five colors.
- Rounded pill buttons, drop shadows deeper than 1px hairline, glassmorphism.
- Icon libraries. If a mark is needed, it is a typographic mark (·, →, ✓ in the text face).
- Sidebars, stat tiles with big colored numbers, sparkline decoration.
- Centered hero sections, stock photography, illustration.
- Spinners or skeleton loaders.
- Any component that reads as a default AI-generated admin template.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-11 | Paper-and-ink direction | The product's output is paper (notices, signs, an audit packet). The taste archive's strongest transplantable idea was to skin the interface in the product's output material. Also separates the project from every dark-mode dashboard in the field. |
| 2026-09-11 | Source Serif 4 + IBM Plex Mono + Plex Sans (controls only) | One legible serif with optical sizes carries screen and print; Plex Mono gives codes and counts the ledger feel; the sans is confined to controls so the page never reads as a SaaS template. |
| 2026-09-11 | One accent, vermilion, rationed | teenage engineering's rule: the single industrial accent is spent only on functional marks (HOLD, approve, needs-human, live). A coordinator's eye goes straight to the decision. |
| 2026-09-11 | No dark mode | Paper. Also removes a whole class of contrast bugs before a deadline. |
