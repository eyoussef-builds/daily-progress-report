"""Synthetic Claude fixtures; no real conversations, accounts or project data."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import collect_claude as cc
import collect_daily as daily

NOW = datetime(2025, 7, 16, 12, tzinfo=timezone.utc)
SID = '11111111-1111-4111-8111-111111111111'
FORK = '22222222-2222-4222-8222-222222222222'


def message(role, text, uuid='one', stamp='2025-07-15T06:00:00Z', **extra):
    return {'type': role, 'uuid': uuid, 'timestamp': stamp, 'sessionId': SID,
            'cwd': '/fictional-project', 'message': {'role': role,
            'content': [{'type': 'text', 'text': text}]}, **extra}


def write(root, records, name='session.jsonl', project='fictional-project'):
    path = root / 'projects' / project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
    return path


class ClaudeTests(unittest.TestCase):
    def run_collect(self, root, **kwargs):
        result, out = cc.collect(root, '2025-07-15', 'UTC', full_scan=True, now=NOW, **kwargs)
        self.addCleanup(shutil.rmtree, out)
        return result, out

    def test_mixed_reminders_preserve_actual_human_request(self):
        r = message('user', '<system-reminder>Internal routing text</system-reminder>\nMake the report clearer')
        self.assertEqual(cc.visible_text(r), 'Make the report clearer')
        self.assertEqual(cc.visible_text(message('user', '<system-reminder>Routing only</system-reminder>')), '')

    def test_tool_thinking_skill_and_compaction_are_excluded(self):
        r = message('assistant', 'Delivered')
        r['message']['content'] += [{'type': 'thinking', 'thinking': 'SECRET_REASONING'},
                                   {'type': 'tool_use', 'input': {'key': 'SECRET_TOOL'}},
                                   {'type': 'tool_result', 'content': 'SECRET_RESULT'}]
        self.assertEqual(cc.visible_text(r), 'Delivered')
        for text in ('Base directory for this skill: /fixture', '[Usage limit reached; internal]', '<task-notification>internal</task-notification>'):
            self.assertEqual(cc.visible_text(message('user', text)), '')
        self.assertEqual(cc.visible_text(message('user', 'Copied summary', isCompactSummary=True)), '')

    def test_day_boundary_and_original_fork_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prior = message('assistant', 'Older result', stamp='2025-07-14T23:59:59Z')
            today = message('assistant', 'New result', uuid='today')
            write(root, [prior, today])
            write(root, [dict(prior, timestamp='2025-07-15T07:00:00Z'), today,
                         message('assistant', 'Fork result', uuid='fork', sessionId=FORK)], name='fork.jsonl')
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['messages']['total_visible'], 2)
            self.assertEqual(len(result['threads']), 2)
            self.assertGreater(result['coverage']['duplicate_frames_removed'], 0)

    def test_streamed_text_and_usage_are_merged_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = message('assistant', 'First paragraph', uuid='part-a')
            a['message'].update(id='api-one', usage={'input_tokens': 10, 'cache_creation_input_tokens': 20,
                'cache_read_input_tokens': 100, 'output_tokens': 5})
            b = message('assistant', 'Final paragraph', uuid='part-b', stamp='2025-07-15T06:00:01Z')
            b['message'].update(id='api-one', stop_reason='end_turn', usage={'input_tokens': 10,
                'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 100, 'output_tokens': 15})
            write(root, [a, b, b, dict(a, uuid='repeat-fragment', timestamp='2025-07-15T06:00:02Z')])
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['messages']['assistant_updates'], 1)
            usage = result['metrics']['tokens']
            self.assertEqual(usage['per_response_records'], 1)
            self.assertEqual(usage['totals']['total_tokens'], 145)
            self.assertEqual(usage['totals']['total_input_tokens'], 130)
            evidence = json.loads(Path(result['threads'][0]['evidence_file']).read_text())
            self.assertEqual(evidence['events'][0]['text'], 'First paragraph\nFinal paragraph')
            self.assertEqual(evidence['events'][0]['phase'], 'end_turn')

    def test_user_prompt_fragments_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, [message('user', 'First', promptId='prompt-one'),
                         message('user', 'Second', uuid='two', promptId='prompt-one')])
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['messages']['user_side'], 1)

    def test_partial_usage_and_missing_usage_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = message('assistant', 'Result')
            a['message'].update(id='api-one', usage={'input_tokens': 10, 'output_tokens': 5})
            write(root, [a])
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['tokens']['status'], 'partial')
            self.assertEqual(result['metrics']['tokens']['totals']['total_tokens'], 15)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, [message('assistant', 'Result')])
            result, _ = self.run_collect(root)
            self.assertIsNone(result['metrics']['tokens']['totals'])

    def test_subagent_is_not_a_new_main_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, [message('assistant', 'Helper finding', agentId='helper', isSidechain=True)])
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['chats']['substantive_main'], 0)
            self.assertEqual(result['metrics']['chats']['supporting'], 1)

    def test_metadata_is_whitelisted_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'claude'
            write(root, [message('assistant', 'Done')])
            metadata = Path(tmp) / 'desktop'
            path = metadata / 'account' / 'workspace' / 'local_session.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'cliSessionId': SID, 'title': 'Fictional title',
                'remoteMcpServersConfig': {'secret': 'DO_NOT_EXPORT'}, 'createdAt': 1}))
            forbidden = root / '.credentials.json'
            forbidden.write_text('{invalid-json-DO_NOT_EXPORT')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(tmp).rglob('*') if p.is_file()}
            result, out = self.run_collect(root, metadata_root=metadata)
            self.assertEqual(result['threads'][0]['title'], 'Fictional title')
            self.assertNotIn('DO_NOT_EXPORT', ''.join(p.read_text() for p in out.glob('*.json')))
            for p, digest in before.items():
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), digest)
            self.assertEqual((out / 'index.json').stat().st_mode & 0o777, 0o600)

    def test_safe_output_names_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, [message('user', 'api_key=synthetic-private-value', sessionId='../escape')])
            result, out = self.run_collect(root)
            path = Path(result['threads'][0]['evidence_file'])
            self.assertEqual(path.parent, out)
            self.assertNotIn('synthetic-private-value', path.read_text())

    def test_full_scan_recovers_old_mtime_and_reports_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = write(root, [message('assistant', 'Found')])
            with p.open('a') as f:
                f.write('{unfinished')
            os.utime(p, (1, 1))
            result, _ = self.run_collect(root)
            self.assertEqual(result['metrics']['messages']['total_visible'], 1)
            self.assertEqual(result['coverage']['malformed_lines'], 1)

    def test_absent_claude_source_does_not_prevent_codex_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            croot = root / 'codex'
            (croot / 'sessions').mkdir(parents=True)
            record = {'type': 'event_msg', 'timestamp': '2025-07-15T06:00:00Z',
                'payload': {'type': 'agent_message', 'message': 'Codex result'}}
            meta = {'type': 'session_meta', 'timestamp': '2025-07-15T00:00:00Z',
                'payload': {'id': SID, 'timestamp': '2025-07-15T00:00:00Z'}}
            (croot / 'sessions/log.jsonl').write_text(json.dumps(meta) + '\n' + json.dumps(record))
            result, out = daily.collect('2025-07-15', 'UTC', codex_root=croot,
                claude_root=root / 'missing', full_scan=True, now=NOW)
            self.addCleanup(shutil.rmtree, out)
            self.assertEqual(result['sources']['claude_code']['status'], 'unavailable')
            self.assertEqual(result['metrics']['messages']['total_visible'], 1)
            self.assertIsNone(result['metrics']['tokens']['recorded_total_tokens'])

    def test_missing_root_is_not_zero_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                cc.collect(Path(tmp) / 'missing', '2025-07-15', 'UTC', now=NOW)

    def test_combined_counts_namespace_sources_and_normalize_cache_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            croot, aroot = root / 'codex', root / 'claude'
            (croot / 'sessions').mkdir(parents=True)
            records = [
                {'type': 'session_meta', 'timestamp': '2025-07-15T00:00:00Z',
                 'payload': {'id': SID, 'timestamp': '2025-07-15T00:00:00Z'}},
                {'type': 'event_msg', 'timestamp': '2025-07-15T06:00:00Z',
                 'payload': {'type': 'agent_message', 'id': 'same-id', 'message': 'First contribution'}},
                {'type': 'token_usage_record', 'timestamp': '2025-07-15T06:00:00Z',
                 'payload': {'thread_id': SID, 'response_id': 'same-id',
                             'usage': {'input_tokens': 100, 'cached_input_tokens': 80,
                                       'output_tokens': 10, 'total_tokens': 110}}}]
            (croot / 'sessions/log.jsonl').write_text('\n'.join(json.dumps(r) for r in records))
            response = message('assistant', 'Second contribution', uuid='same-id')
            response['message'].update(id='same-id', usage={'input_tokens': 1,
                'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3, 'output_tokens': 4})
            write(aroot, [response])
            result, out = daily.collect('2025-07-15', 'UTC', codex_root=croot,
                                       claude_root=aroot, full_scan=True, now=NOW)
            self.addCleanup(shutil.rmtree, out)
            self.assertEqual(result['metrics']['messages']['total_visible'], 2)
            self.assertEqual(result['metrics']['tokens']['recorded_total_tokens'], 120)
            self.assertEqual(result['metrics']['tokens']['status'], 'recorded')
            self.assertEqual(len({t['source_key'] for t in result['threads']}), 2)
            self.assertIsNone(result['metrics']['projects']['count'])


if __name__ == '__main__':
    unittest.main()
