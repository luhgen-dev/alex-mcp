"""Project Jarvis Phase 2 household asset/document library.

Stores metadata and evidence pointers only. Binary files remain in the existing
media/archive layer; this module never duplicates document contents.
"""
from __future__ import annotations

import uuid
import re
from datetime import date, timedelta

import compat_tools as tools
import scope_policy
from context import current_actor


SCHEMA = """
CREATE TABLE IF NOT EXISTS alex_phase2_assets (
    asset_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    brand TEXT,
    model TEXT,
    serial_number TEXT,
    purchase_date TEXT,
    warranty_end TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','SOLD','DISPOSED','LOST')),
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(space_id) REFERENCES spaces(space_id),
    FOREIGN KEY(owner_user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_assets
    ON alex_phase2_assets(owner_user_id,space_id,status);

CREATE TABLE IF NOT EXISTS alex_phase2_asset_documents (
    link_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    document_type TEXT NOT NULL
        CHECK(document_type IN ('RECEIPT','WARRANTY','MANUAL','PHOTO','OTHER')),
    evidence_ref TEXT NOT NULL,
    source_message_id TEXT,
    note TEXT,
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(asset_id) REFERENCES alex_phase2_assets(asset_id),
    FOREIGN KEY(space_id) REFERENCES spaces(space_id)
);
CREATE INDEX IF NOT EXISTS idx_p2_asset_docs
    ON alex_phase2_asset_documents(asset_id,document_type);
"""


def ensure_schema(conn=None):
    own = conn is None
    if own:
        conn = tools.get_db()
    try:
        conn.executescript(SCHEMA)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _ctx(conn, sender_phone, conversation_type):
    user_id, private_space = tools.resolve_user_and_space(
        conn, sender_phone, conversation_type)
    shared = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND space_id='FAMILY_SHARED'",
        (user_id,),
    ).fetchone() is not None
    return user_id, private_space, shared


def _space_for(conn, sender_phone, conversation_type, visibility):
    user_id, private_space, shared = _ctx(conn, sender_phone, conversation_type)
    visibility = str(visibility or "family").lower()
    if conversation_type == "GROUP":
        if visibility != "family":
            raise PermissionError("Private assets cannot be added from the family group")
        return user_id, "FAMILY_SHARED"
    if visibility == "private":
        return user_id, private_space
    if visibility == "family" and shared:
        return user_id, "FAMILY_SHARED"
    raise PermissionError("Requested asset visibility is not authorized")


def _authorized_asset(conn, asset_id, sender_phone, conversation_type):
    user_id, private_space, shared = _ctx(conn, sender_phone, conversation_type)
    row = conn.execute(
        "SELECT * FROM alex_phase2_assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if not row:
        raise ValueError("Asset not found")
    if row["space_id"] == "FAMILY_SHARED":
        if not shared:
            raise PermissionError("Asset is not authorized")
    elif row["space_id"] != private_space or row["owner_user_id"] != user_id:
        raise PermissionError("Asset is not authorized")
    if conversation_type == "GROUP" and row["space_id"] != "FAMILY_SHARED":
        raise PermissionError("Private assets cannot be accessed in the family group")
    return row


def create_asset(name, sender_phone, conversation_type="DIRECT_DM",
                 visibility="family", category=None, brand=None, model=None,
                 serial_number=None, purchase_date=None, warranty_end=None,
                 note=None):
    if not str(name or "").strip():
        raise ValueError("Asset name is required")
    if purchase_date:
        date.fromisoformat(str(purchase_date)[:10])
    if warranty_end:
        date.fromisoformat(str(warranty_end)[:10])

    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, space_id = _space_for(
            conn, sender_phone, conversation_type, visibility)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_assets(
                asset_id,space_id,owner_user_id,name,category,brand,model,
                serial_number,purchase_date,warranty_end,note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ident, space_id, user_id, str(name).strip(), category, brand, model,
            serial_number, str(purchase_date)[:10] if purchase_date else None,
            str(warranty_end)[:10] if warranty_end else None, note,
        ))
        conn.commit()
        return {"asset_id": ident, "name": str(name).strip(), "space": space_id}
    finally:
        conn.close()


