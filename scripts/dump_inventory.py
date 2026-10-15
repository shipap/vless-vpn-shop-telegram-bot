"""Offline confidential dump inventory; emits aggregate evidence only, never rows."""

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

COPY = re.compile(r'^COPY (?:public\.)?"?(\w+)"? \((.*?)\) FROM stdin;$')
REVIEW = (
    ("function", re.compile(r"^CREATE (?:OR REPLACE )?(?:FUNCTION|PROCEDURE)\b", re.IGNORECASE)),
    ("trigger", re.compile(r"^CREATE (?:CONSTRAINT )?TRIGGER\b", re.IGNORECASE)),
    ("extension", re.compile(r"^CREATE EXTENSION\b", re.IGNORECASE)),
    ("external_copy", re.compile(r"^COPY .* (?:PROGRAM|FROM\s+\')", re.IGNORECASE)),
    ("shell_metacommand", re.compile(r"^\\(?:!|i\b|ir\b|o\b|copy\b|connect\b|c\b)", re.IGNORECASE)),
    ("role_or_database", re.compile(r"^(?:CREATE|ALTER|DROP) (?:ROLE|USER|DATABASE)\b", re.IGNORECASE)),
    ("subscription", re.compile(r"^CREATE (?:SUBSCRIPTION|SERVER|FOREIGN)", re.IGNORECASE)),
)
MONEY_FIELDS = {"balance", "referral_balance", "amount", "price", "total_amount", "auto_renew_price"}


def inspect_dump(path):
    path = Path(path)
    tables, review_items, metadata = {}, [], {}
    table = None
    columns = []
    digests = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, raw in enumerate(source, 1):
            line = raw.rstrip("\r\n")
            if table is not None:
                if line == "\\.":
                    table = None
                    continue
                fields = line.split("\t")
                if len(fields) != len(columns):
                    raise ValueError(f"Invalid COPY width at line {line_number}")
                row = dict(zip(columns, fields, strict=True))
                record = tables[table]
                record["count"] += 1
                digests[table].update(raw.encode("utf-8"))
                for key, value in row.items():
                    if value == "\\N":
                        record["nulls"][key] = record["nulls"].get(key, 0) + 1
                    elif key in MONEY_FIELDS:
                        try:
                            amount = Decimal(value)
                            if not amount.is_finite():
                                raise ValueError("Nonfinite money")
                            record["money_totals"][key] = record["money_totals"].get(key, Decimal(0)) + amount
                        except (InvalidOperation, ValueError):
                            raise ValueError(f"Invalid monetary field at line {line_number}") from None
                if "user_id" in row and row["user_id"] != "\\N" and abs(int(row["user_id"])) > 2**53 - 1:
                    record["large_ids"] += 1
                continue
            match = COPY.match(line)
            if match:
                table = match.group(1)
                if table in tables:
                    raise ValueError("Repeated COPY table")
                columns = [key.strip().strip('"') for key in match.group(2).split(",")]
                tables[table] = {
                    "count": 0,
                    "columns": columns,
                    "nulls": {},
                    "large_ids": 0,
                    "money_totals": {},
                }
                digests[table] = hashlib.sha256()
                continue
            for kind, pattern in REVIEW:
                if pattern.search(line):
                    review_items.append({"kind": kind, "line": line_number})
            for label in ("Dumped from database version", "Dumped by pg_dump version"):
                if line.startswith("-- " + label):
                    metadata[label] = line.split(label, 1)[1].strip()
    if table is not None:
        raise ValueError("Unterminated COPY block")
    for key, record in tables.items():
        record["copy_sha256"] = digests[key].hexdigest()
        record["money_totals"] = {field: str(value) for field, value in record["money_totals"].items()}
    return {
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "metadata": metadata,
        "tables": tables,
        "review_items": review_items,
        "status": "OFFLINE AUDIT",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Закрытая офлайн-инвентаризация дампа")
    parser.add_argument("dump", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inspect_dump(args.dump)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "table_count": len(result["tables"]),
                "review_items": result["review_items"],
            },
            ensure_ascii=False,
        )
    )
