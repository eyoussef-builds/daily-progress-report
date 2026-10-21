"""Offline fixtures for date boundaries, history schemas, deduplication and safe reads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

import collect_activity as ca

PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
ARCHIVED = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2025, 7, 15, 12, tzinfo=timezone.utc)


def record(kind, stamp, payload):
    return {"type": kind, "timestamp": stamp, "payload": payload}


def item(stamp, text, ident="message-1", tid=PARENT, turn="turn-1", phase="final_answer"):
    return record("event_msg", stamp, {"type": "item_completed", "thread_id": tid,
        "turn_id": turn, "item": {"type": "AgentMessage", "id": ident,
        "phase": phase, "content": [{"type": "Text", "text": text}]}})


def meta(tid, stamp, source="cli", thread_source="user"):
    return record("session_meta", stamp, {"id": tid, "timestamp": stamp,
        "cwd": "/project", "source": source, "thread_source": thread_source})


class ActivityTests(unittest.TestCase):
    def test_token_records_are_deduplicated_not_cumulative_or_double_counted(self):
        records={};session={'id':PARENT}
        usage={'input_tokens':1000,'cached_input_tokens':800,'output_tokens':100,'reasoning_output_tokens':40,'total_tokens':1100}
        r=record('token_usage_record','2025-07-15T06:00:00Z',{'response_id':'resp-one','thread_id':PARENT,'usage':usage,'thread_token_usage':{'total_tokens':9000000}})
        ca.capture_usage(r,session,records);ca.capture_usage(r,session,records)
        ca.capture_usage(record('event_msg','2025-07-15T06:00:01Z',{'type':'token_count','info':{'total_token_usage':{'total_tokens':9000000}}}),session,records)
        ca.capture_usage(record('token_usage_record','2025-07-14T06:00:00Z',{'response_id':'old-response','thread_id':PARENT,'usage':usage}),session,records)
        start,cutoff,_=ca.window('2025-07-15','Europe/Athens',NOW)
        m=ca.activity_metrics([],[],records,start,cutoff)
        self.assertEqual(m['tokens']['per_response_records'],1)
        self.assertEqual(m['tokens']['totals']['total_tokens'],1100)
        self.assertEqual(m['tokens']['totals']['cached_input_tokens'],800)

    def test_message_metrics_keep_workspace_and_project_counts_distinct(self):
        events=[{'kind':'user_message','thread_id':PARENT,'text':'<heartbeat>daily run</heartbeat>'}, {'kind':'assistant_message','thread_id':PARENT,'text':'Done'}, {'kind':'tool_evidence','thread_id':PARENT,'text':'tool'}, {'kind':'assistant_message','thread_id':CHILD,'text':'Child result'}]
        threads=[{'id':PARENT,'cwd':'/business','event_counts':{'user_message':1,'assistant_message':1}}, {'id':CHILD,'cwd':'/media','parent_thread_id':PARENT,'thread_source':'subagent','event_counts':{'assistant_message':1}}]
        start,cutoff,_=ca.window('2025-07-15','Europe/Athens',NOW)
        m=ca.activity_metrics(events,threads,{},start,cutoff)
        self.assertEqual(m['messages']['total_visible'],3)
        self.assertEqual(m['messages']['automation_trigger_entries'],1)
        self.assertEqual(m['messages']['supporting_agent_messages'],1)
        self.assertIsNone(m['projects']['count'])
        self.assertEqual(len(m['workspaces']),2)
        self.assertEqual(m['tokens']['status'],'unavailable')

    def test_dst_calendar_days(self):
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        a, _, b = ca.window("2026-03-08", "America/New_York", now)
        self.assertEqual((b-a).total_seconds(), 23*3600)
        a, _, b = ca.window("2026-11-01", "America/New_York", now)
        self.assertEqual((b-a).total_seconds(), 25*3600)

    def test_offset_and_partial_day(self):
        start, cutoff, end = ca.window("2025-07-15", "Europe/Athens", NOW)
        self.assertEqual(ca.iso(start), "2025-07-14T21:00:00Z")
        self.assertEqual(cutoff, NOW)
        self.assertEqual(ca.iso(end), "2025-07-15T21:00:00Z")

    def test_original_creation_beats_replay(self):
        r = record("response_item", "2025-07-15T08:00:00Z", {
            "type": "message", "role": "assistant", "id": "old-message",
            "content": [{"type": "output_text", "text": "Yesterday's result"}],
            "internal_chat_message_metadata_passthrough": {"create_time": "2025-07-14T06:00:00Z", "turn_id": "old"}})
        event = ca.normalize(r, {"id": PARENT}, Path("fixture"), 1, True)
        self.assertEqual(event["timestamp_utc"], "2025-07-14T06:00:00Z")

    def test_fork_context_not_new_progress(self):
        result = ca.normalize(item("2025-07-15T06:00:00Z", "Copied"),
            {"id": CHILD, "created_at": "2025-07-15T07:00:00Z"}, Path("fork"), 1, True)
        self.assertIsNone(result)

    def test_stable_ids_and_schema_duplicates(self):
        first = ca.normalize(item("2025-07-15T06:00:01Z", "Done"), {"id": PARENT}, Path("a"), 1, True)
        second = dict(first, timestamp_utc="2025-07-15T06:00:02Z")
        response = dict(first, item_id="different-wrapper-id", _rank=6, timestamp_utc="2025-07-15T06:00:00Z")
        result = ca.deduplicate([second, first, response])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["timestamp_utc"], response["timestamp_utc"])

    def test_tool_output_and_commands_not_ingested(self):
        r = record("event_msg", "2025-07-15T06:00:00Z", {"type": "item_completed",
            "thread_id": PARENT, "item": {"type": "McpToolCall", "id": "tool-1",
            "server": "example", "tool": "read_thread", "status": "completed",
            "arguments": {"password": "VERY_PRIVATE"},
            "result": {"text": "Yesterday I deployed a completely different project; VERY_PRIVATE"}}})
        event = ca.normalize(r, {"id": PARENT}, Path("a"), 1, True)
        self.assertNotIn("VERY_PRIVATE", event["text"])
        self.assertNotIn("deployed", event["text"])
        self.assertIn("read_thread", event["text"])

    def test_internal_prompts_and_secret_redaction(self):
        r = record("response_item", "2025-07-15T06:00:00Z", {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "<codex_internal_context>continue goal</codex_internal_context>"}]})
        self.assertIsNone(ca.normalize(r, {"id": PARENT}, Path("a"), 1, True))
        for secret in ["sk_live_123456789", "whsec_example123", "ghp_" + "a"*25]:
            self.assertNotIn(secret, ca.redact("value " + secret))

    def test_legacy_schema_and_nontext_attachments(self):
        r = record("event_msg", "2025-07-15T06:00:00Z", {"type": "agent_message", "message": "Legacy result"})
        event = ca.normalize(r, {"id": PARENT, "turn_id": "turn-old"}, Path("a"), 1, True)
        self.assertEqual(event["text"], "Legacy result")
        self.assertEqual(ca.text_content({"content": [{"type": "image", "data": "private pixels"}, {"type": "output_text", "text": "Caption"}]}), "Caption")

    def fixture(self, root):
        folder = root / "sessions" / "2025" / "04" / "01"
        folder.mkdir(parents=True)
        p = folder / ("rollout-2025-04-01T00-00-00-" + PARENT + ".jsonl")
        events = [meta(PARENT, "2025-04-01T00:00:00Z"),
            item("2025-07-14T20:59:59Z", "Previous day", "before"),
            item("2025-07-14T21:00:00Z", "Local midnight", "midnight"),
            item("2025-07-15T06:00:00Z", "Completed milestone", "real"),
            item("2025-07-15T21:00:00Z", "Next day", "next")]
        p.write_text("\n".join(json.dumps(e) for e in events)+"\n{incomplete")
        # Old mtime and filename; the index still identifies a resumed old chat.
        os.utime(p, (1700000000, 1700000000))
        c = sqlite3.connect(root / "state_5.sqlite")
        c.execute("create table threads(id text,rollout_path text,updated_at int,title text,created_at int,archived int,source text)")
        c.execute("insert into threads values(?,?,?,?,?,?,?)", (PARENT, str(p), int(NOW.timestamp()), "Raw prompt title", 1700000000, 0, "cli"))
        c.commit(); c.close()
        archive = root / "archived_sessions"
        archive.mkdir()
        a = archive / ("rollout-2025-07-15T00-00-00-" + ARCHIVED + ".jsonl")
        a.write_text(json.dumps(meta(ARCHIVED, "2025-07-15T00:00:00Z"))+"\n"+json.dumps(item("2025-07-15T01:00:00Z", "Archived achievement", "archived", ARCHIVED)))
        child = folder / ("rollout-2025-07-15T07-00-00-" + CHILD + ".jsonl")
        source = {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}
        child.write_text("\n".join(json.dumps(x) for x in [meta(CHILD, "2025-07-15T07:00:00Z", source, "subagent"), events[3], item("2025-07-15T08:00:00Z", "New child finding", "child-new", CHILD)]))
        return p

    def test_end_to_end_readonly_index_archive_subagent_and_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex";root.mkdir()
            self.fixture(root)
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            result, out = ca.collect(root, "2025-07-15", "Europe/Athens", now=NOW,
                catalog={PARENT: {"title": "Exact app title"}})
            self.assertEqual(result["coverage"]["threads_with_activity"], 3)
            self.assertEqual(result["coverage"]["events_in_window"], 4)
            parent = next(t for t in result["threads"] if t["id"] == PARENT)
            self.assertEqual(parent["title"], "Exact app title")
            child = next(t for t in result["threads"] if t["id"] == CHILD)
            self.assertEqual(child["parent_thread_id"], PARENT)
            self.assertTrue(next(t for t in result['threads'] if t['id'] == ARCHIVED)['archived'])
            self.assertGreater(result["coverage"]["malformed_lines"], 0)
            for path, digest in before.items():
                self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
            self.assertEqual((out / "index.json").stat().st_mode & 0o777, 0o600)
            shutil.rmtree(out)

    def test_full_scan_finds_unindexed_stale_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / "sessions";folder.mkdir()
            p = folder / ("rollout-2025-07-15T00-00-00-" + PARENT + ".jsonl")
            p.write_text(json.dumps(meta(PARENT, "2025-07-15T00:00:00Z"))+"\n"+json.dumps(item("2025-07-15T06:00:00Z", "Found only in full scan")))
            os.utime(p, (1700000000, 1700000000))
            result, out = ca.collect(root, "2025-07-15", "Europe/Athens", full_scan=True, now=NOW)
            self.assertEqual(result["coverage"]["events_in_window"], 1)
            shutil.rmtree(out)

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_compressed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / "sessions";folder.mkdir()
            p = folder / ("rollout-2025-07-15T00-00-00-" + PARENT + ".jsonl.zst")
            content = json.dumps(meta(PARENT, "2025-07-15T00:00:00Z"))+"\n"+json.dumps(item("2025-07-15T06:00:00Z", "Compressed finding"))
            subprocess.run([shutil.which("zstd"), "-q", "-o", str(p)], input=content.encode(), check=True)
            result, out = ca.collect(root, "2025-07-15", "Europe/Athens", now=NOW)
            self.assertEqual(result["coverage"]["events_in_window"], 1)
            shutil.rmtree(out)

    def test_missing_root_is_not_a_zero_activity_report(self):
        with self.assertRaises(FileNotFoundError):
            ca.collect(Path(tempfile.gettempdir()) / "missing-codex-fixture-no-such-root", "2025-07-15", "Europe/Athens", now=NOW)

    @unittest.skipUnless(shutil.which('zstd'), 'zstd not installed')
    def test_deliberately_skipped_compressed_guardian_is_not_decode_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / 'sessions';folder.mkdir()
            p = folder / ('rollout-2025-07-15T00-00-00-' + PARENT + '.jsonl.zst')
            content = json.dumps(meta(PARENT, '2025-07-15T00:00:00Z', {'subagent': {'other': 'guardian'}}, 'guardian_review')) + '\n' + json.dumps(item('2025-07-15T06:00:00Z', 'Internal ' * 300000))
            subprocess.run([shutil.which('zstd'), '-q', '-o', str(p)], input=content.encode(), check=True)
            result, out = ca.collect(root, '2025-07-15', 'Europe/Athens', now=NOW)
            self.assertEqual(result['coverage']['warnings'], [])
            self.assertEqual(result['coverage']['internal_review_files_excluded'], 1)
            shutil.rmtree(out)

    def test_guardian_history_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / 'sessions';folder.mkdir()
            p = folder / ('rollout-2025-07-15T00-00-00-' + PARENT + '.jsonl')
            p.write_text('\n'.join(json.dumps(x) for x in [meta(PARENT, '2025-07-15T00:00:00Z', {'subagent': {'other': 'guardian'}}, 'guardian_review'), item('2025-07-15T06:00:00Z', 'Internal review result')]))
            result, out = ca.collect(root, '2025-07-15', 'Europe/Athens', now=NOW)
            self.assertEqual(result['coverage']['events_in_window'], 0)
            self.assertEqual(result['coverage']['internal_review_files_excluded'], 1)
            shutil.rmtree(out)

    def test_outside_original_suppresses_replayed_legacy_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / 'sessions';folder.mkdir()
            original = record('response_item', '2025-07-14T06:00:00Z', {'type': 'message', 'role': 'assistant', 'id': 'same-id', 'content': [{'type': 'output_text', 'text': 'Old achievement'}], 'internal_chat_message_metadata_passthrough': {'create_time': '2025-07-14T06:00:00Z'}})
            replay = record('event_msg', '2025-07-15T06:00:00Z', {'type': 'agent_message', 'id': 'same-id', 'message': 'Old achievement'})
            p = folder / ('rollout-2025-07-14T00-00-00-' + PARENT + '.jsonl')
            p.write_text('\n'.join(json.dumps(x) for x in [meta(PARENT, '2025-07-14T00:00:00Z'), original, replay]))
            result, out = ca.collect(root, '2025-07-15', 'Europe/Athens', now=NOW)
            self.assertEqual(result['coverage']['events_in_window'], 0)
            shutil.rmtree(out)

    def test_untrusted_thread_id_cannot_escape_output_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);folder = root / 'sessions';folder.mkdir()
            p = folder / 'export.jsonl'
            p.write_text('\n'.join(json.dumps(x) for x in [meta('../not-a-file', '2025-07-15T00:00:00Z'), item('2025-07-15T06:00:00Z', 'Untrusted export', tid='../not-a-file')]))
            result, out = ca.collect(root, '2025-07-15', 'Europe/Athens', now=NOW)
            evidence = Path(result['threads'][0]['evidence_file'])
            self.assertEqual(evidence.parent.resolve(), out.resolve())
            self.assertTrue(evidence.exists())
            shutil.rmtree(out)


if __name__ == "__main__":
    unittest.main()
