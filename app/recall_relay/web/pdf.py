"""The audit packet as a PDF: the same numbers as the HTML page, printed plain.

Black on white, hairlines, a serif body and a monospace column for codes and the timeline. No colour, no
logo, no chrome -- this is the page that gets stapled into a binder or emailed to a state inspector, and
it has to survive being photocopied.
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

HAIR = colors.Color(0.6, 0.6, 0.6)
RULE = colors.black

BODY = ParagraphStyle(
    "body", fontName="Times-Roman", fontSize=9.5, leading=13, alignment=TA_LEFT, textColor=colors.black
)
LEAD = ParagraphStyle("lead", parent=BODY, fontSize=11, leading=15)
H1 = ParagraphStyle("h1", parent=BODY, fontName="Times-Bold", fontSize=17, leading=20, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=BODY, fontName="Times-Bold", fontSize=11.5, leading=14, spaceBefore=14, spaceAfter=4)
EYEBROW = ParagraphStyle(
    "eyebrow", parent=BODY, fontName="Courier", fontSize=7, leading=10, textColor=colors.Color(0.35, 0.35, 0.35)
)
MONO = ParagraphStyle("mono", parent=BODY, fontName="Courier", fontSize=7.5, leading=10)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10.5)
CELL_MONO = ParagraphStyle("cellmono", parent=MONO, fontSize=7.5, leading=10)


def _p(text: Any, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(escape(str(text if text not in (None, "") else "—")), style)


def _table(header: list[str], rows: list[list[Any]], widths: list[float], mono_cols: tuple[int, ...] = ()) -> Table:
    data = [[Paragraph(escape(h.upper()), EYEBROW) for h in header]]
    for row in rows:
        data.append([_p(cell, CELL_MONO if i in mono_cols else CELL) for i, cell in enumerate(row)])
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, RULE),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, HAIR),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _kv(pairs: list[tuple[str, str]], width: float) -> Table:
    data = [[Paragraph(escape(k.upper()), EYEBROW), _p(v, CELL)] for k, v in pairs]
    table = Table(data, colWidths=[1.5 * inch, width - 1.5 * inch], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Courier", 7)
    canvas.setFillColor(colors.Color(0.35, 0.35, 0.35))
    label = getattr(doc, "_packet_label", "")
    canvas.drawString(0.75 * inch, 0.55 * inch, label)
    canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.55 * inch, f"page {doc.page}")
    canvas.setStrokeColor(HAIR)
    canvas.setLineWidth(0.25)
    canvas.line(0.75 * inch, 0.72 * inch, LETTER[0] - 0.75 * inch, 0.72 * inch)
    canvas.restoreState()


def render_packet_pdf(view: dict, *, title: Optional[str] = None) -> bytes:
    """Render the packet view model (see web/views.packet_view) to PDF bytes."""
    case = view["case"]
    buffer = BytesIO()
    width = LETTER[0] - 1.5 * inch
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.9 * inch,
        title=title or f"Recall Relay audit packet {case.id}",
        author=view.get("food_bank", "Recall Relay"),
        subject=f"Recall {view.get('recall_number', '')}",
    )
    doc._packet_label = f"{case.id} · recall {view.get('recall_number', 'pending')} · {view.get('food_bank', '')}"

    story: list[Any] = []

    # ------------------------------------------------------------- title block
    story.append(_p(f"AUDIT PACKET{' · DRILL' if case.is_drill else ''}", EYEBROW))
    story.append(Paragraph(escape(view["headline"]), H1))
    story.append(
        _p(
            f"Case {case.id} · Recall {view['recall_number']} · {view['classification']} · "
            f"Status {view['status_label']} · Filed {view['generated_at']}",
            MONO,
        )
    )
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.75, color=RULE, spaceAfter=10))

    story.append(
        _kv(
            [
                ("Food bank", view.get("food_bank", "")),
                ("Firm", case.notice.firm),
                ("Reason", case.notice.reason),
                ("Notice seen", case.notice.source_seen_at.strftime("%Y-%m-%d %H:%M")),
                ("Sources", "\n".join(view["sources"]) or "pasted without a URL"),
                ("Disposition, verbatim", view["disposition_verbatim"] or "not stated in the notice"),
                ("Notices sent", str(view["notices_sent"])),
                (
                    "Responses",
                    f"{len(view['responses'])} of {len(view['responses']) + len(view['outstanding'])}"
                    + (f" · outstanding: {', '.join(view['outstanding'])}" if view["outstanding"] else ""),
                ),
                ("Elapsed, notice to full trace", view["elapsed"] or "still open"),
                ("Closed", view["closed_at"] or "open"),
            ],
            width,
        )
    )

    # ---------------------------------------------------------- matched rows
    story.append(Paragraph("Matched ledger rows", H2))
    if view["matched_rows"]:
        story.append(
            _table(
                ["Receipt", "Received", "Donor / channel", "Product", "Lot", "Cases", "Shipped", "On hand"],
                [
                    [
                        f"{row['receipt'].id:04d}",
                        row["receipt"].received_at,
                        row["receipt"].donor,
                        " ".join(x for x in (row["receipt"].brand, row["receipt"].product, row["receipt"].size) if x),
                        row["receipt"].lot or "none on the receipt",
                        row["receipt"].cases,
                        row["shipped"],
                        row["on_hand"],
                    ]
                    for row in view["matched_rows"]
                ],
                [0.55 * inch, 0.7 * inch, 1.15 * inch, 1.9 * inch, 0.9 * inch, 0.45 * inch, 0.5 * inch, 0.5 * inch],
                mono_cols=(0, 1, 4, 5, 6, 7),
            )
        )
    else:
        story.append(_p("No ledger row was matched to this notice.", BODY))

    # -------------------------------------------------------------- pull list
    if view["pull_rows"]:
        story.append(Paragraph("Pull list", H2))
        pull = view.get("pull_list")
        if pull is not None:
            story.append(
                _p(
                    f"On hand {pull.on_hand_cases} · agencies {len(pull.agencies_affected)}"
                    + (" · widened: the receipt carried no lot, so the whole line is affected"
                       if pull.widening_applied else ""),
                    MONO,
                )
            )
            story.append(Spacer(1, 4))
        story.append(
            _table(
                ["Receipt", "Where", "Cases", "Shipped", "Lot", "Note"],
                [
                    [
                        f"{row['item'].receipt_id:04d}",
                        "on hand (HOLD)" if row["on_hand"] else row["where"],
                        row["item"].cases,
                        row["item"].shipped_at or "—",
                        row["item"].lot or "—",
                        " · ".join(
                            bit
                            for bit in (
                                "" if row["item"].lot_known else "no lot: whole line affected",
                                "physical sort required" if row["item"].physical_sort_required else "",
                                row["item"].note or "",
                            )
                            if bit
                        ),
                    ]
                    for row in view["pull_rows"]
                ],
                [0.55 * inch, 1.7 * inch, 0.45 * inch, 0.7 * inch, 0.9 * inch, 2.4 * inch],
                mono_cols=(0, 2, 3, 4),
            )
        )

    # -------------------------------------------------------------- responses
    story.append(Paragraph("Responses", H2))
    if view["responses"]:
        story.append(
            _table(
                ["At", "Agency", "Answer", "Cases", "Their words"],
                [
                    [r["at"], r["agency_name"], r["label"], r["count"] if r["count"] is not None else "—", r["free_text"] or "—"]
                    for r in view["responses"]
                ],
                [1.05 * inch, 1.7 * inch, 1.2 * inch, 0.5 * inch, 2.25 * inch],
                mono_cols=(0, 3),
            )
        )
    else:
        story.append(_p("No agency has answered yet.", BODY))

    # -------------------------------------------------------------- followups
    if view["followups"]:
        story.append(Paragraph("Follow-ups", H2))
        story.append(
            _table(
                ["Agency", "Kind", "Due", "Sent"],
                [[f["agency_name"], f["kind"], f["due"], f["sent"] or "not sent"] for f in view["followups"]],
                [2.2 * inch, 1.1 * inch, 1.7 * inch, 1.7 * inch],
                mono_cols=(2, 3),
            )
        )

    # -------------------------------------------------------------- approvals
    story.append(Paragraph("Approvals", H2))
    for line in view["approvals"]:
        story.append(_p(line, CELL))

    # --------------------------------------------------------------- timeline
    story.append(PageBreak())
    story.append(Paragraph("Timeline", H2))
    story.append(_p(f"{len(view['timeline'])} events", EYEBROW))
    story.append(Spacer(1, 4))
    if view["timeline"]:
        story.append(
            _table(
                ["At", "Actor", "Event", "Detail"],
                [[e["at"], e["actor"], e["kind"], e["detail"]] for e in view["timeline"]],
                [1.05 * inch, 0.7 * inch, 1.25 * inch, 3.7 * inch],
                mono_cols=(0,),
            )
        )
    else:
        story.append(_p("No events recorded.", BODY))

    story.append(Spacer(1, 14))
    story.append(
        KeepTogether(
            [
                HRFlowable(width="100%", thickness=0.25, color=HAIR, spaceAfter=6),
                _p(
                    "Filed by Recall Relay. Every line in this packet is a stored event, not a reconstruction: "
                    "the timeline is the event log, the responses are what the agencies actually clicked, and the "
                    "disposition is copied from the recall notice word for word.",
                    EYEBROW,
                ),
            ]
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


__all__ = ["render_packet_pdf"]
