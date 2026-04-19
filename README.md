# hermes-hookbus-publisher

Publishes hermes-agent lifecycle events to **HookBus**, the vendor-neutral runtime governance bus for AI agents.

## What it does

- Registers a `pre_tool_call` hook that posts a `PreToolUse` event to HookBus before every tool executes. If any subscriber returns `deny`, hermes blocks the tool call with the reason.
- Registers a `post_tool_call` hook that emits `PostToolUse` observationally.
- Registers an `llm_output` hook that emits `PostLLMCall` with token usage and model attribution.

## Install (60 seconds)

One shell command installs the full HookBus stack and this Hermes publisher plugin. For the Hermes-specific path, use the `--runtime hermes` flag below.

```bash
curl -fsSL https://agenticthinking.uk/install.sh | bash
```

Non-interactive variants:

```bash
# Hermes-agent users
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime hermes

# OpenClaw users
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime openclaw

# Bus + subscribers only, skip publisher
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime skip --noninteractive
```

The script prints the dashboard URL + bearer token on completion. Re-run any time, it is idempotent.

_Prefer not to pipe curl to bash? Inspect first:_ `curl -fsSL https://agenticthinking.uk/install.sh > install.sh && less install.sh && bash install.sh`

---

## Manual install

If you prefer to see every step, or you are building an immutable / reproducible deployment, here is the full manual install.

Hermes plugins live under `~/hermes-agent/plugins/` (the NousResearch/hermes-agent checkout directory). The easiest path is the one-shot installer script:

```bash
curl -fsSL https://raw.githubusercontent.com/agentic-thinking/hookbus-publisher-hermes/main/install.sh | bash
```

Or manually:

```bash
mkdir -p ~/hermes-agent/plugins/hookbus-publisher
cp __init__.py plugin.yaml ~/hermes-agent/plugins/hookbus-publisher/
```

Hermes auto-discovers plugins in that directory on next start.

## Config

| Env var | Default | Purpose |
|---|---|---|
| `HOOKBUS_URL` | `http://localhost:18800/event` | HookBus endpoint |
| `HOOKBUS_TOKEN` | *(empty)* | Bearer token. **Required** if the bus has auth enabled (default since v0.4). Read once: `docker exec hookbus cat /root/.hookbus/.token` and export to your shell |
| `HOOKBUS_TIMEOUT` | `10` | Seconds to wait for bus verdict |
| `HOOKBUS_FAIL_MODE` | `open` | `open` = allow on bus failure, `closed` = deny |
| `HOOKBUS_SOURCE` | `hermes-agent` | Source label in envelope |

Persist these across Hermes restarts by adding them to `~/hermes-agent/.env`:

```
HOOKBUS_URL=http://localhost:18800/event
HOOKBUS_TOKEN=<paste token here>
HOOKBUS_FAIL_MODE=closed
```

## Verify end-to-end

```bash
# Start a hermes chat turn
hermes chat -q \"Reply with exactly: PING\"

# Check it landed on the bus
curl -s -H \"Authorization: Bearer $HOOKBUS_TOKEN\" http://localhost:8883/api/recent?limit=1
# Expected: source=hermes-agent, model=<your-model>, cost recorded
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'dotenv'`** — the Hermes venv is missing python-dotenv. The NousResearch hermes-agent declares it in `requirements.txt` but `pip install -e .` doesn\t always pick that up. Fix:

```bash
~/hermes-agent/venv/bin/pip install python-dotenv
# Or, defensively, install all of requirements.txt:
~/hermes-agent/venv/bin/pip install -r ~/hermes-agent/requirements.txt
```

The `install.sh` installer already includes this step.

**`401 Unauthorized` on every event** — `HOOKBUS_TOKEN` is not set or does not match the bus\s current token. Check with:

```bash
docker exec hookbus cat /root/.hookbus/.token
# Compare against the value in ~/hermes-agent/.env
```

If the bus was restarted without `HOOKBUS_TOKEN` pinned in its docker-compose, it will have regenerated the token. Either pin it (recommended) or re-sync the `.env`.

## Envelope

Matches the shared HookBus schema used by every publisher (Claude Code, Amp, OpenClaw, OpenAI Agents SDK, Anthropic Agent SDK).

## Licence

MIT. See LICENSE.
