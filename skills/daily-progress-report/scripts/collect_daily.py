#!/usr/bin/env python3
"""Collect Codex and local Claude Code evidence, keeping source coverage separate."""
import argparse
import json
import os
from pathlib import Path
import tempfile

import collect_activity as codex
import collect_claude as claude


def combined_metrics(sources):
    available = {key: value['result']['metrics'] for key, value in sources.items()
                 if value['status'] == 'collected'}
    messages = {key: sum(m['messages'].get(key, 0) for m in available.values())
                for key in ('total_visible', 'user_side', 'assistant_updates', 'supporting_agent_messages')}
    chats = {key: sum(m['chats'].get(key, 0) for m in available.values())
             for key in ('substantive_main', 'supporting')}
    token_sources, total = {}, 0
    for family, metrics in available.items():
        usage = metrics['tokens']
        totals = usage.get('totals')
        token_sources[family] = usage
        if totals is not None:
            total += totals.get('total_tokens', 0)
    measured = [u for u in token_sources.values() if u.get('totals') is not None]
    complete = bool(measured) and len(measured) == len(sources) and all(u['status'] == 'recorded' for u in measured)
    return {'messages': messages, 'chats': chats,
            'by_agent': available,
            'projects': {'count': None, 'status': 'requires_cross_source_semantic_grouping',
                         'definition': 'Map the actual products/businesses across agents. Shared projects count once; source labels remain attached to each milestone.'},
            'tokens': {'status': 'recorded' if complete else 'partial' if measured else 'unavailable',
                       'recorded_total_tokens': total if measured else None,
                       'by_agent': token_sources,
                       'definition': 'Sum recorded totals once, preserving different provider cache semantics and unavailable/partial sources. Processing volume, not billing cost, unique writing or all-device coverage.'}}


def collect(day, zone, output=None, codex_root=None, claude_root=None,
            sources=('codex', 'claude_code'), full_scan=False, catalog=None,
            claude_desktop_root=None, now=None):
    start, cutoff, end = codex.window(day, zone, now)
    if cutoff <= start:
        raise ValueError('Requested day has not started in the selected timezone')
    out = Path(output) if output else Path(tempfile.mkdtemp(prefix='agent-daily-evidence-'))
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Output directory must be empty')
    os.chmod(out, 0o700)
    codex_root = Path(codex_root or os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    explicit_claude = claude_root is not None
    claude_root = Path(claude_root) if explicit_claude else claude.default_root()
    if claude_desktop_root is None and not explicit_claude:
        candidate = Path.home() / 'Library/Application Support/Claude/claude-code-sessions'
        if candidate.is_dir():
            claude_desktop_root = candidate
    results = {}
    for family in dict.fromkeys(sources):
        try:
            if family == 'codex':
                result, folder = codex.collect(codex_root, day, zone, out / 'codex',
                    full_scan=full_scan, catalog=catalog, now=now)
            elif family == 'claude_code':
                result, folder = claude.collect(claude_root, day, zone, out / 'claude_code',
                    full_scan=full_scan, metadata_root=claude_desktop_root, now=now)
            else:
                raise ValueError('Unsupported source: ' + family)
            results[family] = {'status': 'collected', 'index': str(folder / 'index.json'), 'result': result}
        except (FileNotFoundError, PermissionError) as exc:
            results[family] = {'status': 'unavailable', 'reason': str(exc)}
    threads = [{**thread, 'agent_family': family, 'source_key': family + ':' + thread['id']}
               for family, source in results.items() if source['status'] == 'collected'
               for thread in source['result']['threads']]
    result = {'date': str(day), 'timezone': zone, 'start_utc': codex.iso(start),
              'cutoff_utc': codex.iso(cutoff), 'calendar_end_utc': codex.iso(end),
              'partial_day': cutoff < end, 'sources': results,
              'metrics': combined_metrics(results),
              'threads': sorted(threads, key=lambda t: t['first_observed']),
              'coverage_note': 'Local retained Codex and Claude Code sources only. Ordinary Claude chats, Cowork/cloud-only sessions and other-device histories require separately authorized readable tools/exports. Missing sources are not zero activity.'}
    codex.write_json(out / 'index.json', result)
    return result, out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True)
    parser.add_argument('--timezone', required=True)
    parser.add_argument('--output')
    parser.add_argument('--codex-root')
    parser.add_argument('--claude-root')
    parser.add_argument('--claude-desktop-root')
    parser.add_argument('--sources', nargs='+', choices=('codex', 'claude_code'), default=['codex', 'claude_code'])
    parser.add_argument('--catalog', help='Optional decoded Codex chat inventory')
    parser.add_argument('--full-scan', action='store_true')
    args = parser.parse_args()
    result, out = collect(args.date, args.timezone, args.output, args.codex_root,
                          args.claude_root, args.sources, args.full_scan,
                          codex.load_catalog(args.catalog), args.claude_desktop_root)
    print(json.dumps({'index': str(out / 'index.json'), 'date': result['date'],
                     'timezone': result['timezone'], 'metrics': result['metrics'],
                     'coverage': {key: {'status': source['status'],
                         'coverage': source.get('result', {}).get('coverage'),
                         'reason': source.get('reason')} for key, source in result['sources'].items()}},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
