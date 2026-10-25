# Onboarding on a reviewer's machine

Written for macOS, which most reviewers use. Notes for Linux and Windows are at the end. The goal: tests pass, the console opens, and, if the reviewer has a key, the real LLM planner works.

**Ask before installing anything.** Say what you will install and why, and wait for a yes. Installing is the only step that changes their machine outside this folder. If you run commands in a sandbox, installing and starting the server may need network access; tell the reviewer that they may be asked to approve it.

## 1. Check what is already there

Run these and summarize the results in one short list:

```bash
sw_vers -productVersion        # macOS version
git --version                  # no git -> macOS offers the Command Line Tools; or run: xcode-select --install
python3 --version              # macOS ships 3.9, which is too old: 3.10 or newer is required
uv --version                   # optional, but the easiest route
brew --version                 # Homebrew, optional
```

Nothing else is needed. Node.js is only for changing the UI, because the built console is committed, and any browser works.

## 2. Install the dependencies

Pick the first route that fits, and confirm it with the reviewer.

**Route A, uv (recommended).** uv installs a suitable Python by itself, so this works even when only macOS's Python 3.9 is present.
1. Install uv: `brew install uv` if Homebrew is present. Otherwise use the official installer, `curl -LsSf https://astral.sh/uv/install.sh | sh`. It installs into `~/.local/bin`; open a new shell, or call `~/.local/bin/uv` directly, if `uv` is not found.
2. Install the project: `cd backend && uv sync`. This downloads Python 3.10 or newer if needed, plus every dependency from the lockfile.
3. Run the tests: `uv run pytest -q`.
4. Start the server: `uv run python -m app`.

**Route B, pip** (when Python 3.10 or newer is already installed, or after `brew install python@3.12`):
1. Create a virtual environment: `cd backend && python3.12 -m venv .venv`. Use whichever 3.10+ interpreter they have.
2. Install the dependencies: `.venv/bin/pip install -r requirements-dev.txt`.
3. Run the tests: `.venv/bin/python -m pytest -q`.
4. Start the server: `.venv/bin/python -m app`.

Run the server in the background and wait until `http://127.0.0.1:8000/api/config` answers. Then tell them to open **http://127.0.0.1:8000**. Report the real test count and result.

## 3. Connect an LLM (optional)

Ask: *"Do you have an OpenAI (GPT) API key or an Anthropic (Claude) API key you would like to use? Without one, the project runs with its deterministic fixture planner, which is labelled as not being an LLM."*

**Never ask them to paste the key into the chat**, and never print `backend/.env`. You write every setting except the key; they paste the key into the file themselves.

1. If `backend/.env` does not exist, copy it from the template: `cp backend/.env.example backend/.env`. If it exists, edit only the `LLM_` lines, and never overwrite the file.
2. Set the values for their provider:

   | Setting | OpenAI | Anthropic (Claude) |
   | --- | --- | --- |
   | `LLM_BASE_URL` | `https://api.openai.com/v1` | `https://api.anthropic.com/v1` |
   | `LLM_MODEL` | `gpt-6-luna` (the model this project was verified with) | `claude-sonnet-5-5` (or `claude-opus-5-5`) |
   | `LLM_REASONING_EFFORT` | `medium` | leave empty: the Anthropic endpoint ignores it |
   | `LLM_API_KEY` | leave empty for them | leave empty for them |

   If their OpenAI account cannot use `gpt-6-luna`, use another Chat Completions model their account offers. The planner validates every plan, so the model only has to return JSON reliably.
3. Ask them to open the file, for example with `open -e backend/.env` (TextEdit), paste the key after `LLM_API_KEY=`, and save. Wait for them to say done.
4. Check that a key is present **without printing it**: `grep -c '^LLM_API_KEY=..' backend/.env` should print `1`.
5. Restart the server (Ctrl+C, or stop the background process, then start it again). `/api/config` should now list the `llm` planner.
6. Ask before spending tokens: one plan costs about 1,500–2,000 tokens. Then create one mission with the LLM planner:

   ```bash
   python3 .claude/skills/iac-tour/scripts/iac.py create "Inspect and resolve the suspected leak on tank T-101" --planner llm
   ```

   - **If it reaches `ready`:** the key works. Show the planner call in the timeline, with the model, latency, and tokens.
   - **If it reaches `planning_failed`:** read the error from `iac events MID planner_call`.

   | Error | Meaning |
   | --- | --- |
   | HTTP 401 | The key is wrong or was pasted incompletely. |
   | HTTP 404, or "model not found" | The model ID is not available to this account. |
   | HTTP 429 | Rate limit or no credit. |
   | "could not reach the model" | A network or proxy problem. If they use a SOCKS proxy, `httpx[socks]` is already installed. |
   | Anthropic error naming a workspace | The key is not scoped to a workspace. Create a workspace-scoped key in the Claude Console, because this planner does not send the `anthropic-workspace-id` header. |

About Claude: the planner talks to Anthropic through its OpenAI-compatible endpoint. That endpoint ignores `response_format`, so Claude is not forced into JSON mode. The planner accepts JSON with surrounding text, and it corrects invalid output up to two times. This path follows Anthropic's documentation but has not been run against a live Claude key in this repository, so tell the reviewer that if they choose it. OpenAI `gpt-6-luna` runs are recorded in `docs/VERIFICATION.md`.

If they pasted a key into the chat anyway, do not repeat it. Put it in the file only if they ask you to. Suggest they rotate it later, because it is now in the conversation history.

## 4. Starting over and cleaning up

- **Start clean:** stop the server and delete `backend/data/orchestrator.db`. The simulated tank resets with it.
- **Remove the project:** delete the folder. uv's cache can be cleared with `uv cache clean`.
- **Remove the key:** delete `backend/.env`.

## Linux and Windows

- **Linux:** the same steps. Use the distribution's `python3.10+` package or uv's installer.
- **Windows:** use PowerShell.
  - Install uv with `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, or install Python 3.10+ from python.org.
  - The virtual environment's interpreter is `backend\.venv\Scripts\python.exe`.
  - Copy the env file with `copy backend\.env.example backend\.env`.
