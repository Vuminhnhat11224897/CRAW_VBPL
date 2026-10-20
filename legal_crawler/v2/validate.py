"""Offline check of Legal/Chunk documents against live ES mappings (no write to ES)."""
import math
import re
from datetime import date, datetime

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def check_value(name, spec, value, errors):
    kind = spec.get("type")
    if value is None:
        return
    if "properties" in spec or kind == "object":
        if spec.get("enabled") is False:
            return
        if not isinstance(value, dict):
            errors.append(f"{name}: expected object")
            return
        for key, item in value.items():
            if key not in spec.get("properties", {}):
                errors.append(f"{name}.{key}: not in mapping")
            else:
                check_value(f"{name}.{key}", spec["properties"][key], item, errors)
        return
    values = value if isinstance(value, list) and kind != "dense_vector" else [value]
    for item in values:
        if kind in {"keyword", "text"}:
            if not isinstance(item, str):
                errors.append(f"{name}: expected string, got {type(item).__name__}")
        elif kind == "date":
            fmt = spec.get("format")
            try:
                if fmt == "yyyy-MM-dd":
                    ok = bool(DATE.fullmatch(item)) and bool(date.fromisoformat(item))
                else:
                    ok = bool(datetime.fromisoformat(item))
            except (TypeError, ValueError):
                ok = False
            if not ok:
                errors.append(f"{name}: bad date {item!r} for format {fmt or 'default'}")
        elif kind in {"integer", "long"}:
            if not isinstance(item, int) or isinstance(item, bool):
                errors.append(f"{name}: expected integer")
        elif kind == "boolean":
            if not isinstance(item, bool):
                errors.append(f"{name}: expected boolean")
        elif kind in {"float", "double"}:
            if not isinstance(item, (int, float)) or isinstance(item, bool):
                errors.append(f"{name}: expected number")
        elif kind == "dense_vector":
            if (not isinstance(item, list) or len(item) != spec["dims"]
                    or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in item)
                    or not any(item)):
                errors.append(f"{name}: invalid vector (dims={spec['dims']})")
        else:
            errors.append(f"{name}: unhandled mapping type {kind}")


def validate_source(source, properties):
    errors = []
    for key, value in source.items():
        if key not in properties:
            errors.append(f"{key}: not in mapping (would be dynamically mapped)")
        else:
            check_value(key, properties[key], value, errors)
    return errors


def validate_batch(document, chunks, doc_props, chunk_props, doc_id):
    errors = [f"document.{e}" for e in validate_source(document, doc_props)]
    seen = set()
    for chunk in chunks:
        cid = chunk["_id"]
        if cid in seen:
            errors.append(f"chunk {cid}: duplicate _id")
        seen.add(cid)
        if not cid.startswith(str(doc_id)):
            errors.append(f"chunk {cid}: _id does not start with doc id")
        if chunk["_source"].get("doc_id") != str(doc_id):
            errors.append(f"chunk {cid}: doc_id mismatch")
        errors += [f"chunk {cid}.{e}" for e in validate_source(chunk["_source"], chunk_props)]
    for key in ("document_number", "title", "legal_type", "issuing_authority", "issuance_date", "full_content"):
        if not document.get(key):
            errors.append(f"document.{key}: required but empty")
    return errors
