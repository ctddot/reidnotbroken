# ReidCLI Handoff

## Repo

Path:

```powershell
C:\Users\gonza\Documents\Codex\2026-07-02\ryyreid-reidcli-https-github-com-ryyreid\work\ReidCLI
```

Project: Python CLI app using Typer, Rich, prompt-toolkit, provider adapters, tools, sessions/tasks, and policy controls.

User asked to keep replies in caveman style earlier. Continue concise if helpful.

## Current User Goal

ReidCLI should feel like a clean terminal AI CLI:

- native terminal scrollback, no broken fullscreen scroll
- red outlined input box
- command menu readable with no gray/white background
- ReidVerse provider by default
- thinking/interrupt behavior
- many slash commands fully working
- npm/npx install path
- separate experimental command for UI testing

## Current Run Commands

From repo root:

```powershell
uv run reidcli
uv run reidcli tui-test
```

`tui-test` is now intentionally native-scroll too. It is separate as a command/file, but no longer uses the fullscreen alternate-screen TUI because the user wants normal terminal scrolling.

Published npm state:

- npm latest was `reidcli@0.1.2` last checked.
- Local package version is `0.1.3`.
- Publishing `0.1.3` is ready but npm requires 2FA/OTP.
- User must run:

```powershell
npm publish --access public --otp YOUR_6_DIGIT_CODE
```

Install/run after publish:

```powershell
npx reidcli
```

## Provider Config

Active intended provider: `reidverse`.

Global files:

```powershell
C:\Users\gonza\.reidcli\config.json
C:\Users\gonza\.reidcli\providers.json
```

Provider:

- name: `reidverse`
- kind: `openai`
- base URL: `https://reidverse-ai.up.railway.app`
- model: `gpt-5-4`
- key prefix: `rc-`

Do not print or commit the key.

Important bug fixed: PowerShell wrote `providers.json` with UTF-8 BOM. `ProviderStore` now reads `utf-8-sig`, so ReidVerse loads instead of silently falling back to stub.

Sanity check:

```powershell
$env:PYTHONPATH='src'; uv run python -c "from reidcli.app.commands import build_orchestrator; o=build_orchestrator(); print(o.config.default_provider); print(o.providers.names()); print(o.provider.name); print(getattr(o.provider,'default_model',''))"
```

Expected:

```text
reidverse
['stub', 'reidverse']
openai
gpt-5-4
```

## Main Files Changed

UI:

- `src/reidcli/ui/app.py`
- `src/reidcli/ui/render.py`
- `src/reidcli/ui/tui_test.py`
- `src/reidcli/ui/commands.py`
- `src/reidcli/ui/assets/reid_logo_outline.png`

Provider/config/runtime:

- `src/reidcli/provider/openai.py`
- `src/reidcli/provider/ollama.py`
- `src/reidcli/provider/store.py`
- `src/reidcli/provider/gemini.py`
- `src/reidcli/provider/registry.py`
- `src/reidcli/runtime/agent.py`
- `src/reidcli/runtime/orchestrator.py`
- `src/reidcli/config/loader.py`

Packaging:

- `package.json`
- `bin/reidcli.js`
- `.npmignore`
- `scripts/install.sh`
- `scripts/install.ps1`
- `pyproject.toml`
- `src/reidcli/__init__.py`

Tests:

- `tests/test_ui_commands.py`
- `tests/test_providers_subagents.py`
- plus older tests touched.

## Implemented UI State

Default `run()` uses `_terminal_run`, not fullscreen.

Input:

- real 3-line prompt-toolkit mini app
- red outline wraps input live
- native terminal scrollback
- slash menu still works
- no giant bottom toolbar

Thought summary:

- It no longer prints under the old submitted box.
- Last turn's thought summary is passed into the next input render and appears under the newest active input box.
- This was implemented with `last_summary` and `_terminal_read_input(..., status_line)`.

Mascot:

- `render.banner()` has ASCII art on the left again when terminal is wide enough.
- Panel shrinks to make room.

