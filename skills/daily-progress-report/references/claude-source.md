# Claude Code sources and cross-agent attribution

The combined entrypoint is `scripts/collect_daily.py`. It collects accessible local Codex and Claude Code history by default, with separate source indexes and one combined index. The existing `collect_activity.py` remains Codex-only; `collect_claude.py` can be run independently. Read each source's coverage and selected dated evidence before writing achievements.

## What Claude history is supported

- Local Claude Code JSONL under `CLAUDE_CONFIG_DIR/projects`, otherwise `~/.claude/projects`. CLI and desktop Code sessions can appear there. The adapter filters actual record timestamps, including old sessions resumed during the day. `--full-scan` also covers stale/copied file modification times.
- Optional desktop session metadata provides display titles and fork relationships. On macOS the combined collector checks the current user's `Library/Application Support/Claude/claude-code-sessions` directory when using the default Claude root. It reads only `local_*.json` session records and exports a strict whitelist of display/provenance fields. An explicitly supplied alternate root does not implicitly read the current user's desktop metadata; pass `--claude-desktop-root` if appropriate.
- The collector never reads credentials, settings, cookies, browser databases, app caches, memory files, raw tool input/output or model thinking. Tool results may contain other conversations or secrets and cannot be treated as new achievements. Only text blocks are retained; image-only content is not interpreted by the helper.
- Ordinary claude.ai chats, Cowork/cloud-only sessions, deleted transcripts and other devices are **not** covered by this adapter. Read a separately authorized connector/UI/export when available and needed, then label its actual scope. Never infer “all Claude chats” from local Code logs. A cloud session visible in the desktop app is not necessarily stored locally.

Configuration location is documented by [Anthropic](https://code.claude.com/docs/en/env-vars). [Desktop documentation](https://code.claude.com/docs/en/desktop) distinguishes local and cloud environments. The specific JSONL and desktop metadata fields handled here are observed formats, not a guaranteed public API. Preserve warnings and adapt with synthetic tests when they change.

## Deduplication and real user text

Forks can repeat their parent's history. Preserve original session/message IDs and timestamps; deduplicate UUIDs across files before selecting the day. Count a fork's actual new activity separately, not the copied accomplishments. Merge assistant text blocks by API message ID and human text fragments by prompt ID. Exclude internal reminders, compaction summaries, skill injections and usage-limit notices. When an internal reminder and the actual user's request share a text block, remove only the reminder and retain the request.

Subagent work belongs to its parent project and is counted separately from main chats. Record counts are not semantic project counts. When merging with Codex, keep source-qualified chat IDs; identical UUID strings from two products do not establish that they are the same chat. A project worked on in both products counts as one actual project, with each milestone attributed to its evidenced agent. Deduplicate the achievement, not legitimate separate messages in the two apps.

Assistant responses can contain multiple text blocks. Their first and last observed timestamps are retained. A final delivery should use the last observed time when appropriate; do not attribute a long response's final result to its first fragment. A reported completion remains reported unless a dated artifact independently corroborates it.

## Usage accounting

The combined index keeps provider breakdowns even when a recorded total is available:

- **Codex:** `total_tokens` already includes input and output. Its cached input and reasoning are subsets; never add them again.
- **Claude Code:** deduplicate by assistant API message ID. Repeated/streamed usage snapshots use the maximum observed value of each field, not a sum. Recorded processing total is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`. The cache-write/read fields are separate from uncached input. Nested cache details are not added again. Usage is assigned to its earliest retained record time, which can differ from the final message time.
- Missing fields or chats without usage are partial/unavailable, not invented zeros. Image generation, external tools and cloud-only sessions may lack usage records. Do not compare provider token volume as efficiency, useful output or cost. A combined total is only the sum of the explicitly recorded components, with scope/partial coverage preserved.

## Daily presentation

Add an **Agent/source** column or concise source labels to the timeline and group related work by actual project. The scale strip may show combined recorded counts with a source breakdown in the written report. State Codex and Claude Code coverage independently. Keep attribution consistent in Facebook/LinkedIn drafts and both images; do not describe Claude's build as Codex's build or copy private chat text into public sharing assets.
