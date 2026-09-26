# GPT Manager

A local desktop library for Codex, Claude Code, Gemini CLI, and Antigravity / agy conversations. Browse provider stores, inspect transcripts and original files, organize contexts, resume a conversation in a terminal, and carry contexts between machines.

![GPT Manager with synthetic demonstration contexts](docs/desktop.png)

## Run

Requires **Node.js 22.12+** (24 recommended), **Python 3.10+**, and a graphical desktop. Linux is the first supported platform.

```sh
nvm use                    # if you use nvm; reads .nvmrc
npm install
npm start
```

If your npm configuration disables install scripts, download Electron's runtime explicitly:

```sh
node node_modules/electron/install.js
```

Provider CLIs must be installed, authenticated, and on the launching shell's PATH. GPT Manager uses their existing installations and approval controls; it does not need a separate API key. `GPT_MANAGER_PYTHON` can select a Python executable. The app opens no HTTP server, loads no remote UI, and sends no chats to a service. Resuming a provider uses that provider's normal network behavior.

## What works

- One library with provider filters, metadata search, sorting, and multi-selection.
- Read-only store discovery, bounded scans for large histories, and paginated transcripts. The library searches titles, projects, tags, and IDs; it does not yet index full transcript text.
- Editable display titles, tags, notes, stars, and manager-only archive status.
- Provider root locations, additional custom roots, and reveal-in-file-manager.
- **Chat here**: real interactive PTYs rendered with xterm.js, up to eight terminal tabs. Tabs persist while navigating, resize with the window, and ask before stopping a running process.
- **Terminal**: opens a Linux system terminal in the recorded project directory with the specific conversation resumed. Missing project directories trigger a folder picker.
- Portable `.gptctx` archives containing original context files, a versioned manifest, SHA-256 checksums, and manager annotations.
- Archive validation and preview, import into an isolated library, and explicit conflict-safe restoration of original files.

### Discovery and resume adapters

| Provider | Default store | Resume command |
| --- | --- | --- |
| Codex | `$CODEX_HOME` or `~/.codex`, including `sessions` and `archived_sessions` | `codex resume SESSION_ID` |
| Claude Code | `$CLAUDE_CONFIG_DIR/projects` or `~/.claude/projects` | `claude --resume SESSION_ID` |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json` | `gemini --resume SESSION_ID` |
| Antigravity CLI | `~/.gemini/antigravity-cli` | `agy --conversation SESSION_ID` |
| Antigravity IDE | `~/.gemini/antigravity` | Binary preservation and readable transcript inspection where available; no IDE resume adapter |

Codex/Claude custom roots are passed through their environment variables when resuming. Other custom stores can be inspected and exported, but their CLI must already be configured to find them. Gemini sessions are project-specific; choose the correct folder if the file does not record it. agy workspace paths are read from cached metadata, the summary database, or matching history entries when available.

CLI syntax was checked against installed `codex`, `claude`, and `agy` help, the [official Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli), and [Gemini session management](https://geminicli.com/docs/cli/session-management/). Store formats are not stable public APIs, so adapters tolerate missing fields and preserve original files. Electron uses a sandboxed renderer, context isolation, and a restricted preload bridge following its [security guidance](https://www.electronjs.org/docs/latest/tutorial/security).

## Backup, restore, and teleport

1. Select contexts and choose **Export selected**. Use a new `.gptctx` filename.
2. Move that file to another machine using your preferred transfer channel.
3. In the other GPT Manager, choose **Import**, review the verified archive, and import it into the library.
4. Read imported contexts immediately. To resume natively, choose **Restore files** on an imported context and select the destination provider root, or use an empty staging folder to inspect the restored layout first.
5. Refresh the manager after restoring to a connected provider store, then resume the local copy.

This is file-based teleportation. Direct peer pairing/network sync and cross-provider conversion are not implemented. Imported contexts are library copies until explicitly restored. Restore rejects conflicts and does not overwrite files. Provider source files are untouched by browsing and organization.

Archives are **not encrypted** and contain private conversations and potentially sensitive tool outputs. Global authentication files, settings, provider indexes, and source repositories are not exported. Claude session companion files and Antigravity conversation databases/brain artifacts are included. SQLite databases use the SQLite backup API to include committed WAL data. Transcripts are copied up to their initial length; a concurrent last partial record is retained but skipped by the viewer. Pause active agents for the most coherent multi-file backup.

A native provider may need its index rebuilt or a matching project checkout before it can resume a restored session. Original project paths are preserved, not rewritten. In particular, Antigravity has shared summary/index state beyond the per-conversation database. **Archive round-trips and file restoration are tested; seamless native resume after migration is not guaranteed.** Backup whole provider installations separately when you need complete application-state disaster recovery.

## Implementation

- Electron desktop shell with a sandboxed, local-only renderer.
- Python standard-library backend over private stdin/stdout RPC; no listening port.
- POSIX PTY subprocess bridge and xterm.js for embedded CLI interaction.
- Manager metadata and imported archives in Electron's user-data directory (`~/.config/gpt-manager` on typical Linux installations). The Locations screen shows the actual path.
- Override `GPT_MANAGER_HOME` and `GPT_MANAGER_DATA` for isolated fixtures or alternate environments. `CODEX_HOME` / `CLAUDE_CONFIG_DIR` still take precedence over home defaults.

Embedded terminals work through POSIX PTYs (Linux/macOS); external terminal launch targets Linux (`x-terminal-emulator`, GNOME Terminal, Konsole, or xterm). macOS has not been validated, and Windows ConPTY is not implemented. No distributable installer is packaged yet.

## Verification

```sh
npm run check
npm test
```

The tests cover provider parsing, annotation persistence, original-byte export/import/restore, conflict refusal, checksum tampering, path traversal, symlinks, undeclared files, SQLite WAL snapshots, pagination, and real PTY input/resize/exit. Resume-command tests verify provider arguments and reject invalid session IDs.

A desktop smoke test uses synthetic provider stores and a fake interactive CLI, never live conversations:

```sh
python3 tests/seed_demo.py /tmp/gpt-manager-demo
PATH="/tmp/gpt-manager-demo/bin:$PATH" \
GPT_MANAGER_HOME=/tmp/gpt-manager-demo \
GPT_MANAGER_DATA=/tmp/gpt-manager-smoke-data \
GPT_MANAGER_SCREENSHOT=/tmp/gpt-manager-desktop.png \
npm start -- --smoke-test
```

The smoke test requires a working graphical display. It checks discovery, transcript rendering, search, interactive PTY round-trips, and process exit, then captures the desktop and quits. Use the synthetic seed data exactly as shown; this test expects the fake `codex` executable.
