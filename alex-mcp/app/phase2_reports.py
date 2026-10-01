"""Project Jarvis Phase 2 deterministic reports and handoff payloads."""
from __future__ import annotations

import csv
import io
import json
from datetime import date

import phase2_finance
import phase2_library
import phase2_work
import compat_tools as tools


def remember_active_report(user_id, conversation_id, report_kind, payload,
                          period=None, spec=None):
    """Remember exactly what this conversation most recently displayed."""
    conn = tools.get_db()
    try:
        conn.execute(
            """INSERT INTO active_report_contexts(
                   user_id,conversation_id,report_kind,period,payload_json,spec_json,created_at_utc
               ) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id,conversation_id) DO UPDATE SET
                   report_kind=excluded.report_kind,
                   period=excluded.period,
                   payload_json=excluded.payload_json,
                   spec_json=excluded.spec_json,
                   created_at_utc=CURRENT_TIMESTAMP""",
            (
                user_id, conversation_id, report_kind, period,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                json.dumps(spec or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_active_report(user_id, conversation_id, max_age_hours=6):
    """Return only the report context from this exact user + conversation."""
    conn = tools.get_db()
    try:
        row = conn.execute(
            """SELECT * FROM active_report_contexts
               WHERE user_id=? AND conversation_id=?
                 AND datetime(created_at_utc) >= datetime('now', ?)
               LIMIT 1""",
            (user_id, conversation_id, f"-{int(max_age_hours)} hours"),
        ).fetchone()
        if not row:
            return None
        return {
            "kind": row["report_kind"],
            "period": row["period"],
            "payload": json.loads(row["payload_json"]),
            "spec": json.loads(row["spec_json"] or "{}"),
            "created_at_utc": row["created_at_utc"],
        }
    finally:
        conn.close()


def finance_query_csv(payload):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "type", "category", "scope", "description", "amount", "currency", "reference"])
    for row in payload.get("records", []):
        writer.writerow([
            row.get("date_local"), row.get("type"), row.get("category"), row.get("scope"),
            row.get("description"), row.get("amount"), row.get("currency"),
            row.get("reference"),
        ])
    return out.getvalue()


def finance_query_text(payload):
    lines = ["ALEX Finance Report", ""]
    for currency, amount in sorted((payload.get("spending_totals") or {}).items()):
        lines.append(f"Spending total: {currency} {amount:.2f}")
    for currency, amount in sorted((payload.get("income_totals") or {}).items()):
        lines.append(f"Income total: {currency} {amount:.2f}")
    lines.append(f"Matching records: {int(payload.get('count') or 0)}")
    lines.extend(["", "Transactions:"])
    for row in payload.get("records", []):
        lines.append(
            f"- {row.get('date_local','')} | {row.get('description','')} | "
            f"{row.get('currency','')} {float(row.get('amount') or 0):.2f} | "
            f"{row.get('category') or ''}"
        )
    return lines


def text_pdf(lines, title="ALEX Report"):
    """Dependency-free multi-page PDF with safe row wrapping and page numbers."""
    def esc(value):
        return _pdf_escape(value)
    wrapped = []
    for line in lines:
        value = str(line)
        if not value:
            wrapped.append("")
            continue
        while len(value) > 92:
            cut = value.rfind(" ", 0, 92)
            cut = cut if cut > 24 else 92
            wrapped.append(value[:cut])
            value = value[cut:].lstrip()
        wrapped.append(value)

    per_page = 43
    pages = [wrapped[i:i + per_page] for i in range(0, len(wrapped), per_page)] or [[]]
    objects = []
    page_ids = []
    font_id = 3
    next_id = 4
    content_ids = []
    for page_index, page_lines in enumerate(pages, 1):
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        content_ids.append(content_id)
        y = 760
        ops = ["BT", "/F1 18 Tf", f"54 {y} Td", f"({esc(title)}) Tj", "ET"]
        y -= 34
        for line in page_lines:
            size = 9 if line.startswith("- ") else 10
            ops.extend(["BT", f"/F1 {size} Tf", f"54 {y} Td", f"({esc(line)}) Tj", "ET"])
            y -= 16
        ops.extend(["BT", "/F1 8 Tf", f"280 30 Td", f"(Page {page_index} of {len(pages)}) Tj", "ET"])
        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        objects.append((page_id, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")))
    objects.extend([
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(page_ids)} >>".encode("ascii")),
        (font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ])
    objects.sort(key=lambda x: x[0])
    out = bytearray(b"%PDF-1.4\n")
    offsets = {0: 0}
    for oid, body in objects:
        offsets[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    max_id = max(offsets)
    out.extend(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        out.extend(f"{offsets.get(oid, 0):010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)



def _human_category(value):
    canonical = str(value or "uncategorised").strip().casefold().replace(" ", "_")
    if canonical == "food":
        return "Food & Drink"
    raw = canonical.replace("_", " ").strip()
    return " & ".join(part.strip().title() for part in raw.split("&"))


def _month_label(period):
    try:
        year, month = (int(x) for x in str(period).split("-", 1))
        return date(year, month, 1).strftime("%B %Y")
    except Exception:
        return str(period or "Current period")


def build_monthly_finance_report(ledger, period, sender_phone,
                                 conversation_type="DIRECT_DM",
                                 requested_scope="all"):
    """One canonical monthly-finance dataset for chat, PDF, CSV and JSON."""
    categories = [
        {
            **row,
            "label": _human_category(row.get("category")),
        }
        for row in (ledger.get("category_totals") or [])
    ]
    obligations = phase2_finance.list_obligations(
        sender_phone, conversation_type, requested_scope, period=period
    )
    goals = phase2_finance.list_goals(
        sender_phone, conversation_type, requested_scope
    )
    title = f"{_month_label(period)} Finance Report"
    summary_lines = []
    for currency, amount in sorted((ledger.get("spending_totals") or {}).items()):
        summary_lines.append(f"Spending — {currency} {float(amount):,.2f}")
    for currency, amount in sorted((ledger.get("income_totals") or {}).items()):
        summary_lines.append(f"Income — {currency} {float(amount):,.2f}")
    for currency, amount in sorted((ledger.get("net_outflow") or {}).items()):
        summary_lines.append(f"Net outflow — {currency} {float(amount):,.2f}")
    display_categories = [
        {
            "number": index + 1,
            "category": row["label"],
            "amount": row["amount"],
            "currency": row["currency"],
            "count": row["count"],
        }
        for index, row in enumerate(categories)
    ]
    return {
        "report_type": "monthly_finance",
        "period": period,
        "scope": requested_scope or "all",
        "ledger": ledger,
        "category_totals": categories,
        "obligations": obligations,
        "goals": goals,
        "display": {
            "title": title,
            "summary": summary_lines,
            "categories": display_categories,
            "transaction_count": int(ledger.get("count") or 0),
        },
    }


def monthly_finance_csv(report):
    """Raw transaction export from the same canonical monthly report dataset."""
    return finance_query_csv(report.get("ledger") or {})


def _pdf_safe_text(value):
    return (
        str(value or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


def _pdf_literal(value):
    return _pdf_escape(_pdf_safe_text(value))


def premium_finance_pdf(report):
    """Render the locked premium ALEX finance document without external deps.

    Layout uses a dark navy identity, amber accents, repeatable transaction
    headers, safe page boundaries and page numbering. The data comes only from
    the canonical report object.
    """
    ledger = report.get("ledger") or {}
    records = list(ledger.get("records") or [])
    categories = list(report.get("category_totals") or [])
    obligations = list(report.get("obligations") or [])
    goals = list(report.get("goals") or [])
    period_label = _month_label(report.get("period"))

    # Page 1 is the executive summary. Transaction pages are deliberately
    # separate so table headers can repeat cleanly on every page.
    tx_per_page = 28
    tx_pages = [records[i:i + tx_per_page] for i in range(0, len(records), tx_per_page)]
    page_kinds = [("summary", None)] + [("transactions", rows) for rows in tx_pages]
    if not tx_pages:
        page_kinds.append(("transactions", []))
    page_count = len(page_kinds)

    objects = []
    page_ids = []
    regular_font_id = 3
    bold_font_id = 4
    next_id = 5

    def text_op(x, y, size, value, bold=False, gray=0.12):
        font = "/F2" if bold else "/F1"
        return [
            f"{gray:.3f} g",
            "BT", f"{font} {size} Tf", f"{x} {y} Td",
            f"({_pdf_literal(value)}) Tj", "ET",
        ]

    def header_ops(page_no):
        ops = [
            "0.047 0.086 0.133 rg", "0 714 612 78 re f",
            "0.886 0.624 0.204 rg", "0 708 612 6 re f",
        ]
        ops += text_op(48, 754, 19, "ALEX", True, 1.0)
        ops += text_op(48, 730, 11, "Monthly Finance Report", False, 0.88)
        ops += text_op(430, 735, 10, period_label, True, 0.95)
        ops += text_op(48, 28, 8, "Private scope is shown separately from expense category.", False, 0.48)
        ops += text_op(500, 28, 8, f"Page {page_no} of {page_count}", False, 0.48)
        return ops

    def money(cur, amount):
        return f"{cur} {float(amount or 0):,.2f}"

    for page_no, (kind, payload) in enumerate(page_kinds, 1):
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        ops = header_ops(page_no)

        if kind == "summary":
            y = 674
            ops += text_op(48, y, 17, f"{period_label} at a glance", True)
            y -= 34
            totals = []
            for cur, amt in sorted((ledger.get("spending_totals") or {}).items()):
                totals.append(("Spending", money(cur, amt)))
            for cur, amt in sorted((ledger.get("income_totals") or {}).items()):
                totals.append(("Income", money(cur, amt)))
            for cur, amt in sorted((ledger.get("net_outflow") or {}).items()):
                totals.append(("Net outflow", money(cur, amt)))
            totals.append(("Transactions", str(int(ledger.get("count") or 0))))
            for label, value in totals[:8]:
                ops += ["0.965 0.965 0.965 rg", f"48 {y-8} 516 27 re f"]
                ops += text_op(60, y, 10, label, False, 0.28)
                ops += text_op(390, y, 11, value, True, 0.08)
                y -= 34

            y -= 8
            ops += text_op(48, y, 13, "Expense breakdown", True)
            y -= 25
            for row in categories[:10]:
                ops += text_op(58, y, 9, row.get("label"), False, 0.22)
                ops += text_op(388, y, 9, money(row.get("currency"), row.get("amount")), True, 0.12)
                y -= 18

            if obligations and y > 170:
                y -= 8
                ops += text_op(48, y, 13, "Bills / obligations", True)
                y -= 23
                for item in obligations[:5]:
                    amount = item.get("expected")
                    detail = "variable" if amount is None else money(item.get("currency") or "MYR", amount)
                    ops += text_op(58, y, 9, f"{item.get('name')}: {detail}", False, 0.22)
                    y -= 18

            if goals and y > 115:
                y -= 8
                ops += text_op(48, y, 13, "Planning context", True)
                y -= 23
                for goal in goals[:4]:
                    ops += text_op(
                        58, y, 9,
                        f"{goal.get('name')}: {money(goal.get('currency') or 'MYR', goal.get('funded'))} / "
                        f"{money(goal.get('currency') or 'MYR', goal.get('target'))}",
                        False, 0.22,
                    )
                    y -= 18
        else:
            y = 674
            ops += text_op(48, y, 16, "Transactions", True)
            y -= 26
            # Repeated table header on every transaction page.
            ops += ["0.110 0.145 0.180 rg", f"48 {y-8} 516 23 re f"]
            for x, label in ((55, "Date"), (132, "Category"), (245, "Scope"), (310, "Description"), (485, "Amount")):
                ops += text_op(x, y, 8, label, True, 0.96)
            y -= 27
            for row in payload:
                date_text = str(row.get("date_local") or "")[:10]
                category = _human_category(row.get("category"))[:18]
                scope = str(row.get("scope") or "")[:9].title()
                desc = _pdf_safe_text(row.get("description") or "")
                if len(desc) > 27:
                    desc = desc[:26] + "…"
                amount = money(row.get("currency"), row.get("amount"))
                ops += text_op(55, y, 8, date_text, False, 0.20)
                ops += text_op(132, y, 8, category, False, 0.20)
                ops += text_op(245, y, 8, scope, False, 0.20)
                ops += text_op(310, y, 8, desc, False, 0.20)
                ops += text_op(485, y, 8, amount, True, 0.10)
                ops += ["0.88 g", f"48 {y-6} 516 0.5 re f"]
                y -= 21

        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        objects.append((page_id, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")))

    objects.extend([
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(page_ids)} >>".encode("ascii")),
        (regular_font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        (bold_font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"),
    ])
    objects.sort(key=lambda x: x[0])
    out = bytearray(b"%PDF-1.4\n")
    offsets = {0: 0}
    for oid, body in objects:
        offsets[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    max_id = max(offsets)
    out.extend(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        out.extend(f"{offsets.get(oid, 0):010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def build_snapshot(sender_phone, conversation_type="DIRECT_DM",
                   requested_scope="all", period=None,
                   include_raw_income=False,
                   include_assets=True,
                   include_leave=True):
    period = period or date.today().strftime("%Y-%m")
    plan = phase2_finance.baseline_plan(
        sender_phone, conversation_type,
        requested_scope=requested_scope,
        reveal_inputs=include_raw_income,
    )
    snapshot = {
        "period": period,
        "privacy": {
            "output_space": plan["output_space"],
            "private_input_used": plan["private_input_used"],
        },
        "baseline_plan": plan,
        "goals": phase2_finance.list_goals(
            sender_phone, conversation_type, requested_scope),
        "obligations": phase2_finance.list_obligations(
            sender_phone, conversation_type, requested_scope, period=period),
    }
    if include_assets:
        snapshot["assets"] = phase2_library.list_assets(
            sender_phone, conversation_type,
            requested_scope=requested_scope, include_documents=False)
    if include_leave and conversation_type != "GROUP":
        balances = []
        for leave_id in ("annual_leave", "medical_leave"):
            try:
                balances.append(
                    phase2_work.leave_balance(
                        leave_id, sender_phone, conversation_type)
                )
            except ValueError:
                pass
        snapshot["leave_balances"] = balances
    return snapshot


def snapshot_json(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)


def finance_csv(snapshot):
    """Spreadsheet-ready rows without requiring a Google credential."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["section", "name", "status", "amount", "currency", "detail"])

    plan = snapshot.get("baseline_plan", {})
    writer.writerow([
        "baseline", "Available monthly baseline", "",
        plan.get("available_baseline_monthly"), plan.get("currency", "MYR"),
        "OT/variable income excluded",
    ])
    for goal in snapshot.get("goals", []):
        writer.writerow([
            "goal", goal.get("name"), goal.get("status"),
            goal.get("funded"), goal.get("currency"),
            f"target={goal.get('target')}; remaining={goal.get('remaining')}; "
            f"baseline={goal.get('baseline_monthly')}",
        ])
    for item in snapshot.get("obligations", []):
        writer.writerow([
            "obligation", item.get("name"), item.get("state"),
            item.get("paid"), item.get("currency"),
            f"expected={item.get('expected')}; due={item.get('due_date')}",
        ])
    for asset in snapshot.get("assets", []):
        writer.writerow([
            "asset", asset.get("name"), asset.get("status"), "",
            "", f"category={asset.get('category')}; brand={asset.get('brand')}; "
                f"model={asset.get('model')}; warranty_end={asset.get('warranty_end')}",
        ])
    for leave in snapshot.get("leave_balances", []):
        writer.writerow([
            "leave", leave.get("name") or leave.get("leave_id"),
            leave.get("status") or "current",
            leave.get("remaining_days"), "days",
            f"entitlement={leave.get('entitlement_days')}; as_of={leave.get('as_of_date')}",
        ])
    return out.getvalue()


def google_sheets_rows(snapshot):
    """Return a 2-D value array ready for an injected Sheets uploader."""
    rows = [["Section", "Name", "Status", "Amount", "Currency", "Detail"]]
    for line in csv.reader(io.StringIO(finance_csv(snapshot))):
        if line and line[0] == "section":
            continue
        rows.append(line)
    return rows


def tv_payload(snapshot):
    """Privacy-safe presentation model. Raw income is never displayed here."""
    plan = snapshot.get("baseline_plan", {})
    cards = [{
        "type": "baseline",
        "title": "Baseline available",
        "value": plan.get("available_baseline_monthly"),
        "currency": plan.get("currency", "MYR"),
        "note": "Variable income excluded",
    }]
    for goal in snapshot.get("goals", []):
        cards.append({
            "type": "goal",
            "title": goal.get("name"),
            "funded": goal.get("funded"),
            "target": goal.get("target"),
            "remaining": goal.get("remaining"),
            "currency": goal.get("currency"),
        })
    return {
        "period": snapshot.get("period"),
        "privacy": snapshot.get("privacy"),
        "cards": cards,
    }


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def minimal_pdf(snapshot):
    """Render the broader household/planning snapshot in ALEX premium style."""
    period_label = _month_label(snapshot.get("period"))
    plan = snapshot.get("baseline_plan") or {}
    goals = list(snapshot.get("goals") or [])
    obligations = list(snapshot.get("obligations") or [])
    assets = list(snapshot.get("assets") or [])
    leave_balances = list(snapshot.get("leave_balances") or [])

    sections = [
        ("Goals", [
            f"{g.get('name')}: {g.get('currency') or 'MYR'} {float(g.get('funded') or 0):,.2f} / "
            f"{g.get('currency') or 'MYR'} {float(g.get('target') or 0):,.2f}"
            for g in goals
        ]),
        ("Bills / obligations", [
            f"{item.get('name')}: {item.get('state')} - "
            + (
                "variable"
                if item.get("expected") is None
                else f"{item.get('currency') or 'MYR'} {float(item.get('expected') or 0):,.2f}"
            )
            for item in obligations
        ]),
        ("Assets", [
            " | ".join(
                x for x in [
                    str(asset.get("name") or ""),
                    str(asset.get("brand") or ""),
                    str(asset.get("model") or ""),
                    (
                        f"Warranty {asset.get('warranty_end')}"
                        if asset.get("warranty_end") else ""
                    ),
                ] if x
            )
            for asset in assets
        ]),
        ("Leave", [
            f"{leave.get('name') or leave.get('leave_id')}: "
            f"{leave.get('remaining_days')} days remaining"
            for leave in leave_balances
        ]),
    ]

    # Flatten into presentation rows while preserving section boundaries.
    rows = []
    baseline = plan.get("available_baseline_monthly")
    if baseline is not None:
        rows.append(("Baseline", f"{plan.get('currency') or 'MYR'} {float(baseline):,.2f}", True))
        rows.append(("", "Variable income / OT excluded from baseline.", False))
    for title, values in sections:
        rows.append((title, "", True))
        if values:
            rows.extend(("", value, False) for value in values)
        else:
            rows.append(("", "No entries.", False))

    per_page = 27
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    page_count = len(pages)
    objects = []
    page_ids = []
    regular_font_id = 3
    bold_font_id = 4
    next_id = 5

    def text_op(x, y, size, value, bold=False, gray=0.12):
        font = "/F2" if bold else "/F1"
        return [
            f"{gray:.3f} g",
            "BT", f"{font} {size} Tf", f"{x} {y} Td",
            f"({_pdf_literal(value)}) Tj", "ET",
        ]

    for page_no, page_rows in enumerate(pages, 1):
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        ops = [
            "0.047 0.086 0.133 rg", "0 714 612 78 re f",
            "0.886 0.624 0.204 rg", "0 708 612 6 re f",
        ]
        ops += text_op(48, 754, 19, "ALEX", True, 1.0)
        ops += text_op(48, 730, 11, "Household Planning Snapshot", False, 0.88)
        ops += text_op(430, 735, 10, period_label, True, 0.95)
        ops += text_op(500, 28, 8, f"Page {page_no} of {page_count}", False, 0.48)

        y = 674
        for label, value, heading in page_rows:
            if heading:
                if label == "Baseline":
                    ops += ["0.965 0.965 0.965 rg", f"48 {y-8} 516 27 re f"]
                    ops += text_op(60, y, 10, "Available monthly baseline", False, 0.28)
                    ops += text_op(385, y, 11, value, True, 0.08)
                    y -= 36
                else:
                    y -= 4
                    ops += text_op(48, y, 13, label, True)
                    y -= 24
                continue
            safe = _pdf_safe_text(value)
            while len(safe) > 78:
                cut = safe.rfind(" ", 0, 78)
                cut = cut if cut > 28 else 78
                ops += text_op(58, y, 9, safe[:cut], False, 0.22)
                safe = safe[cut:].lstrip()
                y -= 17
            ops += text_op(58, y, 9, safe, False, 0.22)
            y -= 19

        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        objects.append((page_id, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")))

    objects.extend([
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(page_ids)} >>".encode("ascii")),
        (regular_font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        (bold_font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"),
    ])
    objects.sort(key=lambda x: x[0])
    out = bytearray(b"%PDF-1.4\n")
    offsets = {0: 0}
    for oid, body in objects:
        offsets[oid] = len(out)
        out.extend(f"{oid} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    max_id = max(offsets)
    out.extend(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for oid in range(1, max_id + 1):
        out.extend(f"{offsets.get(oid, 0):010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)

def handoff_google_sheets(snapshot, uploader):
    """Use an injected, authorized uploader; no credentials live in this module."""
    if not callable(uploader):
        raise ValueError("A configured Sheets uploader is required")
    return uploader(google_sheets_rows(snapshot))


def present_on_tv(snapshot, presenter):
    """Use an injected HA/display presenter; never performs a live call itself."""
    if not callable(presenter):
        raise ValueError("A configured TV presenter is required")
    return presenter(tv_payload(snapshot))