def update_asset(asset_id, sender_phone, conversation_type="DIRECT_DM",
                 name=None, category=None, brand=None, model=None,
                 serial_number=None, purchase_date=None, warranty_end=None,
                 note=None, status=None):
    """Edit authorized asset metadata without recreating or relinking documents."""
    ensure_schema()
    conn = tools.get_db()
    try:
        row = _authorized_asset(conn, asset_id, sender_phone, conversation_type)
        if purchase_date is not None and str(purchase_date).strip():
            date.fromisoformat(str(purchase_date)[:10])
        if warranty_end is not None and str(warranty_end).strip():
            date.fromisoformat(str(warranty_end)[:10])
        next_status = row["status"] if status is None else str(status).upper()
        if next_status not in {"ACTIVE", "SOLD", "DISPOSED", "LOST"}:
            raise ValueError("asset status must be ACTIVE, SOLD, DISPOSED or LOST")
        next_name = row["name"] if name is None else str(name).strip()
        if not next_name:
            raise ValueError("Asset name cannot be empty")
        def keep_or(value, existing, date_value=False):
            if value is None:
                return existing
            if date_value:
                return str(value)[:10] if str(value).strip() else None
            return value
        values = {
            "category": keep_or(category, row["category"]),
            "brand": keep_or(brand, row["brand"]),
            "model": keep_or(model, row["model"]),
            "serial_number": keep_or(serial_number, row["serial_number"]),
            "purchase_date": keep_or(purchase_date, row["purchase_date"], True),
            "warranty_end": keep_or(warranty_end, row["warranty_end"], True),
            "note": keep_or(note, row["note"]),
        }
        conn.execute(
            """UPDATE alex_phase2_assets
               SET name=?,category=?,brand=?,model=?,serial_number=?,
                   purchase_date=?,warranty_end=?,note=?,status=?,
                   updated_at_utc=CURRENT_TIMESTAMP
               WHERE asset_id=?""",
            (
                next_name, values["category"], values["brand"], values["model"],
                values["serial_number"], values["purchase_date"],
                values["warranty_end"], values["note"], next_status, asset_id,
            ),
        )
        conn.commit()
        return {
            "status": "updated", "asset_id": asset_id, "name": next_name,
            "warranty_end": values["warranty_end"], "asset_status": next_status,
        }
    finally:
        conn.close()


def resolve_asset_reference(asset_id, asset_name, sender_phone,
                            conversation_type="DIRECT_DM"):
    """Resolve one authorized asset by id or a unique natural name."""
    ensure_schema()
    if asset_id:
        conn = tools.get_db()
        try:
            return _authorized_asset(
                conn, asset_id, sender_phone, conversation_type
            )["asset_id"]
        finally:
            conn.close()

    wanted = " ".join(re.findall(
        r"[a-z0-9]+", str(asset_name or "").casefold()
    ))
    if not wanted:
        raise ValueError("Provide asset_id or asset_name.")
    rows = list_assets(
        sender_phone, conversation_type, requested_scope="all",
        include_documents=False,
    )
    def norm(value):
        return " ".join(re.findall(
            r"[a-z0-9]+", str(value or "").casefold()
        ))
    exact = [row for row in rows if norm(row.get("name")) == wanted]
    candidates = exact or [
        row for row in rows
        if wanted in norm(row.get("name")) or norm(row.get("name")) in wanted
    ]
    if len(candidates) == 1:
        return candidates[0]["asset_id"]
    if not candidates:
        raise ValueError(
            f"No authorized asset uniquely matches {asset_name!r}."
        )
    names = ", ".join(str(row.get("name")) for row in candidates[:5])
    raise ValueError("Asset name is ambiguous; ask which one: " + names)


