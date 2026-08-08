"""Diff two OpenAPI documents and emit the drift envelope the agent reasons over.

Primary engine is oasdiff, run via Docker so CI needs no Go toolchain:

    docker run --rm -v /tmp:/specs tufin/oasdiff diff ... --format json

If Docker is unavailable, a small structural differ built into this file takes
over and produces the same envelope. That fallback exists so the demo never
depends on a container pull at the wrong moment, and so the whole loop can be
exercised on a laptop with Docker stopped.

    python -m scripts.detect_drift specs/provider.baseline.json /tmp/v2.json

The envelope deliberately carries a `documentation` block per change: whether
the vendor bothered to describe the field. That is the signal that separates
"a field moved and they told you" from the case this project is built for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

MAX_DEPTH = 3
NO_DESCRIPTION = "(no description provided by the vendor)"


# --------------------------------------------------------------------------
# Schema walking
# --------------------------------------------------------------------------


def _resolve(schema: Any, spec: dict, depth: int = 0) -> dict:
    """Follow $ref / anyOf-with-null down to a concrete schema object."""
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        return schema if isinstance(schema, dict) else {}

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        merged = {k: v for k, v in schema.items() if k != "$ref"}
        return {**_resolve(node, spec, depth + 1), **merged}

    # Optional fields render as anyOf: [T, null]. Collapse to T.
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            concrete = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
            if len(concrete) == 1:
                rest = {k: v for k, v in schema.items() if k != key}
                return {**_resolve(concrete[0], spec, depth + 1), **rest}
    return schema


def _response_schema(spec: dict, path: str, method: str) -> dict:
    """The 2xx application/json schema for one operation, arrays unwrapped."""
    operation = spec.get("paths", {}).get(path, {}).get(method, {})
    responses = operation.get("responses", {}) or {}
    for code in ("200", "201", "default"):
        content = (responses.get(code) or {}).get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
        if schema:
            resolved = _resolve(schema, spec)
            if resolved.get("type") == "array":
                return _resolve(resolved.get("items", {}), spec)
            return resolved
    return {}


def _flatten(schema: dict, spec: dict, prefix: str = "", depth: int = 0) -> dict[str, dict]:
    """Dotted-path map of every leaf and object property in a schema."""
    out: dict[str, dict] = {}
    if depth > MAX_DEPTH or not isinstance(schema, dict):
        return out
    required = set(schema.get("required") or ())
    for name, raw in (schema.get("properties") or {}).items():
        resolved = _resolve(raw, spec, depth)
        if resolved.get("type") == "array":
            resolved = {**resolved, "items": _resolve(resolved.get("items", {}), spec, depth)}
        key = f"{prefix}{name}"
        out[key] = {
            "type": resolved.get("type", "object" if resolved.get("properties") else "unknown"),
            "enum": resolved.get("enum") or resolved.get("const"),
            "description": resolved.get("description") or "",
            "required": name in required,
        }
        if resolved.get("properties"):
            out.update(_flatten(resolved, spec, prefix=f"{key}.", depth=depth + 1))
    return out


def _operations(spec: dict) -> list[tuple[str, str]]:
    ops = []
    for path, methods in (spec.get("paths") or {}).items():
        for method in methods:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                ops.append((path, method.lower()))
    return sorted(ops)


# --------------------------------------------------------------------------
# Built-in structural differ (fallback engine)
# --------------------------------------------------------------------------


def _documentation(entry: dict) -> dict:
    described = bool(entry.get("description"))
    return {
        "described": described,
        "description": entry.get("description") or NO_DESCRIPTION,
    }


def builtin_diff(base: dict, revision: dict) -> dict:
    breaking: list[dict] = []
    additive: list[dict] = []

    base_ops = set(_operations(base))
    rev_ops = set(_operations(revision))

    for path, method in sorted(base_ops - rev_ops):
        breaking.append(
            {
                "kind": "endpoint-removed",
                "operation": f"{method.upper()} {path}",
                "location": path,
                "detail": f"{method.upper()} {path} no longer exists.",
                "documentation": {"described": False, "description": NO_DESCRIPTION},
            }
        )

    for path, method in sorted(base_ops & rev_ops):
        operation = f"{method.upper()} {path}"
        before = _flatten(_response_schema(base, path, method), base)
        after = _flatten(_response_schema(revision, path, method), revision)

        for name in sorted(set(before) - set(after)):
            breaking.append(
                {
                    "kind": "response-property-removed",
                    "operation": operation,
                    "location": f"response.{name}",
                    "detail": (
                        f"Response property '{name}' ({before[name]['type']}) was removed. "
                        "No deprecation notice was present in the previous version."
                    ),
                    "was": before[name],
                    "documentation": _documentation(before[name]),
                }
            )

        for name in sorted(set(after) - set(before)):
            entry = after[name]
            # A new response property never breaks a consumer on its own — but
            # it is exactly where the replacement for a removed field hides.
            additive.append(
                {
                    "kind": "response-property-added",
                    "operation": operation,
                    "location": f"response.{name}",
                    "detail": f"Response property '{name}' ({entry['type']}) is new.",
                    "now": entry,
                    "documentation": _documentation(entry),
                }
            )

        for name in sorted(set(before) & set(after)):
            b, a = before[name], after[name]
            if b["type"] != a["type"]:
                breaking.append(
                    {
                        "kind": "response-property-type-changed",
                        "operation": operation,
                        "location": f"response.{name}",
                        "detail": f"Response property '{name}' changed type: {b['type']} -> {a['type']}.",
                        "was": b,
                        "now": a,
                        "documentation": _documentation(a),
                    }
                )
            elif b["enum"] and a["enum"] and set(map(str, b["enum"])) - set(map(str, a["enum"])):
                gone = sorted(set(map(str, b["enum"])) - set(map(str, a["enum"])))
                breaking.append(
                    {
                        "kind": "response-property-enum-values-removed",
                        "operation": operation,
                        "location": f"response.{name}",
                        "detail": f"Enum values no longer returned for '{name}': {', '.join(gone)}.",
                        "was": b,
                        "now": a,
                        "documentation": _documentation(a),
                    }
                )

    return {"breaking": breaking, "additive": additive}


# --------------------------------------------------------------------------
# oasdiff (primary engine)
# --------------------------------------------------------------------------


def _docker_available() -> bool:
    if os.getenv("DRIFT_FORCE_BUILTIN"):
        return False
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=20
        ).returncode == 0
    except Exception:
        return False


def _run_oasdiff(subcommand: str, base: str, revision: str) -> dict | list | None:
    """Run one oasdiff subcommand in Docker, tolerating flag-name drift."""
    with tempfile.TemporaryDirectory() as workdir:
        for src, name in ((base, "base.json"), (revision, "revision.json")):
            with open(src, "rb") as fh, open(os.path.join(workdir, name), "wb") as out:
                out.write(fh.read())

        image = os.getenv("OASDIFF_IMAGE", "tufin/oasdiff")
        for flag in ("--format", "-f"):
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{workdir}:/specs",
                image, subcommand,
                "/specs/base.json", "/specs/revision.json",
                flag, "json",
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            except Exception as exc:
                print(f"[detect_drift] oasdiff {subcommand} failed to launch: {exc}", file=sys.stderr)
                return None
            # `breaking` exits non-zero when it finds breaking changes; that is
            # a result, not an error, so parse stdout regardless of exit code.
            if proc.stdout.strip():
                try:
                    return json.loads(proc.stdout)
                except json.JSONDecodeError:
                    pass
            if proc.returncode != 0 and flag == "-f":
                print(
                    f"[detect_drift] oasdiff {subcommand} exited {proc.returncode}: "
                    f"{proc.stderr.strip()[:400]}",
                    file=sys.stderr,
                )
        return None


def _normalise_oasdiff_breaking(payload: Any) -> list[dict]:
    """Turn oasdiff's breaking-changes output into our change records."""
    rows: Iterable[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("breakingChanges") or payload.get("changes") or []
    else:
        rows = []

    changes: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        method = (row.get("operation") or row.get("method") or "").upper()
        path = row.get("path") or ""
        text = row.get("text") or row.get("description") or row.get("id") or ""
        changes.append(
            {
                "kind": row.get("id") or "breaking-change",
                "operation": f"{method} {path}".strip(),
                "location": row.get("source") or path,
                "detail": text,
                "level": row.get("level"),
                "source": "oasdiff",
                # oasdiff reports the change, not the field's documentation.
                # `None` means "not determined" — distinct from "no description",
                # which is a claim only the structural pass can make.
                "documentation": {"described": None, "description": "(not determined by oasdiff)"},
            }
        )
    return changes


def _oasdiff_property_names(text: str) -> set[str]:
    """Property names oasdiff mentions, e.g. `items/transaction_status`."""
    names = set()
    for quoted in re.findall(r"`([^`]+)`", text or ""):
        leaf = quoted.split("/")[-1].strip()
        if leaf:
            names.add(leaf)
    return names


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


def _summarise(breaking: list[dict], additive: list[dict], base: dict, revision: dict) -> str:
    title = (revision.get("info") or {}).get("title") or "provider API"
    old = (base.get("info") or {}).get("version", "?")
    new = (revision.get("info") or {}).get("version", "?")
    if not breaking:
        return f"{title} {old} -> {new}: no breaking changes detected."

    # The same field change shows up once per endpoint that returns it. Count
    # distinct changes, and report endpoint spread separately.
    distinct: list[str] = []
    for change in breaking:
        if change["detail"] not in distinct:
            distinct.append(change["detail"])
    endpoints = {c["operation"] for c in breaking if c.get("operation")}

    head = f"{title} {old} -> {new}: {len(distinct)} breaking change(s)"
    if len(endpoints) > 1:
        head += f" across {len(endpoints)} endpoints"

    # Only count fields the structural pass actually inspected. `None` means
    # "not determined" (an oasdiff-only record) and must not be reported as
    # "the vendor didn't document this".
    undocumented = {
        c["location"]
        for c in breaking + additive
        if c["documentation"].get("described") is False
    }
    tail = (
        f" ({len(undocumented)} of the changed fields carry no description)"
        if undocumented
        else ""
    )
    return f"{head}{tail}: {' '.join(distinct[:4])}"


def detect(base_path: str, revision_path: str) -> dict:
    with open(base_path) as fh:
        base = json.load(fh)
    with open(revision_path) as fh:
        revision = json.load(fh)

    # The structural pass always runs: it is what carries per-field types,
    # enum vocabularies and the documentation signal the agent needs.
    structural = builtin_diff(base, revision)
    breaking = list(structural["breaking"])
    additive = list(structural["additive"])
    tool = "builtin"
    raw: dict[str, Any] = {"builtin": structural}

    if _docker_available():
        oasdiff_full = _run_oasdiff("diff", base_path, revision_path)
        oasdiff_breaking = _run_oasdiff("breaking", base_path, revision_path)
        if oasdiff_breaking is not None or oasdiff_full is not None:
            tool = "oasdiff"
            raw["oasdiff"] = {"diff": oasdiff_full, "breaking": oasdiff_breaking}
            # Merge semantically, not by (operation, location): oasdiff keys a
            # change by path ("/payments") while the structural pass keys it by
            # field ("response.transaction_status"), so a positional dedupe
            # never matches and reports one real change several times over.
            by_operation: dict[str | None, list[dict]] = {}
            for record in breaking:
                by_operation.setdefault(record.get("operation"), []).append(record)

            for change in _normalise_oasdiff_breaking(oasdiff_breaking):
                names = _oasdiff_property_names(change["detail"])
                duplicate = None
                for existing in by_operation.get(change.get("operation"), []):
                    leaf = (existing.get("location") or "").split(".")[-1]
                    if leaf and leaf in names:
                        duplicate = existing
                        break
                if duplicate is not None:
                    # Same change, seen by both engines. Record the agreement
                    # rather than emitting a second copy of it.
                    duplicate.setdefault("corroborated_by", []).append(change["kind"])
                else:
                    breaking.append(change)
                    by_operation.setdefault(change.get("operation"), []).append(change)
    else:
        raw["oasdiff_skipped"] = "docker unavailable; used built-in structural differ"

    return {
        "tool": tool,
        "provider": {
            "title": (revision.get("info") or {}).get("title"),
            "base_version": (base.get("info") or {}).get("version"),
            "revision_version": (revision.get("info") or {}).get("version"),
            "revision_notes": (revision.get("info") or {}).get("description") or "",
        },
        "breaking": breaking,
        "additive": additive,
        "summary": _summarise(breaking, additive, base, revision),
        "raw": raw,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="baseline OpenAPI document")
    parser.add_argument("revision", help="OpenAPI document from the new provider code")
    parser.add_argument("--out", help="write the envelope here instead of stdout")
    args = parser.parse_args(argv)

    envelope = detect(args.base, args.revision)
    text = json.dumps(envelope, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(envelope["summary"], file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
