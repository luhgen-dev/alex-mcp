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
    writer.writerow(["date", "type", "category", "description", "amount", "currency", "reference"])
    for row in payload.get("records", []):
        writer.writerow([
            row.get("date_local"), row.get("type"), row.get("category"),
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
    """Create a dependency-free, text-only PDF suitable for household reports."""
    plan = snapshot.get("baseline_plan", {})
    lines = [
        f"Project Jarvis Household Report - {snapshot.get('period','')}",
        f"Baseline available: {plan.get('currency','MYR')} {plan.get('available_baseline_monthly',0):.2f}",
        "Variable income such as overtime is excluded from baseline planning.",
        "",
        "Goals:",
    ]
    for goal in snapshot.get("goals", []):
        lines.append(
            f"- {goal.get('name')}: {goal.get('currency')} "
            f"{goal.get('funded',0):.2f} / {goal.get('target',0):.2f}"
        )
    lines.append("")
    lines.append("Obligations:")
    for item in snapshot.get("obligations", []):
        amount = item.get("expected")
        amount_text = "variable" if amount is None else f"{item.get('currency')} {amount:.2f}"
        lines.append(
            f"- {item.get('name')}: {item.get('state')} ({amount_text})"
        )

    # One-page PDF; reports with many rows can later use the same builder with
    # pagination. No external PDF dependency is required in the HA add-on.
    y = 800
    commands = ["BT", "/F1 11 Tf"]
    for line in lines[:48]:
        commands.append(f"1 0 0 1 50 {y} Tm ({_pdf_escape(line)}) Tj")
        y -= 15
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode())
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf.extend(f"{off:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(pdf)


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
