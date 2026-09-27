"""Project Jarvis Phase 2 deterministic reports and handoff payloads."""
from __future__ import annotations

import csv
import io
import json
from datetime import date

import phase2_finance
import phase2_library
import phase2_work


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
