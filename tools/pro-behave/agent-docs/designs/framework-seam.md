# Framework seam

**Status:** accepted. Steps 1 (the seam) and 2 (results) are built; step 3 (the rename) is not. Settled points are in [decisions.md](../decisions.md) as DEC-22 to DEC-40. No questions are open. Where this file and the review copy differ, this file holds.
**Review copy:** <https://claude.ai/artifact/VXfoHQDNFo9AjK8Vpx6xmF>, written against `b1b7ba54`.

Describe the tool in terms that hold for any test framework, and make behave one adapter behind them. Every job reports one result for its unit, and a single run and a campaign share it. The tool becomes `pro-e2e`.

## Behave plugs in at three seams

The workflow and the terms are in [domain.md](../domain.md). The framework takes part at three seams, and each answers one question for the rest of the tool:

| Seam | Answers |
| --- | --- |
| catalog | Which features, scenarios and combos does this checkout have? |
| invocation | Which command, environment and instances run this unit? |
| interpretation | What did the job establish, and where in its log did it fail? |

### Each framework fills the domain in its own terms

| Term | behave | pytest |
| --- | --- | --- |
| feature | `.feature` file | test module |
| scenario | Scenario, Scenario Outline | test function |
| combo | Examples columns, or the first `Given` step | parameters |
| unit | `--name` with `-D releases` and `-D machine_types` | the scenario's node ids on that combo |
| instance | Examples row | parametrize case |
| job | a `tox -e behave` run | a pytest run |
| result | read from the JSON report | read from pytest's report |
| failure | the failing step | the failing assertion |
| run options | `UACLIENT_BEHAVE_*` variables | whatever the pytest harness reads |

### One port holds the three seams

`BehaveFramework` implements the `Framework` port in `jobs/ports.py`. The catalog's types and the queries over them (`catalog_entry`, `aggregate_dimensions`, `scenario_matches`, `match_unit`) are framework-neutral, in `jobs/domain/catalog.py`. The parser stays behave-specific and keeps its own types, so it imports only behave and the standard library; `behave/framework.py` maps them onto the catalog's. A pytest adapter would fill the same catalog types, with a parametrize set as an Examples block.

```python
class Framework(Protocol):
    # catalog
    def catalog(self, repo_root: Path) -> list[FeatureDetail]: ...

    # invocation: at queue time, so the command is stored in the request
    def invocation(self, repo_root: Path, unit: Unit, report: Path) -> Invocation: ...

    # at launch, because the run options hold secrets and are never stored
    def environment(
        self, inherited: Mapping[str, str], options: Mapping[str, str], install_from: str
    ) -> dict[str, str]: ...

    # interpretation; step 2 replaces summarize_report with the unit's result
    def summarize_report(self, report: list[Any]) -> ReportSummary: ...
    def digest_log(self, lines: Sequence[str]) -> LogDigest: ...
```

`Invocation` holds the command. Step 2 adds the unit's instances to it.

### The behave code moves into one adapter package

Everything behave-specific moves into `pro_e2e/behave/`. The queue, lanes, job process, kill, reconcile and the campaign's records already run whatever command a job holds, so they stay where they are.

| Today | Seam | Why it is behave's |
| --- | --- | --- |
| `jobs/parser.py`, parsing | catalog | Gherkin parsing, and combos read from Examples columns or the first step. Its query functions go to `jobs/domain/catalog.py`. |
| `build_command`, `exact_name_pattern` | invocation | `--name` regex with the row suffix, `-D` userdata, report flags. |
| `job_environment`, the `UACLIENT_BEHAVE_` prefix | invocation | How run options reach the harness. The option names stay in the jobs domain. |
| `jobs/domain/report.py` | interpretation | behave's JSON report schema and statuses. |
| `digest_log` and its patterns | interpretation | behave's plain formatter output. `select_log_lines` stays, since reading lines of a log holds for any framework. |

`match_unit` stays in the domain, in `jobs/domain/catalog.py`. A unit is keyed by its scenario's name, so a key that names two scenarios on one combo is a fact about the catalog under any framework. Pytest can produce one too (`TestA::test_x` and `TestB::test_x`), unless its adapter names scenarios by class and function.

The campaign stops importing the parser. Its domain works on campaign-owned `CatalogScenario` values, and only its `CatalogFeatureReader` adapter reads the catalog, from the same framework object the job service validates units against.

### An import contract keeps behave out of the domain

The job process and the two front-ends build `BehaveFramework` and pass it in, as they build the other adapters today. The job process moves out of `jobs/` to `pro_behave/job_process.py`, a composition root beside the front-ends, and the launcher is given the module to run. The layers contract reads `cli`, then `mcp | job_process`, then `behave | campaign`, then `jobs`, so neither `jobs` nor `campaign` can import the adapter, and a pytest adapter stays a sibling package. Two forbidden contracts keep the job process out of `campaign` and the front-ends, and keep the parser free of the tool's own modules.

## Every reader gets the same result for a job

A job's result is decided once, by one set of rules, and every reader gets it: a single run, a list of jobs, and a campaign. Today they can disagree. A job records only whether its command exited 0, and a campaign decides an outcome on its own by counting the report's scenarios. A unit whose rows all skip exits 0, so `wait_for_job` reports `ok: true` while a campaign records `skipped`.

### A result counts only the unit's own instances

Behave selects what to run by filtering: it reports every row of the scenario, and the harness marks the rows for other combos `skipped`. All 17 reports checked from the LP-2163406 campaign hold such rows. A row for another combo is not an instance of the unit, so the result neither counts nor reports it. A `skipped` outcome then means the unit's own instances skipped, for example on a missing contract token.

