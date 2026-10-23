"""Auditable SQLite-to-PostgreSQL transfer with offline and database dry-run modes.

Offline preview (does not read DATABASE_URL or connect):
    python -m database.import_sqlite --source database/db.sqlite3 --dry-run
PostgreSQL preview / apply require explicit target IDs; apply also requires a
validated custom-format backup and an already-migrated destination schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

TABLE_COLUMNS = {
    "users": ("id", "email", "password_hash", "created_at"),
    "accounts": ("id", "user_id", "name", "base_currency", "created_at"),
    "instruments": ("id", "symbol", "asset_class", "exchange", "currency", "provider", "provider_symbol", "exchange_timezone"),
    "portfolios": ("id", "account_id", "name", "created_at"),
    "positions": ("id", "portfolio_id", "instrument_id", "quantity", "average_cost", "updated_at"),
    "transactions": ("id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "quantity", "unit_price", "currency", "occurred_at", "created_at"),
    "watchlists": ("id", "account_id", "name", "created_at"),
    "watchlist_items": ("watchlist_id", "instrument_id", "added_at"),
    "orders": ("id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "order_type", "quantity", "limit_price", "status", "provider", "provider_order_id", "created_at", "updated_at", "submitted_at", "cancelled_at", "rejection_reason", "preview_json"),
    "auth_sessions": ("session_hash", "user_id", "csrf_token", "created_at", "expires_at"),
    "auth_attempts": ("ip_address", "attempted_at"),
    "account_holdings": ("id", "account_id", "symbol", "quantity", "purchase_price", "idempotency_key", "created_at", "currency", "asset_class"),
    "order_events": ("id", "order_id", "account_id", "status", "detail", "created_at"),
}
PRIMARY_KEYS = {
    "users": ("id",), "accounts": ("id",), "instruments": ("id",),
    "portfolios": ("id",), "positions": ("id",), "transactions": ("id",),
    "watchlists": ("id",), "watchlist_items": ("watchlist_id", "instrument_id"),
    "orders": ("id",), "auth_sessions": ("session_hash",),
    "account_holdings": ("id",), "order_events": ("id",),
    "portfolio": ("id",), "portfolio_idempotency": ("idempotency_key",),
    "legacy_ownership_audit": ("source_table", "source_id"),
}
UNIQUE_KEYS = {
    "users": (("email",),),
    "accounts": (("user_id", "name"),),
    "instruments": (("symbol", "exchange", "asset_class"),),
    "portfolios": (("account_id", "name"),),
    "positions": (("portfolio_id", "instrument_id"),),
    "transactions": (("account_id", "idempotency_key"),),
    "watchlists": (("account_id", "name"),),
    "orders": (("account_id", "idempotency_key"),),
    "account_holdings": (("account_id", "idempotency_key"),),
    "legacy_data_quarantine": (("source_table", "source_id"),),
}
TIMESTAMP_COLUMNS = {
    "created_at", "updated_at", "submitted_at", "cancelled_at", "occurred_at",
    "added_at", "expires_at", "attempted_at",
}
DECIMAL_COLUMNS = {"quantity", "average_cost", "unit_price", "limit_price", "purchase_price"}
REQUIRED_COLUMNS = {
    "users": ("id", "created_at"),
    "accounts": ("id", "user_id", "name", "base_currency", "created_at"),
    "instruments": ("id", "symbol", "asset_class", "currency"),
    "portfolios": ("id", "account_id", "name", "created_at"),
    "positions": ("id", "portfolio_id", "instrument_id", "quantity", "average_cost", "updated_at"),
    "transactions": ("id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "quantity", "unit_price", "currency", "occurred_at", "created_at"),
    "watchlists": ("id", "account_id", "name", "created_at"),
    "watchlist_items": ("watchlist_id", "instrument_id", "added_at"),
    "orders": ("id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "order_type", "quantity", "status", "provider", "created_at"),
    "auth_sessions": ("session_hash", "csrf_token", "created_at", "expires_at"),
    "auth_attempts": ("ip_address", "attempted_at"),
    "account_holdings": ("id", "account_id", "symbol", "quantity", "purchase_price", "idempotency_key", "created_at"),
    "order_events": ("id", "order_id", "account_id", "status", "detail", "created_at"),
}
LEGACY_PORTFOLIO_COLUMNS = ("id", "symbol", "quantity", "purchase_price", "created_at", "owner_account_id")
LEGACY_IDEMPOTENCY_COLUMNS = ("idempotency_key", "portfolio_id", "created_at", "owner_account_id")
BASE_TABLE_ORDER = tuple(TABLE_COLUMNS)
ADVISORY_LOCK_KEY = 7_104_202_026


class ImportConflict(RuntimeError):
    """Destination has a key collision with different persisted values."""


def _source_tables(source):
    return {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _source_columns(source, table):
    if table not in _source_tables(source):
        return set()
    return {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}


def _normal_timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        text = str(value).strip()
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("A source timestamp is not valid ISO-8601") from exc
    if stamp.tzinfo is None:
        # SQLite CURRENT_TIMESTAMP is UTC without an offset; other naive values
        # are treated consistently as UTC and explicitly marked during import.
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _normal_decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("A source financial value is not a valid decimal") from exc
    if not number.is_finite():
        raise ValueError("A source financial value is not finite")
    return format(number, "f")


def _convert_row(table, row, source_cols):
    result = {}
    for column in TABLE_COLUMNS[table]:
        if column in source_cols:
            value = row[column]
        elif table == "orders" and column == "provider":
            value = "local_simulator"
        else:
            value = None
        if column in TIMESTAMP_COLUMNS:
            value = _normal_timestamp(value)
        elif column in DECIMAL_COLUMNS:
            value = _normal_decimal(value)
        result[column] = value
    missing = [column for column in REQUIRED_COLUMNS[table] if result.get(column) is None]
    if missing:
        raise ValueError(f"Source table {table} has missing required values in columns: {', '.join(missing)}")
    return result


def _json_payload(row):
    payload = dict(row)
    for key in DECIMAL_COLUMNS.intersection(payload):
        if payload[key] is not None:
            payload[key] = _normal_decimal(payload[key])
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _query_dicts(connection, sql, params=()):
    cursor = connection.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def _count(connection, table):
    return int(connection.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"])


def _canonical(value, column):
    if value is None:
        return None
    if column in DECIMAL_COLUMNS:
        try:
            return Decimal(str(value)).normalize()
        except InvalidOperation:
            return str(value)
    if column in TIMESTAMP_COLUMNS:
        try:
            return _normal_timestamp(value)
        except ValueError:
            return str(value)
    return value


def _same_record(existing, proposed):
    for key, value in proposed.items():
        actual = existing.get(key)
        if key == "source_payload":
            try:
                actual = json.loads(actual) if isinstance(actual, str) else actual
                wanted = json.loads(value) if isinstance(value, str) else value
            except (TypeError, ValueError):
                return False
            if actual != wanted:
                return False
        elif _canonical(actual, key) != _canonical(value, key):
            return False
    return True


def _insert_idempotently(destination, table, values, primary_key, destination_kind):
    columns = tuple(values)
    placeholders = []
    for column in columns:
        placeholders.append("?::jsonb" if destination_kind == "postgres" and table == "legacy_data_quarantine" and column == "source_payload" else "?")
    sql = (f'INSERT INTO "{table}" ({", ".join(columns)}) VALUES ({", ".join(placeholders)}) '
           "ON CONFLICT DO NOTHING")
    params = tuple(values[c] for c in columns)
    cursor = destination.execute(sql, params)
    if cursor.rowcount:
        return True
    where = " AND ".join(f'"{key}" = ?' for key in primary_key)
    key_values = tuple(values[key] for key in primary_key)
    existing = destination.execute(f'SELECT {", ".join(columns)} FROM "{table}" WHERE {where}', key_values).fetchone()
    if existing is None:
        raise ImportConflict(f"Unique-key collision in {table} does not match the source primary key")
    if not _same_record(dict(existing), values):
        raise ImportConflict(f"Existing destination row conflicts with source row in {table}")
    return False


def _check_alternate_unique_conflicts(destination, table, values, primary_key):
    for key_columns in UNIQUE_KEYS.get(table, ()):
        if tuple(key_columns) == tuple(primary_key) or any(values.get(column) is None for column in key_columns):
            continue
        where = " AND ".join(f'"{column}" = ?' for column in key_columns)
        existing = destination.execute(
            f'SELECT {", ".join(primary_key)} FROM "{table}" WHERE {where}',
            tuple(values[column] for column in key_columns),
        ).fetchone()
        if existing is not None and any(existing[column] != values[column] for column in primary_key):
            raise ImportConflict(f"Alternate unique-key collision in {table}")


def _legacy_rows(source, table, source_cols, account_ids, portfolio_owners, ownership_map):
    if table not in _source_tables(source):
        return []
    rows = _query_dicts(source, f'SELECT * FROM "{table}" ORDER BY 1')
    planned = []
    for row in rows:
        source_id = str(row.get("id", row.get("idempotency_key")))
        recorded_owner = row.get("owner_account_id")
        owner = recorded_owner
        audit = None
        mapping = ownership_map.get(table, {}).get(source_id)
        if mapping:
            mapped_owner, audit = _mapping_details(mapping, account_ids)
            if recorded_owner and recorded_owner != mapped_owner:
                raise ValueError(f"Ownership map conflicts with recorded owner in {table} row {source_id}")
            if table == "portfolio_idempotency":
                parent_owner = portfolio_owners.get(row.get("portfolio_id"))
                if not parent_owner or parent_owner != mapped_owner:
                    raise ValueError(f"Map the parent portfolio to the same account before mapping {table} row {source_id}")
            owner = mapped_owner
        parent_owner = portfolio_owners.get(row.get("portfolio_id")) if table == "portfolio_idempotency" else None
        if table == "portfolio_idempotency":
            owner = owner or parent_owner
            # Do not import an idempotency record unless its referenced legacy row
            # also has a verified owner and will be imported into the destination.
            if not parent_owner or (row.get("owner_account_id") and row.get("owner_account_id") != parent_owner):
                owner = None
        if owner and owner in account_ids:
            values = {c: row.get(c) for c in LEGACY_PORTFOLIO_COLUMNS} if table == "portfolio" else {
                "idempotency_key": row.get("idempotency_key"),
                "portfolio_id": row.get("portfolio_id"),
                "created_at": _normal_timestamp(row.get("created_at")),
                "owner_account_id": owner,
            }
            if table == "portfolio":
                values["quantity"] = _normal_decimal(row.get("quantity"))
                values["purchase_price"] = _normal_decimal(row.get("purchase_price"))
                values["created_at"] = _normal_timestamp(row.get("created_at"))
                values["owner_account_id"] = owner
            planned.append(("mapped" if audit else "owned", values, row, audit))
        else:
            reason = "legacy_owner_unassigned" if not owner else "legacy_owner_not_in_source_accounts"
            planned.append(("quarantine", {
                "source_table": table,
                "source_id": source_id,
                "source_payload": _json_payload(row),
                "reason": reason,
            }, row, None))
    return planned


def _mapping_details(entry, account_ids):
    if not isinstance(entry, dict):
        raise ValueError("Each ownership mapping must be an object")
    account_id = str(entry.get("account_id", "")).strip()
    rationale = str(entry.get("rationale", "")).strip()
    approved_by = str(entry.get("approved_by", "")).strip()
    approved_at = _normal_timestamp(entry.get("approved_at"))
    if not account_id or account_id not in account_ids:
        raise ValueError("Ownership mapping account_id must exist in the SQLite accounts table")
    if len(rationale) < 12 or not approved_by or not approved_at:
        raise ValueError("Ownership mapping requires rationale, approved_by, and approved_at")
    return account_id, {"account_id": account_id, "rationale": rationale,
                        "approved_by": approved_by, "approved_at": approved_at}


def _validate_ownership_map(source, mapping, account_ids, portfolio_owners):
    normalized = {}
    if not mapping:
        return normalized
    if not isinstance(mapping, dict) or set(mapping) - {"portfolio", "portfolio_idempotency"}:
        raise ValueError("Ownership map may contain only portfolio and portfolio_idempotency tables")
    for table in ("portfolio", "portfolio_idempotency"):
        if table not in mapping:
            continue
        entries = mapping[table]
        if not isinstance(entries, dict):
            raise ValueError(f"Ownership mappings for {table} must be an object keyed by source ID")
        source_columns = _source_columns(source, table)
        if not source_columns:
            raise ValueError(f"Ownership map references missing source table {table}")
        key_column = "id" if table == "portfolio" else "idempotency_key"
        source_ids = {str(row[0]) for row in source.execute(f'SELECT "{key_column}" FROM "{table}"')}
        unknown_ids = set(entries) - source_ids
        if unknown_ids:
            raise ValueError(f"Ownership map contains unknown source IDs for {table}")
        normalized[table] = {}
        for source_id, detail in entries.items():
            owner, details = _mapping_details(detail, account_ids)
            if table == "portfolio":
                source_row = source.execute("SELECT id, owner_account_id FROM portfolio WHERE CAST(id AS TEXT) = ?", (source_id,)).fetchone()
                if source_row[1] and source_row[1] != owner:
                    raise ValueError("Ownership map conflicts with recorded portfolio owner")
                portfolio_owners[source_row[0]] = owner
            else:
                parent = source.execute("SELECT portfolio_id FROM portfolio_idempotency WHERE idempotency_key = ?", (source_id,)).fetchone()
                parent_owner = portfolio_owners.get(parent[0]) if parent else None
                if not parent_owner or parent_owner != owner:
                    raise ValueError("An idempotency mapping must match a verified or explicitly mapped parent portfolio")
            normalized[table][source_id] = details
    return normalized


def _destination_columns(table, include_json=False):
    if table == "legacy_data_quarantine":
        return ("source_table", "source_id", "source_payload", "reason")
    if table == "portfolio":
        return LEGACY_PORTFOLIO_COLUMNS
    if table == "portfolio_idempotency":
        return LEGACY_IDEMPOTENCY_COLUMNS
    if table == "legacy_ownership_audit":
        return ("source_table", "source_id", "account_id", "rationale", "approved_by", "approved_at")
    return TABLE_COLUMNS[table]


def _row_query_order(table):
    key = PRIMARY_KEYS.get(table, ())
    return " ORDER BY " + ", ".join(key) if key else ""


def build_report(source, destination=None, *, destination_kind="postgres", apply=False, ownership_map=None):
    """Validate conversions and return aggregate counts; source is read-only by convention."""
    available = _source_tables(source)
    source_fk_violations = source.execute("PRAGMA foreign_key_check").fetchall()
    if apply and source_fk_violations:
        raise ValueError("SQLite source has foreign-key violations; import stopped before writes")
    account_ids = {r[0] for r in source.execute("SELECT id FROM accounts")} if "accounts" in available else set()
    portfolio_owners = {}
    if "portfolio" in available and "owner_account_id" in _source_columns(source, "portfolio"):
        portfolio_owners = {r[0]: r[1] for r in source.execute("SELECT id, owner_account_id FROM portfolio")}
    ownership_map = _validate_ownership_map(source, ownership_map or {}, account_ids, portfolio_owners)
    reports = []
    transaction_started = False
    if destination is not None and apply:
        if destination_kind == "postgres":
            destination.execute("SELECT pg_advisory_xact_lock(?)", (ADVISORY_LOCK_KEY,))
        transaction_started = True
    try:
        for table in BASE_TABLE_ORDER:
            if table not in available:
                continue
            source_cols = _source_columns(source, table)
            source_rows = _query_dicts(source, f'SELECT * FROM "{table}"{_row_query_order(table)}')
            if table == "auth_attempts":
                wanted = Counter((row["ip_address"], _normal_timestamp(row["attempted_at"])) for row in source_rows)
                before = _count(destination, table) if destination is not None else None
                inserted = already = 0
                if destination is not None:
                    for (ip_address, attempted_at), multiplicity in wanted.items():
                        current = int(destination.execute(
                            "SELECT COUNT(*) AS count FROM auth_attempts WHERE ip_address = ? AND attempted_at = ?",
                            (ip_address, attempted_at),
                        ).fetchone()["count"])
                        already += min(current, multiplicity)
                        missing = max(0, multiplicity - current)
                        if apply:
                            for _ in range(missing):
                                destination.execute("INSERT INTO auth_attempts(ip_address, attempted_at) VALUES (?, ?)",
                                                    (ip_address, attempted_at))
                        inserted += missing
                after = _count(destination, table) if destination is not None else None
                reports.append({"table": table, "source_rows": len(source_rows), "destination_before": before,
                                "would_insert": len(source_rows) if destination is None else inserted,
                                "already_present": already if destination is not None else 0,
                                "destination_after": after if destination is not None else None,
                                "expected_after_import": None if destination is None else before + inserted,
                                "reconciled": (after == before + inserted) if destination is not None and apply else None,
                                "plan_accounted": already + inserted == len(source_rows) if destination is not None else True,
                                "dedupe_rule": "preserve source multiplicity by IP and normalized timestamp"})
                continue
            proposed = [_convert_row(table, row, source_cols) for row in source_rows]
            before = _count(destination, table) if destination is not None else None
            inserted = already = 0
            if destination is not None:
                for values in proposed:
                    if apply:
                        added = _insert_idempotently(destination, table, values, PRIMARY_KEYS[table], destination_kind)
                    else:
                        _check_alternate_unique_conflicts(destination, table, values, PRIMARY_KEYS[table])
                        # In preview, use the source key to detect existing or conflicting rows.
                        where = " AND ".join(f'"{key}" = ?' for key in PRIMARY_KEYS[table])
                        existing = destination.execute(f'SELECT {", ".join(values)} FROM "{table}" WHERE {where}', tuple(values[k] for k in PRIMARY_KEYS[table])).fetchone()
                        if existing is None:
                            added = True
                        elif _same_record(dict(existing), values):
                            added = False
                        else:
                            raise ImportConflict(f"Existing destination row conflicts with source row in {table}")
                    inserted += int(added)
                    already += int(not added)
            after = _count(destination, table) if destination is not None else None
            reports.append({"table": table, "source_rows": len(source_rows), "destination_before": before,
                            "would_insert": (len(source_rows) if destination is None else inserted),
                            "already_present": already if destination is not None else 0,
                            "destination_after": after if destination is not None else None,
                            "expected_after_import": None if destination is None else before + inserted,
                            "reconciled": (after == before + inserted) if destination is not None and apply else None,
                            "plan_accounted": inserted + already == len(source_rows) if destination is not None else True})

        legacy_accounted = []
        quarantine_before = _count(destination, "legacy_data_quarantine") if destination is not None else None
        q_inserted = q_existing = 0
        for table in ("portfolio", "portfolio_idempotency"):
            if table not in available:
                continue
            source_cols = _source_columns(source, table)
            planned = _legacy_rows(source, table, source_cols, account_ids, portfolio_owners, ownership_map)
            source_count = len(planned)
            owned = [p for p in planned if p[0] == "owned"]
            mapped = [p for p in planned if p[0] == "mapped"]
            quarantine = [p for p in planned if p[0] == "quarantine"]
            if destination is None:
                q_inserted += len(quarantine)
            dest_table = table
            before = _count(destination, dest_table) if destination is not None else None
            inserted = existing = 0
            if destination is not None:
                for classification, values, original, audit in planned:
                    target_table = table if classification in {"owned", "mapped"} else "legacy_data_quarantine"
                    if classification in {"owned", "mapped"}:
                        if table == "portfolio":
                            values = {k: values[k] for k in ("id", "symbol", "quantity", "purchase_price", "created_at", "owner_account_id")}
                        else:
                            values = {k: values[k] for k in ("idempotency_key", "portfolio_id", "created_at", "owner_account_id")}
                        pk = PRIMARY_KEYS[table]
                    else:
                        pk = ("source_table", "source_id")
                    if apply:
                        added = _insert_idempotently(destination, target_table, values, pk, destination_kind)
                    else:
                        _check_alternate_unique_conflicts(destination, target_table, values, pk)
                        where = " AND ".join(f'"{key}" = ?' for key in pk)
                        cols = _destination_columns(target_table)
                        existing_row = destination.execute(f'SELECT {", ".join(cols)} FROM "{target_table}" WHERE {where}', tuple(values[k] for k in pk)).fetchone()
                        if existing_row is None:
                            added = True
                        elif _same_record(dict(existing_row), values):
                            added = False
                        else:
                            raise ImportConflict(f"Existing destination row conflicts with source/quarantine row in {target_table}")
                    if classification == "quarantine":
                        q_inserted += int(added); q_existing += int(not added)
                    else:
                        inserted += int(added); existing += int(not added)
                    if classification == "mapped":
                        audit_values = {"source_table": table, "source_id": str(values.get("id", values.get("idempotency_key"))),
                                        **audit}
                        if apply:
                            _insert_idempotently(destination, "legacy_ownership_audit", audit_values,
                                                 PRIMARY_KEYS["legacy_ownership_audit"], destination_kind)
            after = _count(destination, dest_table) if destination is not None else None
            legacy_accounted.append({"source_table": table, "source_rows": source_count,
                                     "owned_rows": len(owned), "explicitly_mapped_rows": len(mapped),
                                     "quarantined_unowned_rows": len(quarantine),
                                     "destination_before": before,
                                     "would_insert_owned": len(owned) + len(mapped) if destination is None else inserted,
                                     "already_present_owned": existing if destination is not None else 0,
                                     "destination_after": after if destination is not None else None,
                                     "expected_after_import": None if destination is None else before + inserted,
                                     "reconciled": (after == before + inserted) if destination is not None and apply else None,
                                     "source_accounted": len(owned) + len(mapped) + len(quarantine) == source_count})
        quarantine_after = _count(destination, "legacy_data_quarantine") if destination is not None else None
        if destination is not None and apply:
            destination.commit()
        migration_rows = source.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() if "schema_migrations" in available else []
        schema_versions = [int(row[0]) for row in migration_rows]
        expected_source_tables = set(BASE_TABLE_ORDER) | {"portfolio", "portfolio_idempotency"}
        gaps = {}
        for table, expected_columns in {
            "orders": set(TABLE_COLUMNS["orders"]),
            "account_holdings": set(TABLE_COLUMNS["account_holdings"]),
        }.items():
            actual = _source_columns(source, table)
            if table in available and expected_columns - actual:
                gaps[table] = sorted(expected_columns - actual)
        report = {
            "mode": "apply" if apply else ("destination_dry_run" if destination is not None else "offline_dry_run"),
            "source_schema_versions": schema_versions,
            "source_missing_expected_tables": sorted(expected_source_tables - available),
            "source_column_gaps_filled_with_defaults_or_nulls": gaps,
            "application_tables": reports,
            "legacy_tables": legacy_accounted,
            "quarantine": {"destination_before": quarantine_before, "new_rows": q_inserted,
                           "already_present": q_existing, "destination_after": quarantine_after,
                           "expected_after_import": None if destination is None else quarantine_before + q_inserted,
                           "reconciled": (quarantine_after == quarantine_before + q_inserted) if destination is not None and apply else None},
            "source_foreign_key_violations": len(source_fk_violations),
        }
        return report
    except Exception:
        if destination is not None and (apply or transaction_started):
            destination.rollback()
        raise


def _verify_postgres_target(connection, expected_database, expected_endpoint, expected_branch, configured_host):
    row = connection.execute("SELECT current_database() AS database_name, current_setting('neon.endpoint_id',true) AS endpoint_id, current_setting('neon.branch_id',true) AS branch_id").fetchone()
    host_match = bool(re.match(r"^" + re.escape(str(row["endpoint_id"] or "").lower()) + r"(?:-pooler)?\.", configured_host.lower()))
    if (row["database_name"] != expected_database or row["endpoint_id"] != expected_endpoint
            or row["branch_id"] != expected_branch or not host_match):
        raise RuntimeError("PostgreSQL target identity mismatch")
    return row


def _validate_backup(path, expected_hash):
    backup_root = (ROOT / ".private_backups").resolve()
    try:
        path.resolve().relative_to(backup_root)
    except ValueError as exc:
        raise ValueError("Backup must be inside the private ignored backup directory") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Backup file is missing or empty")
    digest_obj = hashlib.sha256()
    with path.open("rb") as backup:
        for chunk in iter(lambda: backup.read(1024 * 1024), b""):
            digest_obj.update(chunk)
    digest = digest_obj.hexdigest()
    if digest.lower() != expected_hash.lower():
        raise ValueError("Backup checksum mismatch")
    restore = Path(r"C:\Program Files\PostgreSQL\18\bin\pg_restore.exe")
    if not restore.is_file():
        raise RuntimeError("PostgreSQL 18 pg_restore was not found at its configured path")
    safe_env = {k: v for k, v in os.environ.items()
                if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA"}}
    result = subprocess.run([str(restore), "--list", str(path)], capture_output=True, env=safe_env, check=False)
    if result.returncode:
        raise ValueError("Backup archive validation failed")


def _open_source(path):
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError("SQLite source database file not found")
    source = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA foreign_keys = ON")
    return source


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview or import AlphaScope SQLite records into PostgreSQL")
    parser.add_argument("--source", type=Path, default=ROOT / "database" / "db.sqlite3")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Offline preview; never reads DATABASE_URL")
    mode.add_argument("--postgres-preview", action="store_true", help="Connect read-only to count/reconcile destination rows")
    mode.add_argument("--apply", action="store_true", help="Import rows; requires confirmed target and backup")
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-endpoint")
    parser.add_argument("--expected-branch")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--ownership-map", type=Path, help="JSON explicit ownership decisions; requires rationale and approver fields")
    args = parser.parse_args(argv)
    try:
        ownership_map = json.loads(args.ownership_map.read_text(encoding="utf-8")) if args.ownership_map else None
    except (OSError, json.JSONDecodeError):
        parser.error("Ownership map could not be read as JSON")
    source = _open_source(args.source)
    destination = None
    try:
        if args.dry_run:
            report = build_report(source, ownership_map=ownership_map)
        else:
            from database.foundation import backend, connect
            if backend() != "postgresql":
                parser.error("DATABASE_URL must select PostgreSQL for a PostgreSQL preview/import")
            if not (args.expected_database and args.expected_endpoint and args.expected_branch):
                parser.error("Expected database, endpoint, and branch IDs are required")
            destination = connect()
            if args.postgres_preview:
                destination.execute("SET TRANSACTION READ ONLY")
            configured_host = urlparse(os.environ["DATABASE_URL"]).hostname or ""
            _verify_postgres_target(destination, args.expected_database, args.expected_endpoint, args.expected_branch, configured_host)
            if args.postgres_preview:
                report = build_report(source, destination, apply=False, ownership_map=ownership_map)
            else:
                if not args.backup or not args.backup_sha256:
                    parser.error("A verified custom-format backup and SHA-256 are required")
                _validate_backup(args.backup, args.backup_sha256)
                from database.postgres_migrations import MIGRATIONS, migration_state
                state = migration_state(destination)
                if state != len(MIGRATIONS):
                    parser.error("Destination schema migrations must be reviewed and applied first")
                report = build_report(source, destination, apply=True, ownership_map=ownership_map)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        source.close()
        if destination is not None:
            destination.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
