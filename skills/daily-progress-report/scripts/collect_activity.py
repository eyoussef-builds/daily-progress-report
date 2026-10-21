#!/usr/bin/env python3
"""Collect dated Codex evidence without modifying chat history or running chat instructions.

Standard library only; .zst logs require the zstd executable. Output is private
scratch evidence for an agent to synthesize, not an automatically verified report.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from zoneinfo import ZoneInfo

UTC = timezone.utc
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ROLLOUT_ID = re.compile(r"(" + UUID + r")(?:_|\.jsonl)", re.I)
INTERNAL_PREFIXES = (
    "<environment_context>", "# AGENTS.md instructions", "<recommended_plugins>",
    "<external_codex_apps_open_page>", "<codex_internal_context", "<turn_aborted>",
)
SECRET_PATTERNS = [
    r"\bsk_(?:live|test)_[A-Za-z0-9]+", r"\bpk_(?:live|test)_[A-Za-z0-9]+",
    r"\bwhsec_[A-Za-z0-9]+", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    r"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}",
    r"\bAIza[A-Za-z0-9_-]{25,}", r"\bya29\.[A-Za-z0-9._-]+",
    r"\bxox[baprs]-[A-Za-z0-9-]+",
]


def redact(text):
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(\b(?:access_token|refresh_token|api_key|password|client_secret)\s*[:=]\s*)[\"']?[^\s\"'&,}]+",
        r"\1[REDACTED]", text,
    )
    return text


def instant(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def window(day, zone, now=None):
    """Local calendar boundaries, including 23/25-hour daylight-saving days."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    now = now or datetime.now(UTC)
    return start, min(end, now), end


def text_content(item):
    content = item.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        c.get("text", "") for c in content if isinstance(c, dict)
        and str(c.get("type", "")).lower() in {"text", "input_text", "output_text"}
        and isinstance(c.get("text"), str)
    )


def parent_from_source(source):
    try:
        source = json.loads(source) if isinstance(source, str) else source
        return source.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id") if isinstance(source, dict) else None
    except (ValueError, TypeError):
        return None