def link_document(asset_id, document_type, evidence_ref, sender_phone,
                  conversation_type="DIRECT_DM", source_message_id=None,
                  note=None):
    document_type = str(document_type or "").upper()
    if document_type not in ("RECEIPT", "WARRANTY", "MANUAL", "PHOTO", "OTHER"):
        raise ValueError("Unsupported document type")
    if not str(evidence_ref or "").strip():
        raise ValueError("Document evidence reference is required")
    ensure_schema()
    conn = tools.get_db()
    try:
        asset = _authorized_asset(
            conn, asset_id, sender_phone, conversation_type)
        ident = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO alex_phase2_asset_documents(
                link_id,asset_id,space_id,document_type,evidence_ref,
                source_message_id,note
            ) VALUES (?,?,?,?,?,?,?)
        """, (
            ident, asset_id, asset["space_id"], document_type,
            str(evidence_ref).strip(), source_message_id, note,
        ))
        conn.commit()
        return {"link_id": ident, "asset_id": asset_id,
                "document_type": document_type}
    finally:
        conn.close()


def list_assets(sender_phone, conversation_type="DIRECT_DM",
                requested_scope="all", include_documents=False, query=None):
    ensure_schema()
    conn = tools.get_db()
    try:
        user_id, private_space, shared = _ctx(
            conn, sender_phone, conversation_type)
        try:
            scope = scope_policy.effective_read_scope(current_actor(), requested_scope)
        except RuntimeError:
            scope = str(requested_scope or "all").lower()
        if conversation_type == "GROUP":
            if scope == "private":
                raise PermissionError("Private assets cannot be shown in the family group")
            clause, args = "space_id='FAMILY_SHARED'", ()
        elif scope == "private":
            clause, args = "space_id=? AND owner_user_id=?", (private_space, user_id)
        elif scope == "family":
            if not shared:
                raise PermissionError("Sender lacks family-space membership")
            clause, args = "space_id='FAMILY_SHARED'", ()
        elif shared:
            clause, args = (
                "(space_id='FAMILY_SHARED' OR (space_id=? AND owner_user_id=?))",
                (private_space, user_id),
            )
        else:
            clause, args = "space_id=? AND owner_user_id=?", (private_space, user_id)

        rows = conn.execute(
            "SELECT * FROM alex_phase2_assets WHERE "
            + clause + " ORDER BY name",
            args,
        ).fetchall()
        wanted = [
            token for token in re.findall(
                r"[a-z0-9]+", str(query or "").casefold()
            )
            if len(token) > 1
            and token not in {
                "my", "the", "asset", "assets", "appliance", "appliances",
                "when", "did", "buy", "bought", "warranty", "expire",
                "expires", "expiry", "show", "find",
            }
        ]
        if wanted:
            filtered = []
            for row in rows:
                hay = " ".join(
                    str(row[key] or "").casefold()
                    for key in (
                        "name", "category", "brand", "model",
                        "serial_number", "note",
                    )
                )
                if all(token in hay for token in wanted):
                    filtered.append(row)
            rows = filtered
        result = []
        for row in rows:
            item = dict(row)
            if include_documents:
                item["documents"] = [
                    dict(x) for x in conn.execute("""
                        SELECT document_type,evidence_ref,source_message_id,note
                        FROM alex_phase2_asset_documents
                        WHERE asset_id=? ORDER BY created_at_utc
                    """, (row["asset_id"],)).fetchall()
                ]
            result.append(item)
        return result
    finally:
        conn.close()


def warranties_expiring(within_days, as_of_date, sender_phone,
                        conversation_type="DIRECT_DM",
                        requested_scope="all"):
    start = date.fromisoformat(str(as_of_date)[:10])
    end = start + timedelta(days=int(within_days))
    rows = list_assets(
        sender_phone, conversation_type, requested_scope=requested_scope)
    return [
        row for row in rows
        if row.get("warranty_end")
        and start <= date.fromisoformat(row["warranty_end"]) <= end
        and row.get("status") == "ACTIVE"
    ]
