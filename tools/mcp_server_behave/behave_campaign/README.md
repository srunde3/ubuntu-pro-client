# behave_campaign

Durable record of behave test units and attempts.

A campaign outlives the MCP's job history. The MCP's `list_scenario_jobs`
window is bounded; an SRU verification runs for days, so what was attempted
and what it established is recorded here instead.

A **test unit** is one behave scenario for one release on one `machine_type`.

## Storage

One append-only JSON Lines file per campaign. `create` writes a `campaign`
header first, then one `plan` record per unit; `record` appends `attempt`
records. The whole campaign replays from the file.

The `campaign` header captures how the campaign was built, so it can be
recreated and audited later: the campaign id, the filters used, the checkout it
was built from (root, commit, branch, dirty), and when. Every record carries a
UTC timestamp, and every attempt records the install source the job ran with --
the evidence an SRU verification actually rests on, which would otherwise only
live in MCP job history that expires.

Current state per unit:

- A unit with no attempts is `unattempted`.
- Any passing attempt makes and keeps the unit `passed`.
- Otherwise the latest attempt state wins: `running`, `failed`, `skipped`,
  or `error`.

## Scope

`create` builds units from the feature files themselves, parsed with
`behave_mcp.parser` -- the same parser the MCP uses to select scenarios at run
time, so a campaign covers exactly the combinations each scenario supports
rather than a Cartesian product.

`create` defines scope once, from repeatable `--release`, `--machine-type`,
`--feature`, and `--scenario` filters; omitting them all yields the full
campaign. `--campaign-id` is an opaque label -- for SRU work it is the
Launchpad bug number, but the record attaches no meaning to it.

## Usage

```bash
uv sync --extra test

# See which releases and machine types the feature files can run.
uv run behave-campaign dimensions --repo-root ../..

# Create the campaign: "all the jammy lxd-vm tests".
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567 --release jammy --machine-type lxd-vm

# Repeat a filter to cover several values, as a real SRU usually does.
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567 \
  --release bionic --release focal --release jammy \
  --release noble --release resolute --release stonking

# With no filters, the campaign covers every feature file.
uv run behave-campaign create --campaign T.jsonl --repo-root ../.. \
  --campaign-id 1234567

# Record started and finished attempts in batches.
uv run behave-campaign record --campaign T.jsonl --install-from proposed \
  --input attempts.json

# Or hand the MCP's own start/wait payloads straight to the tool.
uv run behave-campaign record --campaign T.jsonl --install-from proposed \
  --from-mcp --input results.json

# Ask what to run next, then inspect results.
uv run behave-campaign next --campaign T.jsonl --limit 4
uv run behave-campaign status --campaign T.jsonl --state failed
uv run behave-campaign history --campaign T.jsonl \
  --feature features/cli/attach.feature
```

All commands read `--input` from stdin by default and print JSON to stdout.
`status`, `next`, and `history` accept the same filters plus `--state`,
repeatable to allow several values. `status` also reports the campaign header.

Splitting work across people happens out of band: each person creates their
own campaign file with the slice they agreed to run.

## Guardrails

The tool fails loudly instead of guessing:

- A campaign file can only be initialised once; a campaign is not edited
  afterwards.
- An unknown release, machine type, or feature in `create` is rejected rather
  than silently matching nothing.
- Recording an attempt for a unit that was never planned is rejected.
- Attempt state must be one of the known states.
- An MCP result that is not a clean pass, failure, or skip is rejected.

## MCP results

`record --from-mcp` takes `[{"unit": {...}, "result": <MCP payload>}]`, where
`result` is a `start_scenario` or `wait_for_scenario_completion` response. Each
job covers exactly one unit, so the mapping is:

| MCP payload | State |
| --- | --- |
| `started`, `timeout` | `running` |
| `completed`, every scenario passed | `passed` |
| `completed`, any scenario failed | `failed` |
| `completed`, only skips | `skipped` |
| `completed`, no parseable report (`summary: null`) | `error` |

Anything else -- mixed passes and skips, an `unknown` scenario status, or a
pass contradicted by `ok: false` -- is rejected so it gets looked at rather
than silently recorded. Pass an explicit attempt state if that ever happens.

## Architecture

Same hexagonal layering as [behave_mcp](../behave_mcp), one module per layer:

- `domain.py` -- pure campaign rules. Units, attempts, state reduction, and
  the order remaining work is taken up in. No I/O.
- `discovery.py` -- builds units from the feature files via
  `behave_mcp.parser`.
- `repo.py` -- reads the checkout state a campaign was built from.
- `cli.py` -- the standalone front-end.

## Build, test, lint

Run from `tools/mcp_server_behave`; this package shares that project's
`pyproject.toml`, virtualenv, and CI job.

```bash
uv sync --extra test           # or --extra lint
uv run pytest -q tests/test_campaign_domain.py \
  tests/test_campaign_discovery.py tests/test_campaign_cli.py
uv run black --check behave_campaign
uv run isort --check-only behave_campaign
uv run flake8 behave_campaign
uv run mypy behave_campaign
```

## TODOs

- Consider stronger DB than JSONL with various record types
- Find common data models with the MCP; avoid duplicate serde if we can.
  `repo.py` and `behave_mcp.adapters.LocalWorkspace.repo_state` read the same
  git state through different types.