def read_index(root, warnings):
    """Use the newest available schema; never read credential/config tables."""
    paths = sorted(root.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    columns_wanted = {"id", "rollout_path", "created_at", "updated_at", "cwd", "title", "name", "source", "thread_source", "archived", "agent_path", "project_id"}
    for path in paths:
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as con:
                con.execute("PRAGMA query_only=ON")
                available = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                columns = sorted(available & columns_wanted)
                if "id" not in columns:
                    continue
                con.row_factory = sqlite3.Row
                rows = {r["id"]: dict(r) for r in con.execute("SELECT " + ",".join(columns) + " FROM threads")}
                return rows, str(path)
        except (sqlite3.Error, OSError) as exc:
            warnings.append({"path": str(path), "issue": type(exc).__name__})
    return {}, None


@contextmanager
def open_log(path):
    if path.name.endswith(".zst"):
        binary = shutil.which("zstd")
        if not binary:
            raise RuntimeError("zstd unavailable; compressed history not read")
        proc = subprocess.Popen([binary, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        exhausted = False

        def lines():
            nonlocal exhausted
            for line in proc.stdout:
                yield line
            exhausted = True

        try:
            yield lines()
            # Internal-review logs are deliberately stopped after their metadata.
            # Closing a partial decoder is not a corrupt-history error.
            if not exhausted and proc.poll() is None:
                proc.terminate()
            proc.stdout.close()
            result = proc.wait()
            if exhausted and result:
                raise RuntimeError("zstd could not decode the history file")
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
    else:
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield stream


def discover(root, rows, start, full_scan):
    paths = set()
    for name in ("sessions", "archived_sessions"):
        folder = root / name
        if folder.exists():
            paths.update(p.resolve() for p in folder.rglob("*.jsonl*") if p.is_file() and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst")))
    indexed_recent = set()
    for row in rows.values():
        raw = row.get("rollout_path")
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not p.exists() and Path(str(p) + ".zst").exists():
            p = Path(str(p) + ".zst")
        if p.is_file() and (p.name.endswith('.jsonl') or p.name.endswith('.jsonl.zst')):
            paths.add(p.resolve())
            updated = instant(row.get("updated_at"))
            if updated and updated >= start:
                indexed_recent.add(p.resolve())
    selected = []
    for p in sorted(paths):
        # An old conversation resumed today must be included regardless of its filename date.
        if full_scan or p in indexed_recent or p.stat().st_mtime >= start.timestamp():
            selected.append(p)
    return selected, len(paths)


def normalize(record, session, path, line, include_tools, redact_text=True):
    """Return only canonical visible messages and safe tool metadata, never tool output/reasoning."""
    p = record.get("payload", {})
    if not isinstance(p, dict):
        return None
    stamp = instant(record.get("timestamp"))
    kind, phase, text, item_id = None, None, None, None
    thread_id, turn_id = session.get("id"), session.get("turn_id")
    origin, rank = "record_time", 1
    if record.get("type") == "event_msg" and p.get("type") == "item_completed":
        item = p.get("item", {})
        if not isinstance(item, dict):
            return None
        thread_id = p.get("thread_id") or thread_id
        turn_id = p.get("turn_id") or turn_id
        stamp = instant(p.get("completed_at_ms")) or stamp
        origin, rank = "item_completed", 5
        item_id = item.get("id")
        item_type = str(item.get("type", "")).lower()
        if item_type in {"usermessage", "agentmessage"}:
            kind = "user_message" if item_type == "usermessage" else "assistant_message"
            text, phase = text_content(item), item.get("phase")
        elif include_tools and item_type in {"commandexecution", "filechange", "mcptoolcall", "websearch"}:
            kind = "tool_evidence"
            # Command strings, arguments and tool output can contain credentials or other chats.
            safe = {"item_type": item.get("type"), "status": item.get("status")}
            for key in ("exit_code", "exitCode", "server", "tool"):
                if key in item:
                    safe[key] = item[key]
            if item_type == "filechange":
                changes = item.get("changes", [])
                if isinstance(changes, dict):
                    safe["paths"] = list(changes)[:100]
                elif isinstance(changes, list):
                    safe["paths"] = [c.get("path") for c in changes if isinstance(c, dict) and c.get("path")][:100]
            text = json.dumps(safe, ensure_ascii=False)
        else:
            return None
    elif record.get("type") == "response_item" and p.get("type") in {"message", "agent_message"}:
        role = p.get("role") or ("assistant" if p.get("type") == "agent_message" else None)
        if role not in {"user", "assistant"}:
            return None
        kind = "user_message" if role == "user" else "assistant_message"
        text, phase, item_id = text_content(p), p.get("phase"), p.get("id")
        meta = p.get("internal_chat_message_metadata_passthrough") or {}
        if not isinstance(meta, dict):
            meta = {}
        created = instant(meta.get("create_time"))
        if created:
            stamp, origin, rank = created, "message_created", 4
        turn_id = meta.get("turn_id") or turn_id
    elif record.get("type") == "event_msg" and p.get("type") in {"user_message", "agent_message"}:
        kind = "user_message" if p["type"] == "user_message" else "assistant_message"
        text = p.get("message")
        phase, item_id = p.get("phase"), p.get("id")
        turn_id = p.get("turn_id") or turn_id
    else:
        return None
    if not stamp or not isinstance(thread_id, str) or not thread_id or not isinstance(text, str) or not text.strip():
        return None
    if kind == "user_message" and text.lstrip().startswith(INTERNAL_PREFIXES):
        return None
    # A fork's copied history is context, not new work in that fork.
    created_at = instant(session.get("created_at"))
    if created_at and stamp < created_at - timedelta(seconds=5):
        return None
    return {"kind": kind, "item_id": item_id, "thread_id": thread_id, "turn_id": turn_id,
            "timestamp_utc": iso(stamp), "timestamp_basis": origin, "phase": phase,
            "text": redact(text.strip()) if redact_text else text.strip(),
            "source": {"path": str(path), "line": line}, "_rank": rank}


def deduplicate(events):
    """Prefer original message creation time and stable IDs over replay time."""
    unique = {}
    for e in events:
        if e.get("item_id"):
            key = (e["kind"], "id", e["item_id"])
        else:
            key = (e["kind"], "fallback", e.get("thread_id"), e.get("turn_id"), e["text"])
            if not e.get("turn_id"):
                key += (e["timestamp_utc"],)
        old = unique.get(key)
        if old is None or (e["_rank"], -instant(e["timestamp_utc"]).timestamp()) > (old["_rank"], -instant(old["timestamp_utc"]).timestamp()):
            unique[key] = e
    # Old schemas emit both an event and response message without matching IDs.
    best = {}
    for e in unique.values():
        key = (e["kind"], e["thread_id"], e.get("turn_id"), e["text"])
        old = best.get(key)
        if e["kind"] == "tool_evidence" or not e.get("turn_id"):
            key += (e.get("item_id"), e["timestamp_utc"])
            old = best.get(key)
        if old is None or e["_rank"] > old["_rank"]:
            best[key] = e
    return sorted(best.values(), key=lambda e: (e["timestamp_utc"], e["thread_id"]))


def load_catalog(path):
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    # Supply the decoded list_threads object or a flat array of entries.
    entries = data if isinstance(data, list) else data.get("pinnedThreads", []) + data.get("threads", [])
    return {e["id"]: e for e in entries if isinstance(e, dict) and e.get("kind", "codex") == "codex" and e.get("hostId", "local") == "local"}


def write_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def capture_usage(record, session, usage_records):
    """Count per-response usage once. Cumulative token_count records are not additive."""
    if record.get('type') != 'token_usage_record':
        return
    p = record.get('payload', {})
    if not isinstance(p, dict) or not isinstance(p.get('usage'), dict):
        return
    stamp = instant(record.get('timestamp'))
    response_id = p.get('response_id')
    tid = p.get('thread_id') or session.get('id')
    if not stamp or not isinstance(response_id, str) or not response_id or not isinstance(tid, str):
        return
    created = instant(session.get('created_at'))
    if created and stamp < created - timedelta(seconds=5):
        return
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
    usage = {k: p['usage'][k] for k in keys if isinstance(p['usage'].get(k), int) and not isinstance(p['usage'][k], bool) and p['usage'][k] >= 0}
    if 'total_tokens' not in usage:
        return
    entry = {'timestamp_utc': iso(stamp), 'thread_id': tid, 'usage': usage}
    previous = usage_records.get(response_id)
    if previous is None or entry['timestamp_utc'] < previous['timestamp_utc']:
        usage_records[response_id] = entry


def activity_metrics(events, threads, usage_records, start, cutoff):
    visible = [e for e in events if e['kind'] in {'user_message', 'assistant_message'}]
    user = [e for e in visible if e['kind'] == 'user_message']
    supporting = {t['id'] for t in threads if t.get('parent_thread_id') or t.get('thread_source') == 'subagent'}
    substantive = [t for t in threads if t['event_counts'].get('user_message', 0) + t['event_counts'].get('assistant_message', 0)]
    usage = [x for x in usage_records.values() if start <= instant(x['timestamp_utc']) < cutoff]
    tokens = Counter()
    for entry in usage:
        tokens.update(entry['usage'])
    token_threads = {x['thread_id'] for x in usage}
    message_threads = {e['thread_id'] for e in visible}
    return {
        'messages': {'total_visible': len(visible), 'user_side': len(user),
                     'assistant_updates': len(visible) - len(user),
                     'supporting_agent_messages': sum(e['thread_id'] in supporting for e in visible),
                     'automation_trigger_entries': sum(e['text'].lstrip().startswith('<heartbeat>') for e in user),
                     'definition': 'Deduplicated visible user-side and assistant messages; includes commentary and any automated/handoff entries, excludes tools, reasoning, system/developer text and internal reviews.'},
        'chats': {'substantive_main': sum(t['id'] not in supporting for t in substantive),
                  'supporting': sum(t['id'] in supporting for t in substantive),
                  'tool_only': sum(not t['event_counts'].get('user_message') and not t['event_counts'].get('assistant_message') for t in threads)},
        'workspaces': sorted({str(Path(t['cwd']).expanduser()) for t in substantive if t.get('cwd')}),
        'projects': {'count': None, 'status': 'requires_semantic_grouping',
                     'definition': 'Group chats and workspaces by the actual product/business/project. Different chat titles, worktrees or a shared video-editing folder do not establish distinct projects.'},
        'tokens': {'status': 'recorded' if usage else 'unavailable',
                   'per_response_records': len(usage), 'totals': dict(tokens) if usage else None,
                   'message_threads_without_usage': sorted(message_threads-token_threads),
                   'definition': 'Observed per-response usage, deduplicated by response ID and dated by record time. Includes cached/repeated input. Cached input and reasoning are breakdowns, not amounts to add to total_tokens. Not unique words, billed cost or all-device usage; cumulative counters and uninstrumented tools/image generation are excluded.'}
    }


def collect(root, day, zone, output=None, full_scan=False, catalog=None, now=None, include_tools=True):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError("Codex history root does not exist: " + str(root))
    start, cutoff, end = window(day, zone, now)
    if cutoff <= start:
        raise ValueError("Requested day has not started in the selected timezone")
    warnings = []
    rows, index_path = read_index(root, warnings)
    paths, discovered = discover(root, rows, start, full_scan)
    if not discovered:
        warnings.append({"path": str(root), "issue": "No retained session logs discovered"})
    sessions, all_events, files_read, malformed, internal_files = {}, [], 0, 0, 0
    outside_ids = {}
    usage_records = {}
    for path in paths:
        match = ROLLOUT_ID.search(path.name)
        tid = match.group(1) if match else None
        session = dict(rows.get(tid, {}))
        if tid:
            session.setdefault("id", tid)
        if root / "archived_sessions" in path.parents:
            session["archived"] = True
        try:
            with open_log(path) as stream:
                skip = False
                for line_number, raw in enumerate(stream, 1):
                    try:
                        record = json.loads(raw)
                    except (ValueError, TypeError):
                        malformed += 1
                        continue
                    if not isinstance(record, dict):
                        malformed += 1
                        continue
                    p = record.get("payload", {})
                    if record.get("type") == "session_meta" and isinstance(p, dict):
                        sid = p.get("id") or p.get("session_id") or session.get("id")
                        session.update({k: p[k] for k in ("cwd", "source", "thread_source") if k in p})
                        session["id"] = sid
                        session.setdefault("created_at", p.get("timestamp"))
                        session["parent_thread_id"] = parent_from_source(session.get("source"))
                        skip = session.get("thread_source") in {"guardian_review", "memory_consolidation"} or '"guardian"' in str(session.get("source", ""))
                        if sid:
                            sessions[sid] = {**sessions.get(sid, {}), **session}
                        if skip:
                            internal_files += 1
                            break
                    if skip:
                        continue
                    capture_usage(record, session, usage_records)
                    if record.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "task_started":
                        session["turn_id"] = p.get("turn_id")
                    event = normalize(record, session, path, line_number, include_tools, redact_text=False)
                    if event:
                        stamp = instant(event["timestamp_utc"])
                        if start <= stamp < cutoff:
                            event["text"] = redact(event["text"])
                            all_events.append(event)
                        elif event.get("item_id"):
                            # Preserve only provenance for out-of-window messages, not their text.
                            key = (event["kind"], event["item_id"])
                            score = (event["_rank"], -stamp.timestamp())
                            outside_ids[key] = max(score, outside_ids.get(key, score))
                files_read += 1
        except (OSError, RuntimeError) as exc:
            warnings.append({"path": str(path), "issue": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__})
    events = []
    for e in deduplicate(all_events):
        outside = outside_ids.get((e["kind"], e.get("item_id")))
        score = (e["_rank"], -instant(e["timestamp_utc"]).timestamp())
        if outside is None or outside < score:
            events.append(e)
    tz = ZoneInfo(zone)
    grouped = {}
    for e in events:
        e.pop("_rank", None)
        e["timestamp_local"] = instant(e["timestamp_utc"]).astimezone(tz).isoformat()
        grouped.setdefault(e["thread_id"], []).append(e)
    catalog = catalog or {}
    output = Path(output) if output else Path(tempfile.mkdtemp(prefix="codex-daily-evidence-"))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output directory must be empty; refusing to overwrite evidence")
    os.chmod(output, 0o700)
    threads = []
    for tid, entries in grouped.items():
        safe_id = tid if re.fullmatch(UUID, tid, re.I) else "thread-" + hashlib.sha256(tid.encode()).hexdigest()[:20]
        info = {**rows.get(tid, {}), **sessions.get(tid, {})}
        exact = catalog.get(tid, {})
        # Index title can be an entire first prompt; it is not a verified display title.
        fallback = info.get("name") or info.get("title") or tid
        title = exact.get("title") or redact(str(fallback).splitlines()[0])[:100]
        summary = {"id": tid, "host_id": "local", "title": title,
                   "title_source": "app_catalog" if exact.get("title") else "local_index_fallback",
                   "cwd": info.get("cwd"), "archived": bool(info.get("archived")),
                   "project_id": exact.get('projectId') or info.get('project_id'),
                   "thread_source": info.get("thread_source"),
                   "parent_thread_id": info.get("parent_thread_id") or parent_from_source(info.get("source")),
                   "first_observed": entries[0]["timestamp_local"], "last_observed": entries[-1]["timestamp_local"],
                   "event_counts": dict(Counter(e["kind"] for e in entries)),
                   "evidence_file": str(output / (safe_id + ".json"))}
        write_json(output / (safe_id + ".json"), {"thread": summary, "events": entries})
        threads.append(summary)
    coverage = {"codex_root": str(root), "index": index_path,
                "mode": "full_scan" if full_scan else "index_and_mtime_candidates",
                "files_discovered": discovered, "files_selected": len(paths), "files_read": files_read,
                "internal_review_files_excluded": internal_files, "malformed_lines": malformed,
                "warnings": warnings, "events_in_window": len(events), "threads_with_activity": len(threads),
                "limitation": "Local retained history only. Fast mode selects by index update time or file mtime; use --full-scan if timestamps/index may be stale. Other devices/hosts require separate connector reads or explicit exported roots. Logs are not a time tracker; assistant claims are not independent proof."}
    result = {"date": str(day), "timezone": zone, "start_utc": iso(start), "cutoff_utc": iso(cutoff),
              "calendar_end_utc": iso(end), "partial_day": cutoff < end,
              "metrics": activity_metrics(events, threads, usage_records, start, cutoff),
              "coverage": coverage, "threads": sorted(threads, key=lambda t: t["first_observed"])}
    write_json(output / "index.json", result)
    return result, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Local calendar date YYYY-MM-DD")
    parser.add_argument("--timezone", required=True, help="IANA zone, e.g. UTC or Europe/London")
    parser.add_argument("--codex-root", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--output", help="Empty private scratch folder; defaults to a new temporary folder")
    parser.add_argument("--catalog", help="Decoded list_threads JSON for exact local display titles")
    parser.add_argument("--full-scan", action="store_true", help="Inspect every retained log regardless of mtime/index")
    parser.add_argument("--messages-only", action="store_true", help="Omit safe tool metadata")
    args = parser.parse_args()
    result, output = collect(args.codex_root, args.date, args.timezone, args.output, args.full_scan,
                             load_catalog(args.catalog), include_tools=not args.messages_only)
    print(json.dumps({"index": str(output / "index.json"), "date": result["date"],
                      "timezone": result["timezone"], "cutoff_utc": result["cutoff_utc"],
                      "coverage": result["coverage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
