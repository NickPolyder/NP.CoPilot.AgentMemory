# Session inbox notifier

Status: **implemented** (v0.9.3).

## Purpose

The `np-agent-memory-inbox` extension runs once for each Copilot CLI session.
It checks only that session's `workingDirectory` and reports unread inbox changes in that session's timeline.
The polling check uses no model tokens.

## Design

- The extension source is bundled at `.github/extensions/np-agent-memory-inbox/extension.mjs`.
  `.claude-plugin/plugin.json` declares `"extensions": ".github/extensions/"`,
  so Copilot loads the extension with the plugin.
- The installer adds `np-agent-memory` to uv's persistent tool directory and
  stores its absolute executable path in
  `$HOME\.copilot\np-agent-memory\settings.json`.
  The extension invokes that executable directly; it never runs `uvx` while
  polling.
- It obtains the current session working directory from
  `session.rpc.metadata.snapshot()` immediately after joining the foreground
  session, so it never observes another Copilot session's agent identity or
  waits for the first user prompt.
  A later `onSessionStart` directory is authoritative and cannot be replaced
  by a delayed metadata snapshot.
- It invokes `<uv-tool-bin>/np-agent-memory inbox-summary --agent-cwd <working-directory>`.
- The `inbox-summary` command is read-only and returns only canonical path, unread and urgent counts, and capped message IDs/priorities.
  It never returns message bodies, subjects, senders, or agent ULIDs.
- The extension logs only when it first observes unread messages or observes new unread message IDs.
  It does not read or acknowledge inbox messages.
- `onSessionEnd` stops the session-local poll timer.
- If the repository has not been registered yet, the notifier logs one warning
  and waits for `np-agent-memory-agent_register` rather than repeating failures.

## Settings

Create or edit `$HOME\.copilot\np-agent-memory\settings.json`:

```json
{
  "inboxNotifier": {
    "enabled": true,
    "pollIntervalSeconds": 60,
    "promptMode": "prompt-on-urgent",
    "executablePath": "C:\\Users\\NickP\\.local\\bin\\np-agent-memory.exe"
  }
}
```

`pollIntervalSeconds` is clamped to 10–3600 seconds.
`executablePath` is written by the installer and must be an absolute path.

| `promptMode` | Behaviour | Model tokens |
|---|---|---|
| `notify` | Logs inbox changes only. | No |
| `prompt-on-urgent` *(default)* | Logs changes and, for a new urgent message, sends one queued instruction to read the inbox. | Yes, only when triggered |
| `prompt-on-any` | Logs changes and sends one queued instruction for any new message. | Yes, only when triggered |

Prompts contain only unread counts and urgency.
Before the first user/model turn, a new qualifying message prompts immediately.
Once work has begun, prompts are queued until the session becomes idle.
They tell the agent to use `np-agent-memory-inbox_check`; they never include
inbox content or perform reads/acknowledgements automatically.

## Configuration and activation

After installing or updating the plugin, run the configuration script from the
plugin's installed directory:

```powershell
./install-inbox-notifier.ps1
```

This installs the uv tool and records its executable path without copying or
relocating the bundled extension. Restart Copilot CLI, or run `/clear`, after
configuration.