The invocation names the unit's instances, and the job's request records them when the job is queued. An instance has an `id`, unique in the checkout, and the `name` the framework gives it. For behave the id is the row's location, `<feature>:<line>`, which is also the location behave reports for the row; the name is the row's runtime name, `<scenario> -- @<block>.<row> <examples name>`. Names alone are not unique: in the corpus, 105 units have a row whose name is shared by a row on another combo, because same-named outlines each number their rows from `@1.1`. Locations had no duplicates. For pytest the id would be the node id. Interpretation matches the report against that list by id, and checks each name, so a feature edited between queueing and running is reported rather than misread.

Most units have one instance. 11 of the 271 scenario outlines have a combo with more than one Examples row. "Disable and purge fips" in `features/cli/disable.feature` is one: its rows vary the FIPS service on the same combos.

### The first matching rule decides the outcome

The adapter observes every run the report shows, with a neutral status: `passed`, `failed` (a step failed), `skipped`, or `error` (the test machinery broke: a hook or cleanup error, an undefined step, or a status it does not recognise). Skip reasons and hook errors come from the job's log, since behave's JSON report carries neither. The rules below live in the jobs domain, so every framework shares them.

| The job | Outcome |
| --- | --- |
| was killed, or its process died | none: the job is interrupted |
| could not start, or left no readable report | `error`, with the reason |
| shows none of the unit's instances | `error` |
| shows one of the unit's instances missing, twice, or renamed | `error`: the feature changed since the job was queued |
| has an instance with a failing step | `failed`, with the failures |
| has an instance whose test machinery broke | `error`, with the reason |
| exited non-zero | `error`, naming the row that broke, which may be another combo's: its hooks still ran |
| skipped every instance | `skipped`, with the skip reasons |
| passed at least one instance and skipped the rest | `passed`, with the skipped instances and their reasons |
| anything else | `error` |

A unit whose instances all passed is `error` when behave's teardown fails afterwards. Its passed instances stay in the result, so a watcher can see the harness broke rather than the product.

### Results replace `ok` and the report counts

- `exit.json` becomes `result.json`, which carries `outcome`, `reason`, `instances` and `failures`. `ok` goes, and `returncode` stays as evidence. `error` folds into `reason`.
- Job responses carry `outcome` in place of `ok` and the `summary` count tables.
- A campaign records the job's outcome as it stands. `classify.py` stops reading report counts.
- The jobs and campaign copies of the outcome agree on four values. `ScenarioStatus` and its `unknown` value go.
- Each failure names the instance it belongs to.

## The job process writes one result.json when the job ends

When the command exits, the job process interprets the report and writes the exit and the result together into `result.json`, in one write by rename. It replaces today's `exit.json`. This keeps the crash-first model (DEC-04): the file appears whole or not at all. A job that ran to the end has its outcome in it, a killed job's result says `killed` with no outcome, and a job process that dies leaves no file, so the job reads as interrupted. A result never changes once written, so a campaign record and the job always agree.

Interpretation runs before the exit is written, so a guard turns a failure there into an `error` result whose reason carries the exception. The command's return code is written either way. Two costs are accepted: a fix to the interpretation rules does not reach jobs that already ended, and `result.json` holds the framework's reading of the report next to the process facts.

### The job's files record each step of its life

| State | What is on disk | Written by |
| --- | --- | --- |
| queued | `request.json`: the unit, its instances, the command and the install source. | the caller queuing the job |
| running | `job.lock`, held for the job process's life. `job.json`: when it started, the checkout's commit, and the run options with secrets hidden. `stdout.log` and behave's `report.json` fill in. | reconcile, then the job process and behave |
| finished | `result.json`: the return code, `outcome`, `reason` and `failures`. | the job process when the command exits, or reconcile when the job could not start |
| interrupted, killed | `result.json`: `killed`, with no outcome. | the job process for a running job, `kill_job` for a queued one |
| interrupted, died | no `result.json`, and `job.lock` is free. | nothing: the absence is the record |

### A separate file or a result decided on read would break that

| Alternative | Why not |
| --- | --- |
| Two files, the exit and then the result | Two writes leave a window, and a crash can leave it open: an ended job with no result. Readers would need a rule for that state, and reconcile would have to fill it in. |
| Decided on read, with nothing stored | A result could change after an upgrade while the outcome a campaign recorded does not, which is the disagreement this design removes. |

## The surface reports outcomes and matches scenarios exactly

- Results carry `outcome`, as above.
- Scenario filters match exact names. Substring search by name goes from `find_scenarios` and `job_results`.
- `describe_feature` keeps behave's fields (`type`, `example_columns`, `examples`), because it shows the parser's catalog.

## The tool becomes pro-e2e

The CLI becomes `pro-e2e`, the package `pro_e2e`, the state directory `.pro-e2e`, the environment variables `PRO_E2E_*`, the skills `pro-e2e-*` and the MCP client registration name `pro-e2e`. The adapter package keeps its name: `pro_e2e/behave/`.

## The work lands in three steps

1. **Seam.** Move the behave code into `pro_behave/behave/` behind the `Framework` port, and add the import contract. Behaviour does not change.
2. **Results.** The request records the unit's instances, and the job process writes the result with the exit, by the rules above. Job responses and the campaign read it.
3. **Rename.** Last, so the first two diffs stay readable.

## Pytest, scheduling and MCP tuning are out of scope

- A pytest adapter, and a pytest catalog parser.
- Scheduling and parallelism.
- MCP tuning: defaults, limits and schema size.
