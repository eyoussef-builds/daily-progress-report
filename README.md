# Daily Progress Report

[العربية](README.ar.md) · [Install for 15 agents](docs/agent-installation.md) · [Privacy](docs/privacy.md) · [MIT license](LICENSE)

**Ask what you achieved today. Get a clear timeline, outcomes by project, honest counts, and two matching infographics: English and Arabic.**

Imagine your work is spread across several notebooks. This skill teaches your assistant to open the notebooks it is allowed to read, find the pages from the day you asked about, and tell the story in order. It does not guess what is inside a closed notebook.

## What is a skill?

A skill is a folder with an instruction book named `SKILL.md`. This one also includes a small Python helper for reading local Codex records. The assistant reads the instructions, gathers evidence and writes the report. The helper gathers information; it cannot decide by itself whether a product really works.

This is a **Codex-progress workflow**. Other coding agents can load its instructions, but installing it does not make their own chat histories supported or give them access to Codex history. They need authorized Codex tools or readable Codex records. With no accessible evidence, they must explain the gap.

## Start here: the easiest installation

If your agent can download and install skills, paste:

```text
Install the daily-progress-report skill from
https://github.com/eyoussef-builds/daily-progress-report.
Read its installation guide and inspect the skill and helper scripts first.
Install the complete skills/daily-progress-report folder in the supported personal
skill directory for this agent. Preserve any existing customized copy.
Tell me how to invoke it. Do not read my history or run a report yet.
```

Read any download/file-access permission request. For a manual route, use the [guide for 15 agents](docs/agent-installation.md). It includes downloading without Git, Windows copying, and explicit instruction-import fallbacks.

## Your first report, step by step

1. Open an agent session that can read the Codex history you want summarized. A local session is usually the simplest place to use local logs.
2. In Codex, type:

   ```text
   Use $daily-progress-report. What have we done yesterday across my accessible
   Codex conversations? Use my local timezone.
   ```

3. If the timezone is unknown, tell it your IANA timezone, such as `UTC` or `Europe/London`. If you mean a particular day, give the exact date. “Today” stops at the stated collection time.
4. Let it inspect date-relevant chats, including old chats resumed that day and archives it can access. Other devices count only if their actual history can be read through existing tools or an explicitly supplied export.
5. Read the timeline and achievements. Follow useful chat/artifact links if you want to check a claim.
6. Check the counts and coverage note. “No recorded activity found” does not mean you did no work. It means the available records did not establish any.
7. At the end, receive **two separate generated images**, English first and Arabic second. They use the same facts and numbers. They show achieved outputs, with unfinished work kept in the written report only.

You can also say “report today's progress,” “daily recap,” or “what did we achieve yesterday?” Explicitly name the skill if automatic matching does not select it. Other agents have their own invocation syntax in the installation guide.

## What you get

| Part | What it tells you |
| --- | --- |
| Date and coverage | Which day, timezone, cutoff and accessible sources were used. |
| Timeline | The important observed milestones in time order. |
| Achievements by project | What was researched, designed, written, edited, built, tested or released. |
| Still open, in text | Material unfinished work and blockers, honestly separated from completed results. |
| Day in numbers | Verified project, chat, visible-message and recorded-token counts where available. |
| English infographic | An illustrated achievements recap and a clear bottom metrics strip. |
| Arabic infographic | The same recap in readable RTL Arabic, with Western digits `0–9`. |

The design shows one person directing several kinds of work through an AI hub: research, writing, design, coding, media editing or other evidenced streams. It shows useful outputs and revision stages, not just a pile of task names. The number of lanes changes with the actual day. Four chats about one business are still one project.

## A fictional example

Suppose you worked on a fictional meal app and a fictional newsletter. At 09:10 you finished the meal-app outline. At 11:30 you fixed its shopping list. At 15:00 you prepared a newsletter draft. The report can show **2 projects** and those three outcomes if the records support them. A newsletter scheduled for tomorrow must be called scheduled, not already sent.

These are invented teaching examples, not records from the creator's work. The public repository contains no real reports, screenshots, conversations or personal metrics.

## Understand the numbers without the jargon

- **Project:** the actual product, business or other coherent effort. Several chats or folders can belong to one project.
- **Visible messages:** deduplicated user-side and assistant messages, including progress updates. Tools, internal instructions and model reasoning are excluded. Automated entries are disclosed.
- **Recorded tokens:** small chunks of text processed by a model, counted only from dated per-response usage records when present. Re-reading cached context can make the number large. It is not a count of newly written words or a bill.
- Cached input and reasoning are parts of the recorded totals, not extra totals to add again. Missing or partially covered usage must be labeled rather than guessed.

The skill does not turn message counts into hours worked, hours saved, money earned, cost, or a productivity score. Overlapping chats do not prove you continuously worked on every stream at once.

## What you need

For a tool-based report, the host needs chat inventory/read access. For local logs, it needs Python 3.9+ with timezone data, file access to your configured Codex root, and the `zstd` executable if your retained files are compressed. The helper uses Python's standard library and does not need an API key. On some systems, Python timezone data needs separate installation.

Images need an actual image-generation tool in the host. This repository does not provide one or silently buy API usage. If generation is unavailable, the assistant should deliver the factual written report and explain which image outputs could not be produced. Native goals, scheduling and remote-device pairing are not prerequisites.

## Optional: run the local collector yourself

From the downloaded repository folder, replace the example date and timezone with yours:

```sh
python3 -B skills/daily-progress-report/scripts/collect_activity.py --date 2025-01-15 --timezone UTC
```

On Windows, use `py -3` instead of `python3` if that is your Python launcher. The default source is `CODEX_HOME`, otherwise `~/.codex`. Use `--codex-root` only for another authorized Codex history root. Run `--help` for all options.

The default output is a new private temporary evidence folder. The printed `index` tells the assistant where the evidence is. It includes private messages and paths, so do not upload it or commit it. Normal mode filters candidates by index/file update time, then actual event timestamps. `--full-scan` reads all retained logs when stale timestamps could hide activity; a large archive may take longer. Both modes report limitations.

To run the offline fixture tests, without reading your history:

```sh
python3 -B -m unittest discover -s skills/daily-progress-report/scripts -p 'test_*.py' -v
```

The fixtures use invented chats, messages and identifiers. Passing them checks the collector, not whether an arbitrary daily report is correct.

## Privacy, customization and limits

The collector reads history without modifying it and has no network-upload code. The host model and any image service still process the information you give them according to their own settings. Read [the privacy guide](docs/privacy.md). Obvious-token redaction is a precaution, not an anonymization guarantee.

Optional private defaults belong in your installed `references/local-profile.md`: timezone, language or presentation preferences. Keep it out of Git. Updates should preserve your custom copy. To uninstall, remove only the installed `daily-progress-report` folder or use the agent's uninstall control.

This is an on-demand retrospective. It does not send messages, publish reports, continue the summarized projects or create an automation merely because you requested a recap.

---

Created by [eyoussef-builds](https://github.com/eyoussef-builds) · [hello@eyoussef.me](mailto:hello@eyoussef.me) · [eyoussef.me](https://eyoussef.me)