Bad key behavior:

- OpenAI/Ollama provider errors now log at debug instead of dumping stack traces.
- Terminal catches turn exceptions, prints friendly error, and returns to prompt.
- Bad OpenAI key message points to `/config set openai_api_key <key>`, `OPENAI_API_KEY`, or `/use stub`.

## TUI Test

Command:

```powershell
uv run reidcli tui-test
```

File:

```text
src/reidcli/ui/tui_test.py
```

Current behavior:

- separate command and file
- uses same native terminal path as main CLI
- this is deliberate because user asked for normal native scroll
- fullscreen `_fullscreen_run` remains in `ui/app.py` as fallback/old code, but is not the active path

Earlier fullscreen scroll controls were improved before switching away:

- mouse wheel
- PageUp/PageDown
- Home/End when input empty
- pinned bottom behavior

But user disliked "scrolled" mode, so `tui-test` now avoids fullscreen.

## Slash Commands Added

Many commands were added in `src/reidcli/ui/commands.py`, including:

- config/env/status/usage
- providers/connect/disconnect/use/models/model
- sessions search, session rename/delete
- export, compact, undo, retry, edit, fork
- plan, tools enable/disable, approvals, cost, web
- prompt save/run, workflows save/run/show/delete
- mcp list/connect/disconnect
- agents/agent/deepreid
- theme, keys, update

Argument completions were added in `SlashCommandCompleter`.

## Interrupt Behavior

Terminal generation:

- `Ctrl+B` toggles thought details for the turn.
- `Esc` once shows confirmation.
- `Esc` again within 2 seconds sets cancel event.

Cancel is passed to `orchestrator.submit_task(..., cancel=cancel_event.is_set)`.

## Verification

Latest checks passed:

```powershell
python -m py_compile src\reidcli\ui\app.py src\reidcli\ui\render.py src\reidcli\ui\tui_test.py src\reidcli\app\commands.py
$env:PYTHONPATH='src'; uv run ruff check src tests
$env:PYTHONPATH='src'; uv run pytest
npm pack --dry-run
$env:PYTHONPATH='src'; uv run reidcli --help
```

Latest pytest:

```text
52 passed
```

`reidcli --help` shows `tui-test`.

`npm pack --dry-run` includes `src/reidcli/ui/tui_test.py`.

## Dirty Worktree

Worktree is very dirty and includes user/agent changes. Do not revert.

Status includes modified:

- `.gitignore`
- `README.md`
- `pyproject.toml`
- `settings.json`
- `src/reidcli/...`
- `tests/...`

Untracked includes:

- `.npmignore`
- `bin/`
- `package.json`
- `scripts/`
- `settings.example.json`
- `src/reidcli/provider/gemini.py`
- `src/reidcli/ui/assets/`
- `src/reidcli/ui/tui_test.py`
- `tests/test_ui_commands.py`
- `uv.lock`

## Next Likely Requests

If user wants publish:

1. Confirm latest npm version.
2. If latest is still below local version, ask/require OTP.
3. Run `npm publish --access public --otp CODE`.

If user wants more UI work:

- Keep native terminal scroll.
- Avoid fullscreen alternate-screen unless user explicitly accepts custom scroll behavior.
- Keep `tui-test` separate while experimenting.
- Do not bring back prompt-toolkit bottom toolbar.

If user says "run it":

```powershell
Start-Process powershell -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-Command','Set-Location -LiteralPath ''C:\Users\gonza\Documents\Codex\2026-07-02\ryyreid-reidcli-https-github-com-ryyreid\work\ReidCLI''; uv run reidcli')
```

For TUI test:

```powershell
Start-Process powershell -ArgumentList @('-NoExit','-ExecutionPolicy','Bypass','-Command','Set-Location -LiteralPath ''C:\Users\gonza\Documents\Codex\2026-07-02\ryyreid-reidcli-https-github-com-ryyreid\work\ReidCLI''; uv run reidcli tui-test')
```
