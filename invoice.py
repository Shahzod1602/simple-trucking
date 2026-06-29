"""PDF Invoice generator for trucking loads."""
import io
from datetime import datetime, timezone

import database
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)


def _p(text: str, style) -> Paragraph:
    return Paragraph(str(text or ""), style)


def generate_invoice(load: dict, company: dict, invoice_number: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
    )

    # ── Styles ─────────────────────────────────────────────────────────────
    normal   = ParagraphStyle("n",   fontName="Helvetica",       fontSize=9,  leading=13)
    bold     = ParagraphStyle("b",   fontName="Helvetica-Bold",  fontSize=9,  leading=13)
    small    = ParagraphStyle("sm",  fontName="Helvetica",       fontSize=8,  leading=11, textColor=colors.HexColor("#64748b"))
    title    = ParagraphStyle("t",   fontName="Helvetica-Bold",  fontSize=22, textColor=colors.HexColor("#0f172a"))
    label    = ParagraphStyle("l",   fontName="Helvetica-Bold",  fontSize=7.5, textColor=colors.HexColor("#64748b"), spaceAfter=2)
    value    = ParagraphStyle("v",   fontName="Helvetica",       fontSize=9.5)
    total_s  = ParagraphStyle("tot", fontName="Helvetica-Bold",  fontSize=13, textColor=colors.HexColor("#0f172a"))
    accent   = colors.HexColor("#f59e0b")
    light_bg = colors.HexColor("#fafafa")
    border   = colors.HexColor("#e2e8f0")

    story = []

    # ── Header: INVOICE title + company info ───────────────────────────────
    company_name = company.get("company_name") or "Your Company"
    company_lines = [
        _p(company_name, ParagraphStyle("cn", fontName="Helvetica-Bold", fontSize=11)),
    ]
    if company.get("address"):
        company_lines.append(_p(company.get("address"), small))
    if company.get("phone"):
        company_lines.append(_p(company.get("phone"), small))
    if company.get("email"):
        company_lines.append(_p(company.get("email"), small))
    mc = []
    if company.get("mc_number"):
        mc.append(f"MC# {company['mc_number']}")
    if company.get("dot_number"):
        mc.append(f"DOT# {company['dot_number']}")
    if mc:
        company_lines.append(_p(" · ".join(mc), small))

    header_table = Table(
        [[_p("INVOICE", title), [*company_lines]]],
        colWidths=[3.5 * inch, None],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN",  (1, 0), (1, 0),  "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=2, color=accent, spaceAfter=14))

    # ── Invoice meta (number, date, due) ───────────────────────────────────
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    terms = company.get("payment_terms") or "Net 30"
    meta_data = [
        [_p("Invoice #",      label), _p("Date",         label), _p("Payment Terms", label)],
        [_p(invoice_number,   value), _p(today,           value), _p(terms,           value)],
    ]
    meta_table = Table(meta_data, colWidths=[2.2 * inch, 2.2 * inch, 2.2 * inch])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0),  light_bg),
        ("ROWBACKGROUNDS", (0, 1), (-1, 1), [colors.white]),
        ("BOX",         (0, 0), (-1, -1), 0.5, border),
        ("INNERGRID",   (0, 0), (-1, -1), 0.5, border),
        ("TOPPADDING",  (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 18))

    # ── Bill To ────────────────────────────────────────────────────────────
    bill_to_lines = [_p("BILL TO", label)]
    broker = load.get("broker_name") or "Broker / Shipper"
    bill_to_lines.append(_p(broker, bold))
    story.append(Table([[bill_to_lines]], colWidths=["100%"]))
    story.append(Spacer(1, 16))

    # ── Load details table ─────────────────────────────────────────────────
    load_number   = load.get("load_number") or str(load.get("id", ""))
    origin        = load.get("pickup_address")  or load.get("origin_state")  or "—"
    destination   = load.get("delivery_address") or load.get("destination_state") or "—"
    pickup_date   = load.get("pickup_date")   or "—"
    delivery_date = load.get("delivery_date") or "—"
    miles         = load.get("miles") or "—"
    rate          = load.get("total_rate_usd") or "0"

    # Numeric amounts. 'charge' is the load's billable accessorial/extra charge
    # (shown as a $ amount in the weekly KPI); add it as a line item when > 0.
    rate_amount   = database.parse_money(rate)
    charge_amount = database.parse_money(load.get("charge"))
    total_amount  = rate_amount + charge_amount

    detail_header = [
        _p("Description", bold),
        _p("Load #",      bold),
        _p("Miles",       bold),
        _p("Amount",      bold),
    ]
    description = f"Freight Transportation\n{origin} → {destination}"
    detail_rows = [
        detail_header,
        [
            _p(description, normal),
            _p(load_number, normal),
            _p(str(miles),  normal),
            _p(_money(rate_amount), normal),
        ],
    ]
    if charge_amount > 0:
        detail_rows.append([
            _p("Additional Charges", normal),
            _p("", normal),
            _p("", normal),
            _p(_money(charge_amount), normal),
        ])
    detail_table = Table(
        detail_rows,
        colWidths=[3.4 * inch, 1.3 * inch, 1.0 * inch, 1.5 * inch],
    )
    detail_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#f1f5f9")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white]),
        ("BOX",           (0, 0), (-1, -1), 0.5, border),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, border),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("ALIGN",         (2, 0), (-1, -1), "RIGHT"),
        ("RIGHTPADDING",  (2, 0), (-1, -1), 10),
    ]))
    story.append(detail_table)
    story.append(Spacer(1, 4))

    # ── Pickup / Delivery dates row ────────────────────────────────────────
    dates_table = Table(
        [[_p(f"Pickup: {pickup_date}", small), _p(f"Delivery: {delivery_date}", small)]],
        colWidths=[3.5 * inch, None],
    )
    story.append(dates_table)
    story.append(Spacer(1, 20))

    # ── Total ──────────────────────────────────────────────────────────────
    total_table = Table(
        [
            [_p("Subtotal", normal), _p(_money(total_amount), normal)],
            [_p("TOTAL DUE", ParagraphStyle("td", fontName="Helvetica-Bold", fontSize=11)),
             _p(_money(total_amount), total_s)],
        ],
        colWidths=[5.5 * inch, 1.7 * inch],
    )
    total_table.setStyle(TableStyle([
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("RIGHTPADDING",  (1, 0), (1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE",     (0, 1), (-1, 1), 1, border),
        ("LINEBELOW",     (0, 1), (-1, 1), 2, accent),
    ]))
    story.append(total_table)

    # ── Payment info ───────────────────────────────────────────────────────
    bank_lines = []
    if company.get("bank_name"):
        bank_lines.append(f"Bank: {company['bank_name']}")
    if company.get("bank_account"):
        bank_lines.append(f"Account #: {company['bank_account']}")
    if company.get("bank_routing"):
        bank_lines.append(f"Routing #: {company['bank_routing']}")

    if bank_lines:
        story.append(Spacer(1, 24))
        story.append(HRFlowable(width="100%", thickness=0.5, color=border, spaceAfter=10))
        story.append(_p("Payment Information", label))
        for line in bank_lines:
            story.append(_p(line, small))

    # ── Footer ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 30))
    story.append(HRFlowable(width="100%", thickness=0.5, color=border, spaceAfter=6))
    story.append(_p("Thank you for your business!", ParagraphStyle(
        "footer", fontName="Helvetica-Oblique", fontSize=8, textColor=colors.HexColor("#94a3b8"), alignment=1
    )))

    doc.build(story)
    return buf.getvalue()


def _money(value) -> str:
    """Format a free-text money value (e.g. '$1,675.00' or 1675) into
    '$1,675.00' exactly once. Parsing with database.parse_money() strips any
    existing '$'/commas so we never end up with a doubled dollar sign."""
    return f"${database.parse_money(value):,.2f}"
