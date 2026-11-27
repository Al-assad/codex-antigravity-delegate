---
name: agy-delegate
description: Delegate a code or design review, document rewrite, or follow-up from Codex to an explicitly requested running Antigravity Desktop session (for example, Neon or Carbon). Also use to check or retrieve results for existing delegations. Do not use for general questions or reviews that do not request Antigravity Desktop.
---

# Delegate from Codex to Antigravity Desktop

Use the `Codex AGY Delegate` Sidecar with a running Antigravity Desktop. This is an interactive local handoff, not headless `agy -p`. In Codex Desktop, invoke it explicitly with `$agy-delegate`. In T3 Code, use its native delegation for ordinary cross-provider, isolated worktree, or PR review tasks; use this skill when the user specifically wants a Desktop conversation they can open and continue.

```sh
agy="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}/agy-delegate/scripts/agy_delegate.py"
"$agy" health
"$agy" review --cwd /path/to/repo --uncommitted --model neon --prompt 'Focus on regressions and edge cases.' --background
"$agy" doc --cwd /path/to/repo --file docs/plan.md --model carbon --prompt 'Make the plan concise without changing facts or open questions.' --background
```

## Commands

| Command | Use |
| --- | --- |
| `health` | Check the Sidecar, the Desktop integration, and its default project. |
| `review` | Request a read-only review of `--uncommitted` changes, changes since the merge base with `--base <branch>`, `--commit <sha>`, or one or more `--file` paths. |
| `doc` | Rewrite only the files listed with `--file`; without files, return a draft in the conversation. |
| `start` | Delegate a general task. Specify what may change. Supports `--cwd`, `--model`, `--title`, `--file`, and `--include-git`. `doc` also accepts `--include-git`. |
| `followup <task-id>` | Continue the same conversation and model. May use `--conversation-id <UUID>` and an optional `--title`. Following up by task ID preserves the original review or document-editing boundaries. |
| `status <task-id>` | Check Desktop `runState`, new transcript turns, elapsed time, and warnings. Also accepts `--conversation-id <UUID>`. |
| `wait <task-id> --timeout 90` | Wait for Desktop to be idle and a matching reply to appear. Exit `124` means timeout, not cancellation. |
| `result <task-id>` | Read the matching reply and current Git summary. Run `wait` first to avoid reading an intermediate reply. Verify any warning about manual messages in the conversation. |
| `list` | List recent local task records. Use `status` for live state. |

Pass the brief with `--prompt`, `--prompt-file`, or stdin. The default is to wait; `--background` returns a `task_id` for `status`, then `wait` and `result`. Commands accept `--json` or `--json-output`. If the Sidecar is offline, `--fallback-brief` can produce a brief for manual handoff. If submission is uncertain, check the ledger and Desktop before retrying.

The default is `neon`. Supported aliases are `neon` → `gemini-4-neon-high`, `neon-xhigh` → `gemini-4-neon-xhigh`, `neon-medium` → `gemini-4-neon-medium`, `carbon` → `gemini-4-carbon-high`, `argon` → `gemini-4-argon-high`, and `flash` → `gemini-3.8-flash-high`. Full model IDs are sent unchanged. The Sidecar rejects models not listed by Desktop rather than silently falling back.

## Share context and verify the handoff

- `--codex-session <UUID>` explicitly includes selected user and assistant text from a local Codex session. No conversation history is sent by default. Share only material appropriate for the receiving model and omit credentials. Offsets and prefix checksums advance only after submission is confirmed. If the source was rewritten, verify it before using `followup --reset-codex-offset` to resend recent excerpts.
- `--cwd` prefers a matching registered Desktop project. If none matches, the Sidecar uses its own default project and includes the absolute working directory in the brief; that does not grant access to the directory. Check `project_id` and `project_match` in the result.
- Read-only review and file-limited rewriting are task instructions, not OS sandbox restrictions. Verify edits and replies before accepting them. To cancel, open the conversation in Antigravity Desktop and click **Stop**.

## Setup and troubleshooting

Run `health` after installation. If Desktop has not picked up the enabled Sidecar, restart it and check again. The installer writes the absolute path of its Python interpreter into the installed `sidecar.json`, so the Sidecar can run even when Desktop has a limited `PATH`.

The local ledger defaults to `~/.local/state/agy-delegate/`; override it with `AGY_DELEGATE_STATE_DIR`. The CLI and Sidecar use `~/.local/state/agy-delegate/sidecar.sock` by default; set `AGY_DELEGATE_SOCKET` to the same custom path on both ends. The helper reads `~/.gemini/antigravity/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl` without modifying the transcript.
