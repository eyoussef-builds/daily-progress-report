#!/usr/bin/env python3
"""Read local Claude Code transcripts; never open credentials, caches or tool output.

Observed local JSONL schemas are not a stable API. Outputs are private evidence
for synthesis, not proof of completion or access to ordinary claude.ai chats.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import collect_activity as common


def default_root():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))


def desktop_metadata(root, warnings):
    """Whitelist display/provenance fields from local session metadata only."""
    result = {}
    if root is None:
        return result
    for path in sorted(Path(root).expanduser().glob('*/*/local_*.json')):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                continue
            sid = value.get('cliSessionId')
            if isinstance(sid, str):
                result[sid] = {key: value.get(key) for key in
                               ('title', 'forkedFromSessionId', 'createdAt', 'isArchived')}
        except (OSError, ValueError):
            warnings.append({'path': str(path), 'issue': 'Unreadable session metadata'})
    return result


def visible_text(record):
    if record.get('type') not in ('user', 'assistant') or record.get('isCompactSummary'):
        return ''
    message = record.get('message')
    if not isinstance(message, dict):
        return ''
    # tool_use, tool_result, thinking, images and attachments never enter evidence.
    text = common.text_content(message)
    if record['type'] == 'user':
        text = re.sub(r'<system-reminder\b[^>]*>.*?</system-reminder>', '', text, flags=re.S)
        text = re.sub(r'<system-reminder\b.*$', '', text, flags=re.S).strip()
        if record.get('isMeta') or text.startswith((
            '<local-command', '<command-', '<task-notification', '[Request interrupted',
            '[Usage limit', '[Earlier usage-limit', 'Base directory for this skill:',
            'This session is being continued from a previous conversation', '[Image: source:',
        )):
            return ''
    return text.strip()


def identity(record, path):
    sid = record.get('sessionId') or path.stem
    sid = sid if isinstance(sid, str) else path.stem
    agent = record.get('agentId')
    if 'subagents' in path.parts and not agent:
        agent = path.stem
    if agent:
        return sid + '/agent/' + str(agent), sid
    return sid, None


def usage_metrics(records, start, cutoff, message_threads):
    selected = [r for r in records.values() if start <= r['stamp'] < cutoff]
    keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
    totals = Counter()
    complete = 0
    for record in selected:
        usage = record['usage']
        totals.update(usage)
        complete += all(key in usage for key in keys)
    usage_threads = {r['thread_id'] for r in selected}
    missing = sorted(message_threads - usage_threads)
    status = 'unavailable' if not selected else ('recorded' if complete == len(selected) and not missing else 'partial')
    known = dict(totals) if selected else None
    if known is not None:
        # Anthropic cache-write/read input is separate from uncached input.
        known['total_input_tokens'] = sum(totals[k] for k in keys[:3])
        known['total_tokens'] = sum(totals[k] for k in keys)
    return {'status': status, 'per_response_records': len(selected), 'totals': known,
            'incomplete_usage_records': len(selected) - complete,
            'message_threads_without_usage': missing,
            'definition': 'Unique assistant API message IDs, dated by their earliest retained usage record; streamed fields use their maximum, never a sum. Total = uncached input + cache creation + cache read + output. Missing fields/sources are partial. Not unique writing, billing cost or all-device usage.'}


def collect(root, day, zone, output=None, full_scan=False, metadata_root=None, now=None):
    root = Path(root).expanduser().resolve()
    projects = root / 'projects'
    if not projects.is_dir():
        raise FileNotFoundError('Claude Code projects history unavailable: ' + str(projects))
    start, cutoff, end = common.window(day, zone, now)
    if cutoff <= start:
        raise ValueError('Requested day has not started in the selected timezone')
    warnings = []
    metadata = desktop_metadata(metadata_root, warnings)
    paths = sorted(projects.rglob('*.jsonl'))
    # No other directories, history/config files, caches, memory or credentials.
    selected = [p for p in paths if full_scan or p.stat().st_mtime >= start.timestamp()]
    frames, usages, sessions = {}, {}, {}
    malformed = files_read = duplicate_frames = 0
    for path in selected:
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line, raw in enumerate(stream, 1):
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        malformed += 1
                        continue
                    if not isinstance(record, dict):
                        malformed += 1
                        continue
                    stamp = common.instant(record.get('timestamp'))
                    if not stamp or record.get('type') not in ('user', 'assistant'):
                        continue
                    sid, parent = identity(record, path)
                    info = sessions.setdefault(sid, {'cwd': record.get('cwd'), 'parent_thread_id': parent,
                        'thread_source': 'subagent' if parent or record.get('isSidechain') else 'user'})
                    if record.get('cwd'):
                        info['cwd'] = record['cwd']
                    message = record.get('message', {})
                    if not isinstance(message, dict):
                        continue
                    mid = message.get('id')
                    usage = message.get('usage')
                    if record['type'] == 'assistant' and isinstance(mid, str) and isinstance(usage, dict):
                        values = {k: usage[k] for k in ('input_tokens', 'cache_creation_input_tokens',
                                  'cache_read_input_tokens', 'output_tokens')
                                  if isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0}
                        if values:
                            saved = usages.setdefault(mid, {'stamp': stamp, 'thread_id': sid, 'usage': {}})
                            if stamp < saved['stamp']:
                                saved['stamp'], saved['thread_id'] = stamp, sid
                            for key, value in values.items():
                                saved['usage'][key] = max(value, saved['usage'].get(key, 0))
                    text = visible_text(record)
                    if not text:
                        continue
                    uuid = record.get('uuid')
                    key = (record['type'], uuid) if uuid else (record['type'], mid, text, common.iso(stamp))
                    event = {'kind': 'user_message' if record['type'] == 'user' else 'assistant_message',
                        'item_id': uuid, 'thread_id': sid, 'timestamp_utc': common.iso(stamp),
                        'timestamp_basis': 'transcript_record', 'phase': message.get('stop_reason'),
                        'text': common.redact(text), 'api_message_id': mid,
                        'prompt_id': record.get('promptId'),
                        'source': {'path': str(path), 'line': line}, 'agent_family': 'claude_code'}
                    if key in frames:
                        duplicate_frames += 1
                        if frames[key]['timestamp_utc'] <= event['timestamp_utc']:
                            continue
                    frames[key] = event
            files_read += 1
        except OSError:
            warnings.append({'path': str(path), 'issue': 'Unreadable transcript'})
    # Copy/fork records retain original timestamps/UUIDs. Filter after dedup so a
    # replay on this date cannot turn an older achievement into new progress.
    grouped_messages = {}
    message_parts = defaultdict(set)
    for event in sorted(frames.values(), key=lambda e: e['timestamp_utc']):
        if not start <= common.instant(event['timestamp_utc']) < cutoff:
            continue
        ident = event.get('api_message_id') if event['kind'] == 'assistant_message' else event.get('prompt_id')
        key = (event['kind'], ident) if ident else (event['kind'], event['item_id'] or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest())
        fragment = event['text']
        if key in grouped_messages:
            existing = grouped_messages[key]
            if event['text'] not in message_parts[key]:
                existing['text'] += '\n' + event['text']
            if event.get('phase') == 'end_turn':
                existing['phase'] = 'end_turn'
            existing['last_observed_utc'] = event['timestamp_utc']
        else:
            event['last_observed_utc'] = event['timestamp_utc']
            grouped_messages[key] = event
        message_parts[key].add(fragment)
    events = list(grouped_messages.values())
    grouped = defaultdict(list)
    from zoneinfo import ZoneInfo
    for event in events:
        event['timestamp_local'] = common.instant(event['timestamp_utc']).astimezone(ZoneInfo(zone)).isoformat()
        event['last_observed_local'] = common.instant(event['last_observed_utc']).astimezone(ZoneInfo(zone)).isoformat()
        grouped[event['thread_id']].append(event)
    out = Path(output) if output else Path(tempfile.mkdtemp(prefix='claude-daily-evidence-'))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Output directory must be empty')
    os.chmod(out, 0o700)
    threads = []
    for sid, entries in grouped.items():
        info, display = sessions[sid], metadata.get(sid, {})
        summary = {'id': sid, 'agent_family': 'claude_code', 'host_id': 'local', **info,
            'title': common.redact(display['title'] if isinstance(display.get('title'), str) else sid),
            'title_source': 'desktop_session_metadata' if display.get('title') else 'session_id',
            'forked_from_session_id': display.get('forkedFromSessionId'),
            'archived': bool(display.get('isArchived')), 'first_observed': entries[0]['timestamp_local'],
            'last_observed': max(e['last_observed_local'] for e in entries),
            'event_counts': dict(Counter(e['kind'] for e in entries))}
        filename = hashlib.sha256(sid.encode()).hexdigest()[:24] + '.json'
        summary['evidence_file'] = str(out / filename)
        common.write_json(out / filename, {'thread': summary, 'events': entries})
        threads.append(summary)
    supporting = {t['id'] for t in threads if t['thread_source'] == 'subagent'}
    users = sum(e['kind'] == 'user_message' for e in events)
    metrics = {'messages': {'total_visible': len(events), 'user_side': users,
        'assistant_updates': len(events) - users,
        'supporting_agent_messages': sum(e['thread_id'] in supporting for e in events),
        'definition': 'Visible text only, one user prompt/assistant API message; merged streaming blocks and deduplicated forks. Excludes tool results, thinking, reminders, skill injections and compact summaries.'},
        'chats': {'substantive_main': len(threads) - len(supporting), 'supporting': len(supporting)},
        'workspaces': sorted({t['cwd'] for t in threads if isinstance(t.get('cwd'), str)}),
        'projects': {'count': None, 'status': 'requires_cross_source_semantic_grouping'},
        'tokens': usage_metrics(usages, start, cutoff, set(grouped))}
    coverage = {'agent_family': 'claude_code', 'claude_root': str(root),
        'mode': 'full_scan' if full_scan else 'mtime_candidates', 'files_discovered': len(paths),
        'files_selected': len(selected), 'files_read': files_read, 'malformed_lines': malformed,
        'duplicate_frames_removed': duplicate_frames, 'warnings': warnings,
        'limitation': 'Retained local Claude Code JSONL only. Ordinary claude.ai chats, Cowork/cloud-only sessions, other devices and deleted history are not collected. File mtime is only a candidate hint. Observed schema, not a stable public API.'}
    result = {'agent_family': 'claude_code', 'date': str(day), 'timezone': zone,
        'start_utc': common.iso(start), 'cutoff_utc': common.iso(cutoff),
        'calendar_end_utc': common.iso(end), 'partial_day': cutoff < end,
        'metrics': metrics, 'coverage': coverage, 'threads': sorted(threads, key=lambda t: t['first_observed'])}
    common.write_json(out / 'index.json', result)
    return result, out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True)
    parser.add_argument('--timezone', required=True)
    parser.add_argument('--claude-root', default=str(default_root()))
    parser.add_argument('--claude-desktop-root', help='Optional local session metadata directory; no chat/config/cookie databases')
    parser.add_argument('--output')
    parser.add_argument('--full-scan', action='store_true')
    args = parser.parse_args()
    result, out = collect(args.claude_root, args.date, args.timezone, args.output,
                          args.full_scan, args.claude_desktop_root)
    print(json.dumps({'index': str(out / 'index.json'), 'coverage': result['coverage'],
                      'metrics': result['metrics']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
