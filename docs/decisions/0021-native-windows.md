# 0021 — Native Windows support

- Status: accepted
- Date: 2026-11-12

## Context

Forge v0.1.0 supports macOS, Linux and WSL (spec §17). Native Windows was a v1 roadmap item (§19). The user asked for it now: Forge must work from PowerShell and cmd, without WSL, and macOS and Linux must not regress.

A review of the code found these Windows problems:

| Area | Problem on native Windows |
| --- | --- |
| Claim lock (`core/pending.py`) | It uses `fcntl.flock`, which Windows lacks. `require_flock` refused every claim, release and handoff (exit 5). |
| Process liveness | `os.kill(pid, 0)` terminates the process on Windows. `pid_running` therefore always answered "alive", so an interrupted claim was never taken over automatically. |
| Text encoding | `read_text()`, `write_text()` and `subprocess(text=True)` without `encoding=` use the ANSI code page (cp1252, …). git and gh output is UTF-8. Non-ASCII issue titles, paths or comments are garbled, or raise `UnicodeEncodeError`. |
| Piped output | Agents read `--json` through a pipe. On Windows, a piped stdout uses the ANSI code page. The envelope is written with `ensure_ascii=False`, so a non-ASCII character can crash the command. |
| Executables | `subprocess` finds only `.exe` files by bare name. `codex` and `claude` installed by npm are `.cmd` shims, so `forge setup` reported them missing. |
| Per-user files | The profile and the machine id followed `$HOME/.config` and `$HOME/.local/state`. On Windows `HOME` is usually unset or points to Git Bash's home. |
| Line endings | Git for Windows checks text files out with CRLF by default (`core.autocrlf=true`). `adapters generate --check`, `docs --check` and `init` compared bytes, so a clean checkout looked like drift. |
| Atomic writes | `os.replace` fails with `PermissionError` while another process (editor, antivirus, another Forge run) has the target open. |
| Editor | The fallback editor is `vi`. The `$EDITOR` value was split with POSIX rules, which eat the backslashes in `C:\Tools\ed.exe`. |
| Long paths | Task worktrees live next to the clone. Deep trees (`node_modules`) pass the 260-character limit, and git then fails to check files out unless `core.longpaths` is on. |
| Claude Code subagents | Forge renders `shell` as `Bash`. On Windows without Git Bash, Claude Code runs commands with its `PowerShell` tool, so such a subagent had no shell. |
| Installer | `install.sh` refuses Windows and points to WSL. |

### Tool verification (2026-11-12)

Checked against the official documentation on 2026-11-12.

