# Decisions

Each decision has an ID that stays fixed. Where a review doc gave it an ID first, the Source column keeps that ID (R1 to R12 from the architecture review, Q1 to Q5 from the framework seam design).

## One checkout per server, and no process owns the work

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-01 | A server serves one checkout. A per-call `repo_root` chooses where behave runs and which feature files are read, never where files are written. | One server keeps one set of jobs and lanes. `repo_root` stays on every tool so a worktree with different feature files can be tested. | R1 |
| DEC-02 | MCP is served over streamable HTTP only. | One transport. Under stdio, a job's child process could read MCP protocol bytes from the stdin it inherited. | R9 |
| DEC-03 | Nothing long-lived drives jobs or campaigns. Reconcile works from files, and every front-end command, the server's timer and each job's own process run it. Several servers and CLIs may share a state directory. | Work finishes with nothing driving it, and there is no in-memory state to recover after a crash. Recovery means reading the files and locks of live jobs. | R5 |
| DEC-04 | Crash-first: each fact is written once, whole, by rename. A process that dies leaves no fact, and the absence reads as interrupted. | The state is correct after any crash, with no recovery code. The user will not trade this model away. | Q3 |

## A job is one unit, kept in files

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-05 | A job runs exactly one unit, selected exactly. A selection that matches no unit, or more than one, is refused, for one-off jobs and campaign units alike. | A result belongs to one unit, with nothing else mixed in. | R3 |
| DEC-06 | Lanes belong to the server, and one-off jobs and campaigns share them. A lane is a lock file the job process holds. Max jobs per pool is checkout config. | Capacity is shared across processes through the kernel, with nothing held in memory. | R4 |
| DEC-07 | A killed job is interrupted (`killed`), and a job whose process died is interrupted (`died`). Neither is an error. | Neither established anything about the unit, so a campaign queues the unit again. | review vocabulary |
| DEC-08 | Job settings have one interface: `config.json` in the state directory, set with `pro-e2e config`, secrets included and never shown. A job drops every inherited `UACLIENT_BEHAVE_*` variable. | A job must not depend on who starts it. | p4 design |
| DEC-09 | The job layer knows nothing of campaigns. A job's own process reconciles jobs only. | The job service stays usable on its own, and campaigns can be lifted out. | |

## A campaign writes its intent and derives the rest

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-10 | Any pass counts: a unit that has ever passed stays passed. | A flaky failure after a pass says nothing new about the unit. | R6 |
| DEC-11 | The commands are `run`, `pause` (optionally killing what is in flight) and `retry`, which always sets the intent to run. There is no cancel and no terminal state. | Intent is the only state written, so three commands cover every change, and a complete campaign can still be retried. | review state model |
| DEC-12 | A unit with a problem outcome is run again only when someone asks. | Judging a failure flaky or real is the watcher's call, never the scheduler's. | |
| DEC-13 | Each campaign caps its own jobs per pool, 1 by default for each pool in its scope. | The job queue is first in, first out per pool, so a campaign's cap keeps it from crowding out others. | p5 design |
| DEC-14 | A campaign stores facts only. Its events are derived from its records on every read, and a starved pool or an overdue job is recorded once per episode. | One log, nothing to keep in sync, and a watcher hears of each problem once, across restarts. | |

## One surface, shaped for the CLI first

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-15 | The MCP tool, the CLI command and the service method have one name (`run_job`, `job run`, `JobService.run_job`). | One concept, one name, whichever front-end is used. | p6 design |
| DEC-16 | Shape the service and CLI first: what a response carries, and how many calls a task takes. Tune MCP defaults, limits and schema size separately. | Token cost justifies only the tuning. | |
| DEC-17 | Parameters use the glossary's terms (`feature`, `scenario`, `release`, `machine_type`) and the existing taxonomy (a combo is `{release, machine_type}`). Combos stay dimension-agnostic, and outputs invent no notation. | The same thing reads the same way everywhere. | |
| DEC-18 | Every list that grows takes a `limit` and reports `total` and `truncated`. Unknown filter values are refused. | Bounded responses, and a typo is an error rather than an empty result. | |

## The parts stay independent, and breaking changes are welcome

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-19 | Duplication across `jobs`, `campaign` and the parser is intentional. Copies must agree on names, fields and values. | Each part can move upstream on its own. | |
| DEC-20 | No compatibility shims, optional fields or legacy readers. | The tool has no consumers. | |
| DEC-21 | The feature-file parser is its own track and may become a library. Other code reaches it only through ports. | It may leave the package. | |

## Behave is one adapter behind a framework-neutral tool