- **Claude Code** ([setup](https://code.claude.com/docs/en/setup), [tools reference](https://code.claude.com/docs/en/tools-reference), [sub-agents](https://code.claude.com/docs/en/sub-agents)):
  - Native Windows 10 1809+ is supported. The native installer puts `claude.exe` in `%USERPROFILE%\.local\bin`. npm also installs it (`claude.exe` behind npm's shims).
  - With Git for Windows, the `Bash` tool uses Git Bash. Without it, commands run through the `PowerShell` tool. Tool names in a subagent's `tools:` list are exact strings, and `PowerShell` is one of them.
  - A subagent whose `tools` list resolves to nothing is refused. The documentation does not say what happens to a single unavailable entry in an otherwise valid list.
  - `.claude/agents/`, `.claude/skills/<name>/SKILL.md` and `CLAUDE.md` are discovered the same way on Windows. `~/.claude` means `%USERPROFILE%\.claude`.
- **Codex** ([Windows](https://learn.chatgpt.com/docs/windows/windows-sandbox), [CLI](https://learn.chatgpt.com/docs/codex/cli.md), [skills](https://learn.chatgpt.com/docs/build-skills.md), [subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents.md)):
  - Native Windows is supported, with its own sandbox, and WSL2 is optional. The standalone installer puts `codex.exe` in `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin`. npm installs a Node launcher, which npm exposes as a `.cmd` shim.
  - `AGENTS.md`, `.agents/skills/` and `.codex/agents/*.toml` are discovered the same way. `CODEX_HOME` defaults to `%USERPROFILE%\.codex`.
- **uv** ([installation](https://docs.astral.sh/uv/getting-started/installation/), [storage](https://docs.astral.sh/uv/reference/storage/)): `irm https://astral.sh/uv/install.ps1 | iex`. uv and the tools that `uv tool install` installs go to `%USERPROFILE%\.local\bin`. `uv tool update-shell` writes the user `PATH` in the registry.
- **GitHub CLI** ([Windows install](https://github.com/cli/cli/blob/trunk/docs/install_windows.md)): `winget install --id GitHub.cli --source winget`, or the MSI from the releases page.
- **Ollama** ([Windows](https://docs.ollama.com/windows)): a native Windows application. The `ollama` CLI works in cmd and PowerShell.

## Decision

**Supported platforms.** macOS, Linux (including WSL) and native Windows 10/11 with Git for Windows ≥ 2.38, `gh` and Python ≥ 3.11 (provisioned by uv). Forge runs the same from PowerShell, cmd or Git Bash.

**One module for platform differences: `core/system.py`.** Every function that depends on the platform takes an optional `platform` argument (default `sys.platform`), so tests cover every branch on any machine:

| Function | macOS / Linux | Windows |
| --- | --- | --- |
| `config_home()` | `$XDG_CONFIG_HOME`, else `$HOME/.config` | `$XDG_CONFIG_HOME`, else `%APPDATA%` |
| `state_home()` | `$XDG_STATE_HOME`, else `$HOME/.local/state` | `$XDG_STATE_HOME`, else `%LOCALAPPDATA%` |
| `pid_running()` | `os.kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess` (ctypes) |
| `resolve_argv()` | `shutil.which` on the program | same; `PATHEXT` finds `.cmd` shims |
| `default_editor()` | `vi` | `notepad` |
| `replace_file()` | `os.replace` | `os.replace`, retried for up to 1 s on `PermissionError` |
| `utf8_stdio()` | no-op | stdout and stderr reconfigured to UTF-8 |
| `same_text()` | CRLF and LF compare equal | same |

So the profile is `%APPDATA%\forge\profile.yml` and the machine id is `%LOCALAPPDATA%\forge\machine-id` on Windows. Setting `XDG_CONFIG_HOME` or `XDG_STATE_HOME` to an absolute path overrides the default on every platform.

**Claim lock.** `task_lock` keeps its contract: an exclusive per-task lock file under `<git-common-dir>/forge/pending/` that the OS releases when its process dies. macOS and Linux keep `flock`. Windows uses `msvcrt.locking` (`LockFileEx`) on one byte at offset 2³⁰. Windows locks are mandatory, and that offset keeps the holder JSON at the start of the file readable. Windows cannot remove an open file, so the holder unlocks and closes the file first, then removes it. If a waiter has the file open, the removal fails and the file stays; the waiter then locks that same file, so two lock files for one task never exist. `require_flock` and its exit-5 refusal are removed. Claims, releases, reclaims and handoffs work on Windows, and so does the automatic takeover of an interrupted claim (ADR 0009).

**UTF-8 everywhere.**
- Every text file Forge reads or writes uses `encoding="utf-8"`, and every write uses `newline="\n"`.
- Every git and gh subprocess uses `encoding="utf-8"`.
- Check, resolver and setup commands are decoded as UTF-8 with `errors="replace"`, because their output is only shown, never parsed.
- The root command switches stdout and stderr to UTF-8 on Windows before anything is printed.
- `tests/test_portability.py` reads `src/forge` and fails on any text I/O or text-mode subprocess without an explicit encoding. It also fails on any `os.replace` or `os.kill` outside `core/system.py`, and on any `fcntl` or `msvcrt` import outside `core/pending.py`.

**Line endings.** Forge keeps writing LF. Every check that compares a checked-out file with what Forge would write treats CRLF and LF as equal, and does not rewrite a file that differs only in line endings. These checks are `adapters generate [--check]`, `docs --check` and `init`'s unchanged/overwrite decision. Machine-level files (`--local`) are never checked out by git, so they are compared byte for byte. The framework repository gets a `.gitattributes` with `* text=auto eol=lf`, so templates and snapshots stay LF in Windows checkouts and in the git clone that uv builds the wheel from. `forge init` does not add line-ending rules to the product repository: that is the team's choice.

**Shell commands.** `checks`, `regenerate` resolver commands and `setup.commands` keep running with the system shell. That is `/bin/sh` on macOS and Linux and `cmd.exe` on Windows (Python's `shell=True`). Forge does not translate commands. A team that mixes platforms writes commands that work in both shells, for example `uv run pytest -q`, `npm test`, `exit 1` or `&&` chains. Anything else goes in a script that the command runs, for example `python scripts/check.py`. Per-platform command keys would change the config schema; they are left for later if teams need them. Git hooks are not affected: Git for Windows runs them with its own `sh`.

**Executables.** Forge resolves the program through `PATH` (and `PATHEXT`) before it runs `gh`, the agent tools (`claude`, `codex`, `ollama`) or the editor, so npm's `.cmd` shims are found. `$EDITOR`/`$VISUAL` are split with Windows rules on Windows: quotes group words and backslashes stay literal.

**Long paths.** On Windows, `forge setup` turns on `core.longpaths` in the clone, next to rerere. `forge setup --check` warns when it is off. Nothing changes on macOS and Linux.

**Claude Code subagents.** Subagent files are rendered per machine and git ignores them (ADR 0014). On Windows the abstract `shell` tool renders as `Bash, PowerShell`, so a subagent has a shell with or without Git Bash. macOS and Linux keep `Bash`. Codex subagents have no tool list (ADR 0014) and do not change.

**Installer.** A new `install.ps1` does on Windows what `install.sh` does elsewhere, in Windows PowerShell 5.1 or PowerShell 7. It checks git ≥ 2.38, checks gh (and offers `winget install --id GitHub.cli` when winget is present), installs uv with its official PowerShell installer, picks the newest release tag (or `FORGE_VERSION`), runs `uv tool install` and prints the next steps. `install.sh` points Windows users (MSYS, MinGW, Cygwin) to `install.ps1` instead of WSL.

**CI.** The framework workflow keeps the Linux jobs on Python 3.11 and 3.12. It adds `windows-latest` and `macos-latest` jobs on Python 3.12 that run the same gates: ruff, mypy, `forge docs --check` and pytest. The Linux job also runs `mypy --platform win32` on `core`, so the Windows-only branches are type-checked on every push. Tests that need POSIX permission semantics (`chmod(0)`) skip on Windows with a reason. Tests that run real shell commands use `tests/support/shell.py`, whose commands behave the same in `sh` and `cmd.exe`.

## Consequences

- The `win32` refusal in claims, releases and handoffs is gone. The two tests that pinned it are replaced by tests for the Windows lock, for cross-process exclusion and for `pid_running` on a live child.
- `docs --check` no longer reports a file that differs only in CRLF. A missing final newline is still reported.
- Profiles that Windows users created under WSL stay in WSL. A native Windows install starts with a new profile in `%APPDATA%\forge` (`forge setup`).
- The spec changes: §5.3 (profile location), §17 (prerequisites and the PowerShell installer) and §19 (Windows leaves the v1 list). So do the README, `docs/protocol.md` (the `flock` note), `docs/architecture.md` (the new module) and the project templates `docs/forge/onboarding.md` and `faq.md`.
- Each product's own checks are the team's responsibility. Forge cannot make a `bash`-only check run in `cmd.exe`. The onboarding guide says so.
- Still open: a Windows dogfooding run on a real machine, like the v0.1.0 run on macOS. CI covers the test suite on Windows but not a full multi-user session against GitHub.