The framework seam design, settled in review.

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-22 | The tool, its package, state directory, environment variables, skills and MCP client registration are named `pro-e2e`. | The name should say what the tool runs, and stay true after a framework change. | Q1, Q4 |
| DEC-23 | Behave sits behind one adapter with three seams: catalog, invocation and interpretation. The parser stays behave-specific. | A move to pytest becomes a sibling adapter. A parser is naturally specific to its framework. | |
| DEC-24 | A job decides its unit's result when it ends, and every reader gets that one result. The result counts only the unit's own instances, which the request records when the job is queued. | A single run and a campaign must report the same outcome. Rows for other combos are not part of the unit. | |
| DEC-25 | The job process writes the result with the exit, in one file and one write. A failed interpretation becomes an `error` result. | Keeps DEC-04: a finished job always has its result. | Q3 |
| DEC-26 | Scenario filters match exact names. Substring search by name goes. | Units are selected exactly (DEC-05). | |
| DEC-27 | A unit passes when at least one of its own instances passes and none fails. Its skipped instances are listed in the result with their skip reasons. A unit whose own instances all skip is `skipped`. | What matters is a pass on the unit that was asked for. | Q2 |
| DEC-28 | The file that marks a job's end is `result.json`. A killed job's result says `killed`, with no outcome. | Readers look for a job's result there, and a kill is a fair thing for a result to say. | Q5 |

## Built: the framework seam

Step 1 of the framework seam design. Each decision was reviewed by an independent agent before it was built.

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-29 | The catalog's types and its queries are framework-neutral, in `jobs/domain/catalog.py`. The parser keeps its own types, and the behave adapter maps them onto the catalog's, guarded by a test that maps a fully populated feature. | The job service and campaigns query the catalog without importing behave, and a pytest parser fills the same types. The parser stays liftable as a library (DEC-21). | |
| DEC-30 | `match_unit` stays in the domain: a unit's key must name exactly one catalog scenario. | Units are keyed by scenario name under any framework, so the check guards every adapter. | |
| DEC-31 | The port builds the command at queue time (`invocation`) and the environment at launch (`environment`), in separate methods. `Invocation` holds the command, and step 2 adds the unit's instances to it. | The command is stored in the request. The environment carries secrets, so it is never stored. | |
| DEC-32 | The job process is a composition root at `pro_e2e/job_process.py`. The launcher is given the module to run, and a contract keeps the job process out of `campaign` and the front-ends. | It must build the behave adapter, which `jobs` may not import, and DEC-09 must stay enforced. | |
| DEC-33 | The campaign domain works on campaign-owned `CatalogScenario` values. Only campaign adapters read the catalog, from the framework the job service uses. | A campaign plans against the same catalog its jobs are validated against, and its domain imports nothing from the catalog. | |

## Built: a job decides its result

Step 2. Each decision was reviewed by an independent agent before it was built.

| ID | Decision | Why | Source |
| --- | --- | --- | --- |
| DEC-34 | An instance is identified by its location (`<feature>:<line>`) and recorded with its expected name in the request, at queue time. | Row names repeat across same-named outlines (105 units in the corpus); locations do not. The name check catches a feature edited before the job ran. | |
| DEC-35 | The adapter observes, with a neutral status per run; the domain decides, with one set of rules for every framework. `failed` means a step failed; `error` means the test machinery broke. | The rules must not be copied into each adapter, and a broken hook says nothing about the product. | |
| DEC-36 | The rules end in `error`, never in `passed`. A missing, duplicated or renamed instance is `error`, and so is a non-zero exit, named after the row that broke even when it is another combo's. | A result must never claim a pass it cannot show. Hooks run for skipped rows too, so their failures explain the exit. | |
| DEC-37 | The job process takes a copy of `killed` when its command exits, ignores any later SIGTERM, and builds the result inside a guard whose fallback uses plain values only. Domain constructors build every result: finished, killed, could not start, unreadable. A validator keeps `killed` exactly when there is no outcome. | Keeps DEC-04: a finished job always has its result, and every writer produces one shape. | DEC-25 |
| DEC-38 | Job responses carry `outcome`, `reason`, `instances` and `failures`; `ok`, the behave counts, `ScenarioStatus` and `text` search go. An interruption's cause is `interruption`, so `reason` means one thing. The response item is `JobSummary`; `JobResult` is the file. | Every reader gets the same result, and one field name has one type. | DEC-26 |
| DEC-39 | A campaign's `JobRunner.poll` returns the campaign's own typed `JobStatus`, which its adapter builds from the job service's response by field name. Finished records carry the unit's instances, and each failure names its instance. | One parse, at the adapter; the campaign's copies agree with the jobs copies in name and fields. | |
| DEC-40 | `unit.unclassifiable` goes. `FinishedRecord.reason` means what `JobResult.reason` means, and every `error` is announced as `unit.errored` with its reason. A finished status the campaign cannot read is `error` with a reason that names it a fault in the tool. | One meaning per field; a watcher acts on both the same way, by looking at the job. | |
