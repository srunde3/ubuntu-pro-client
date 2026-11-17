# Working design for the end-to-end test suite

This document helps us agree on how our end-to-end tests fit together before
we build more tools around them. It is a working design document, not
maintainer documentation, and we do not intend to keep it after this work is
complete.

The durable guide is
[`e2e_test_suite.md`](../../explanation/e2e_test_suite.md). That guide explains
the finished model. This document records the evidence and decisions that lead
us there.

[`system-overview.md`](system-overview.md) shows how the parser, suite model,
validation, coverage analysis, and test execution fit together.

[`review-guide.md`](review-guide.md) explains what reviewers should agree on
before this design guides the rest of the epic.

## Why we need a shared design

Several tools (some currently implemented, some still-to-be-developed) need to
read the same feature files:

* a test runner needs to find and run one exact test;
* a coverage tool needs to know which releases and machine types are tested;
* validation needs to reject feature files that tools cannot understand;
* maintainers need errors that point back to the relevant source.

These tools currently make some of their own assumptions. One tool may treat a
release and machine type as a complete test, while another tool needs the other
values from the same `Examples` row. One tool may ignore a file that fails to
parse, while another reports an error.

We need one description of the suite that all of these tools can use. The
description must preserve the details needed to run tests without making
coverage reports unnecessarily repetitive.

## What the current suite looks like

The current suite contains:

* 73 feature files;
* 277 authored `Scenario` or `Scenario Outline` blocks;
* 271 `Examples` blocks;
* 1,250 `Examples` rows;
* 19 groups where the same scenario name appears more than once in a file;
* 39 groups of rows where several rows use the same release and machine type;
* 6 plain scenarios that write the release and machine type directly in a
  step instead of using an `Examples` table.

These are not just unusual edge cases. The design must describe all of them
without losing tests or guessing what an author meant.

## What counts as one scenario?

A maintainer normally thinks of a scenario as a named behavior, such as
`Fix command on an unattached machine`.

We will use these rules:

* a scenario is identified by its feature-file path and exact name;
* each authored `Scenario` or `Scenario Outline` is one scenario;
* scenario names must be unique within a feature file;
* similar scenarios must use names that explain their different setup,
  inputs, or expected behavior.

The current suite violates this rule in 19 places. We will clean those names
up as a small migration effort. The cleanup can consolidate, split, or rename
tests, but the result must leave every scenario with a unique and descriptive
name. The violations are listed in the appendix.

## What counts as one runnable test?

For a Scenario Outline, Behave runs one test for each `Examples` row. The row
may contain `release` and `machine_type`, but it can also contain package
versions, services, commands, expected output, or other values that change
what the test proves.

For example, two rows can both run on `noble` and `lxd-vm` while testing
different packages. A coverage report may count the Noble LXD target once, but
the runner must still be able to run and report both rows separately.

We will use these rules:

* one `Examples` row is one runnable case;
* the case keeps every value from the row;
* the case keeps the source location of that exact row;
* the case belongs to the scenario block and `Examples` block that
  produced it;
* release and machine type alone never identify the case.

### Examples

This is a good runnable shape:

```gherkin
Scenario Outline: Enable one service
  Given a `<release>` `<machine_type>` machine
  When I enable `<service>`

  Examples:
    | release | machine_type | service   |
    | noble   | lxd-vm       | esm-apps  |
    | noble   | lxd-vm       | esm-infra |
```

The rows share one target, but `service` gives them different complete
parameter bindings. They are two distinct cases.

Repeating the complete binding is invalid:

```gherkin
Scenario Outline: Enable one service
  Given a `<release>` `<machine_type>` machine
  When I enable `<service>`

  Examples:
    | release | machine_type | service  |
    | noble   | lxd-vm       | esm-apps |
    | noble   | lxd-vm       | esm-apps |
```

The second row does not describe another case. Moving the duplicate into
another `Examples` block or giving that block a different name would not make
the case distinct.

Hiding the target in a plain scenario is also outside the canonical shape:

```gherkin
Scenario: Enable ESM Apps
  Given a `noble` `lxd-vm` machine
  When I enable `esm-apps`
```

The release, machine type, and service are part of the authored steps rather
than an explicit parameter binding. The normalized form is a one-row Scenario
Outline with those values in its `Examples` table.

### Referring to a case later

There are two related needs:

* select one exact case from the suite that is being read now;
* understand which case a past execution attempted, even if the source later
  moves.

The logical identity of a case is:

* the scenario identity: feature path and scenario name;
* the complete parameter binding: every column name paired with its row value.

Column order does not affect identity. The authored order is still preserved
for display, but identity compares the complete name-to-value mapping.

This follows the usual parameterized-test model.
[Pytest](https://docs.pytest.org/en/stable/how-to/usage.html#nodeids)
identifies an invocation by its test path and function plus a parameter ID.
[JUnit](https://docs.junit.org/6.1.3/writing-tests/test-templates.html) nests
each parameterized invocation under its test template.
[Cucumber](https://github.com/cucumber/messages) derives an executable pickle
from a scenario and an `Examples` row. In each case, the test definition and
one parameter binding together identify the runnable case; the parameter
values alone do not identify a test across the suite.

Behave accepts `FILE:LINE` as an input location, so the feature path and row
line remain a convenient pointer for selecting the case. Moving or reordering
the row changes this pointer without changing the logical case.

When an execution creates a job, it records a snapshot of the case it is
attempting. That snapshot includes:

* the logical case identity;
* the row line used to select it;
* the parameter-group source;
* every parameter value;
* the target.

The execution record separately keeps the repository revision and dirty state
of its checkout. It may also retain a content digest or source snapshot when
it needs to reconstruct dirty source exactly.

Changing a scenario's steps does not automatically create a new logical case.
It changes the implementation of that scenario, just as changing a test
function does not normally rename the test. The execution's repository
revision identifies which version ran.

Within one scenario, a complete parameter binding may appear only once, even
when the scenario has several `Examples` blocks. This keeps the logical
identity unambiguous. If the same values are meant to exercise different
behavior, the author must use a different scenario name or add a parameter
that describes the difference.

After a row moves, its scenario identity and complete parameters still find
the same logical case. The historical execution retains the old line as the
pointer it used and the repository revision as the source version it ran.

## What is a target?

The target is the environment where a case runs. For this work, the target
consists of:

* an Ubuntu release, such as `noble`;
* a machine type, such as `lxd-vm`.

Other row values describe the test, not its environment. A package version or
guest-container release does not become part of the target merely because it
appears in the same row.

Several cases can share one target. This lets coverage tools count distinct
environments while execution tools retain every runnable case.

Release and machine type are the complete target for this work. We do not need
to design a future target model now. If another environment property later
affects coverage or availability, that work can extend the target definition
and update its consumers deliberately.

## What does an `Examples` block mean?

An outline can have more than one `Examples` block. Even though the current
main branch does not use that shape, Behave supports it and the coverage work
expects to use it.

Each block can describe a different group of cases. For example, one block may
cover containers across all supported releases while another covers public
cloud machines on a smaller release set.

We will use this rule:

* one `Examples` block is one parameter group;
* the group owns the rows written inside it;
* the group states its own complete coverage intent;
* the group does not inherit coverage intent from another block;
* tools do not merge separate groups merely because they belong to the same
  scenario.

This is a rule we are choosing and will enforce. We do not support inheritance
or grouping across separate `Examples` blocks.

Every `Examples` block must contain at least one row. Every row must contain
explicit `release` and `machine_type` values. Tools do not infer either value
from scenario steps or neighboring rows.

Two rows in the same scenario must not contain the same complete parameter
binding, even when they belong to different parameter groups. Duplicate
bindings are structural errors because they produce the same logical case
more than once.

## How should plain scenarios fit?

Six current scenarios do not use an `Examples` table. They all write a literal
release and machine type in their first machine-setup step.

Each can be converted to the canonical shape:

1. change `Scenario` to `Scenario Outline`;
2. replace the literal release and machine type with placeholders;
3. add an `Examples` block with one row.

We will normalize these six scenarios rather than add an implied
parameter-group concept solely to preserve the old shape. The affected
scenarios are listed in the appendix.

## Which Behave source shapes can the reader accept?

The shared reader needs a clear boundary between source it supports and source
it must reject. This boundary should describe the meaning we require from the
source, rather than restating every syntax rule that Behave already enforces.

The normalized suite accepts this canonical shape:

* a release-dependent test is a `Scenario Outline`;
* it contains one or more non-empty `Examples` blocks;
* each block is an independent parameter group;
* each row explicitly supplies `release` and `machine_type`;
* each row in a group has a unique complete set of values.

### Tables and columns

Every `Examples` table has at least one column. Column names are non-empty and
unique within the block. `release` and `machine_type` are required exact,
case-sensitive names. Other column names are not restricted to a new naming
convention.

Every row has exactly one value for each column. The release and machine-type
values must be non-empty and known to their repository-owned catalogs. Other
values may be empty because an empty value can be a meaningful test input.

An optional `Examples` name is display text only. A block is identified by
its feature path and source line, not by that name.

### Placeholders

Every placeholder that Behave can substitute in a scenario's name, steps,
multiline text, or step table must have a matching column in each of that
scenario's parameter groups. A missing value is a structural error rather
than a placeholder left in the rendered test.

An `Examples` column does not have to appear as a placeholder. Hooks and other
test support can inspect the active row, so the reader preserves unused
columns as case parameters instead of guessing that they are mistakes.

### Other Behave constructs

A feature-level `Background` is accepted. Behave applies its steps to every
case in the feature. It does not create a scenario, parameter group, case, or
coverage declaration of its own.

`Rule` is not supported in this contract. It introduces another ownership and
tag scope that the current suite and model do not need. A file containing a
`Rule` produces a structural error until the model deliberately adds that
concept.

A plain `Scenario` is not part of the normalized release-dependent suite
because it has no explicit parameter group or target row. The current plain
scenarios are migration input and will be converted to one-row outlines. The
migration comparison tool can read the old shape without making it part of
the production contract.

### Tags

Coverage tags are valid only on an `Examples` block. A coverage tag on a
feature or scenario is a structural error. Duplicate coverage facts are also
structural errors; the decoder does not silently collapse repeated selectors,
machine types, bounds, or exceptions.

Behave remains responsible for the syntax of unrelated tags. The reader
preserves those tags at their authored feature, scenario, or `Examples` scope
and derives effective case tags from those lists.

### Failure boundary

Behave's parser reports invalid Gherkin syntax. The shared reader reports
valid Gherkin that violates the narrower contract above. The distinction is
useful internally, but both are structural errors to the caller: the
diagnostic identifies the affected source and no partial normalized suite is
produced.

## What information comes from a feature file?

The feature-file reader must preserve enough information for execution,
coverage, and useful errors.

The existing parsers and `pro-e2e` prototype show that the shared reader needs
to keep:

* the feature path and feature title;
* every feature, scenario, and `Examples` tag;
* each scenario's exact name and source location;
* each `Examples` block's name, source location, columns, and tags;
* every row's source location and complete values;
* the release and machine type derived for each row;
* effective tags derived for each case.

The prototype currently keeps scenario names, tags, column names, and distinct
release/machine pairs, but it discards complete row values and several source
locations. The shared reader must add those missing details.

We will retain all authored tags for now. We can remove tags that become
irrelevant only after the suite migration shows that they are no longer
needed.

Tags remain attached to the source level where they were written:

* feature tags apply to every scenario and case in the feature;
* scenario tags apply to every parameter group and case in the scenario;
* `Examples` tags apply only to cases in that parameter group.

The reader may provide the combined effective tags for a case, but that is a
derived view. It does not replace the authored feature, scenario, and
parameter-group tag lists.

Coverage tags are valid only on an `Examples` block. A coverage tag on a
feature or scenario is a structural error because its ownership would be
ambiguous. Other Behave tags keep their normal inheritance behavior.

The current main branch has feature and scenario tags but no `Examples` tags.
The coverage migration will introduce the first `Examples` tags. Thirteen
current `Examples` blocks have no name, so a parameter-group name cannot be
used as its identity; its source location remains the reliable reference.

Behave-specific objects stay inside the feature-file reader. Other parts of
the system should not need to understand Behave parser classes or raw Gherkin
nodes.

## What normalized information do consumers receive?

The shared model needs a logical contract before we choose a serialized file
format. This section should describe the information and relationships every
consumer can rely on. A later implementation can choose concrete classes and
an output encoding without changing those meanings.

### Suite

One suite is the complete result of reading one input tree. It contains an
ordered list of features and identifies the framework adapter that produced
them. It does not contain a mixture of valid objects and parse failures.

Repository revision and dirty state remain command or execution provenance.
They are not fields on the suite, its cases, or its coverage records.

### Feature and scenario

A feature contains its repository-relative path, title, source reference,
authored tags, and scenarios. Its path is its identity.

A scenario contains its exact name, source reference, authored tags, and
parameter groups. Its feature path and unique name form its identity. Each
authored `Scenario Outline` is one scenario; there is no separate scenario
definition layer.

### Parameter group

A parameter group contains its source reference, optional authored name,
ordered columns, authored tags, complete coverage declaration, and cases. Its
feature path and `Examples` source line identify it within one suite. The
optional name does not participate in identity.

### Case

A case contains its source reference, complete ordered parameters, target,
and effective tags. Containment connects it to its feature, scenario, and
parameter group without copying their identities into a second authoritative
location.

The scenario identity and complete parameter binding identify the case. Its
row source line is a selectable pointer and may change without changing the
logical case.

### Coverage record

The normalizer provides one coverage record for each parameter group. It is a
view of the same model, not a second source of truth. It contains or references
the group identity and source, scenario and feature context, declaration,
cases with their complete parameters, and distinct observed targets.

The record remains non-runnable. A consumer that needs to execute a case uses
the suite's case reference rather than treating the coverage record itself as
a test.

### Ordering and completeness

Features are ordered by repository-relative path. Scenarios, parameter groups,
cases, columns, authored tags, and parameters retain source order.

Semantically unordered declaration values use a canonical order in normalized
output:

* selectors use the documented vocabulary order;
* release lines use `lts`, then `interim`;
* machine types sort by their exact name;
* exceptions sort by release chronology, then whole-release before
  target-specific exceptions, then machine type;
* distinct observed targets retain the order of their first case.

The same valid source therefore produces the same normalized output.
Structural errors produce diagnostics but no suite or coverage records.

### Contract evolution

The logical contract and its serialized form use the same public version. A
consumer must be able to discover that version from every serialized output,
although the output format will decide where it is written.

Changing identity, removing or renaming required information, changing a
field's meaning, or changing a required relationship is a breaking version
change. Adding optional information or a new controlled value is compatible
when older consumers can safely ignore it. Internal Python class names and
the Behave package version are not part of this compatibility promise.

## What does coverage mean?

Coverage analysis uses three separate sets of facts.

### What we test now

The cases in the feature files show the targets that are present today. This
is observed coverage.

### What we intend to test

Each parameter group needs a declaration of the releases and machine types it
is meant to cover. This may follow release lifecycles, use fixed historical
releases, set upper or lower bounds, or record temporary exceptions.

This is intended coverage. Rolling declarations must not infer it from the
current rows, because deleting a row would then also delete the evidence that
the target was intended. A `fixed` declaration is the deliberate exception:
it marks the current rows as the complete intent.

Rolling declarations must name their machine types explicitly. Tools do not
infer intended machine types from the rows that happen to exist.

A fixed declaration means that the group's current rows are intentionally
fixed. It does not repeat those targets in a separate declaration. Fixed
coverage does not expand as releases move through their lifecycles. The tag
does not prevent authors from editing or removing rows. Those changes are
ordinary decisions for the author and reviewer.

Bounds and exceptions may eventually have human explanations. The shared
model keeps an optional place for a reason, but the Behave adapter leaves it
empty for now because Behave tags cannot carry free text. Normalized records
do not require a reason.

### What we can test

Not every machine type is available for every release. Repository-owned data
must say whether each known release and machine-type pair can be used.

This is target availability. It must not be inferred from current rows,
because the absence of a row is exactly what coverage analysis needs to
explain.

The repository defines the known target space from two sources:

* the release catalog defines known releases;
* the machine-type catalog defines known machine types.

Applicability data lists the releases offered for each known machine type.
Within the known target space, a pair omitted from that list is explicitly not
applicable rather than unknown. This compact form still supports machine types
with discontinuous release availability.

Unknown releases, unknown machine types, duplicate values, malformed data, or
a known machine type missing from the applicability data are structural
errors.

### Verification against the prototype

The prototype mostly keeps these facts separate:

* actual targets come from the feature-file rows;
* release selectors, bounds, and exceptions come from coverage tags;
* target availability comes from repository-owned machine-type data.

Another review found two places where the prototype boundaries need
correction:

* When a declaration does not name machine types, the coverage calculation
  falls back to the machine types in the current rows. This makes intended
  coverage depend on observed coverage. Deleting the last row for a machine
  type can therefore delete the intent as well. The new design requires
  explicit machine types instead.
* The prototype reports malformed tags and unsupported source shapes in the
  same `Finding` list as missing coverage and always exits successfully. This
  mixes invalid input with coverage analysis at the reporting boundary.

The new design keeps all three facts independent and removes those two leaks.

## What does a coverage declaration contain?

A coverage declaration belongs to one parameter group and describes the
targets that group is intended to cover. This section defines the declaration
independently of the Behave tags used to write it.

The accepted starting rules are:

* every parameter group has one complete declaration;
* separate groups do not inherit from or combine with one another;
* rolling declarations name their intended machine types explicitly;
* a `fixed` declaration marks its current rows as its complete intent;
* optional human reasons exist in the model but may be empty.

### Release selection

The repository release catalog assigns each known release to the `lts` or
`interim` release line and gives it a lifecycle status for an explicit
analysis date.

The supported selectors mean:

* **supported LTS:** shipped LTS releases in standard support;
* **ESM LTS:** LTS releases in expanded security maintenance;
* **legacy LTS:** LTS releases in the Legacy support period;
* **interim:** shipped interim releases in standard support;
* **latest LTS:** the newest shipped LTS release in catalog order, regardless
  of which supported LTS releases overlap with it.

A development release is not selected until it ships. Selectors are additive:
the intended release set is their union. `latest LTS` may be combined with
the lifecycle selectors.

### Fixed coverage

Fixed coverage does not resolve lifecycle selectors and does not grow when
release status changes. Its intended targets are the current rows. Manual row
changes remain ordinary author-and-reviewer decisions.

`Fixed` is a complete declaration by itself. It cannot be combined with
rolling selectors, bounds, machine-type declarations, or exceptions because
those values would have no independent work to do.

### Bounds

A declaration may contain at most one lower and one upper bound for each
release line. Bounds are inclusive and use the release catalog's chronological
order. They constrain every selector for that line, including `latest LTS`.

The bound release must be known and belong to the named line. The declaration
must contain a selector for that line, and the lower bound must not come after
the upper bound. Violations are structural errors.

### Machine types

A rolling declaration explicitly names one or more machine types. The intended
target set starts with the cross-product of the selected releases and those
machine types, then removes active exceptions. Applicability remains a
separate fact that semantic analysis applies afterward.

Unknown and duplicate values are structural errors. No machine type is
inferred from current rows.

### Exceptions

A rolling declaration can exclude:

* exclude a release for every intended machine type;
* exclude one release and machine-type pair;
* make either exclusion temporary until a stated date.

An exception's release must be known, belong to a selected release line, and
fall within that line's bounds. A target-specific exception must name one of
the declaration's machine types. These checks use stable catalog facts rather
than the release's lifecycle status on the day validation runs.

Expiry dates use ISO `YYYY-MM-DD`. A temporary exception is active through
that date and expires on the following day. An expired exception remains
structurally valid and can become a later semantic finding.

Duplicate exceptions and overlapping whole-release and target-specific
exceptions for the same release are structural errors. An exception for an
inapplicable target is interpretable but redundant, so it is not a structural
error.

### Valid and conflicting combinations

| Combination | Result |
| --- | --- |
| Several lifecycle selectors | Valid; their release sets are combined. |
| `latest LTS` plus LTS lifecycle selectors | Valid; the union is used. |
| A rolling selector with one or more machine types | Valid. |
| `fixed` by itself | Valid. |
| `fixed` plus any other coverage fact | Structural error. |
| A rolling selector without machine types | Structural error. |
| Other rolling facts without a selector | Structural error. |
| A bound for an unselected line | Structural error. |
| Any repeated coverage fact | Structural error. |
| An exception outside its selected line or bounds | Structural error. |

### Normalized declaration

The normalized declaration contains:

* mode: `rolling` or `fixed`;
* rolling selectors;
* optional lower and upper bounds by release line;
* intended machine types;
* whole-release and target-specific exceptions with optional expiry dates;
* optional reason fields, empty for Behave-derived declarations.

The parameter group separately retains its authored tags and source reference
so a diagnostic can identify the declaration that produced each value.

## When should invalid source stop processing?

Some problems mean that a tool cannot describe the suite. Examples include:

* a feature file that cannot be parsed;
* duplicate scenario names in one feature;
* an empty `Examples` block;
* a row without explicit `release` and `machine_type` values;
* two rows in one scenario with the same complete parameter binding;
* a row whose release or machine type cannot be determined;
* a coverage declaration that is malformed or contradictory;
* an unknown release or machine type;
* two source items that the design cannot distinguish.

These are structural errors. They behave like compiler errors.

A command may collect and report several structural errors in one run, but it
returns a failure and no normalized suite or coverage output. The maintainer
fixes the source before any consumer continues.

This avoids a partial suite being mistaken for a complete one. We do not need
rules for preserving valid fragments because partial output is not a
requirement.

## What is a coverage finding?

A coverage finding is a difference discovered after the source has been read
successfully. For example, a parameter group may declare that it covers all
supported LTS releases but have no Noble case.

The classification rule is:

* A **structural error** means the source violates a rule required to build a
  complete and trustworthy description of the suite.
* A **coverage finding** means the source is valid, but the cases differ from
  the declared intent or target availability.

Structural errors include:

* parse failures;
* unknown target values;
* malformed or conflicting declarations;
* unsupported source shapes;
* ambiguous test identity.

Coverage findings include:

* missing intended targets;
* unexpected targets;
* expired exceptions;
* cases on unavailable targets.

Structural errors block validation. Coverage findings remain separate so the
project can decide later how to review or act on them.

## How does execution connect to the suite?

The shared suite describes tests before they run. Execution adds attempts and
results:

* one job attempts one exact case;
* retrying the case creates another job;
* a completed job produces a result;
* a job does not redefine the case;
* a result does not determine coverage intent.

Execution needs the case reference, complete parameters, target, source
location, and effective tags. An execution tool may derive its configuration
requirements from those tags. This is enough to select the exact Behave row
and connect the result back to the suite.

The execution system alone owns:

* job and campaign IDs;
* queue, start, and completion times;
* attempts and retries;
* commands and environment values;
* worker pools and capacity;
* report and log locations;
* running, interrupted, and completed state;
* outcomes, reasons, and failures.

These facts describe an attempt to run a case. They do not belong to the case
or affect coverage intent.

The shared model must make exact cases available to execution tools. Updating
`pro-e2e` to run one exact case is downstream work and is not part of this
epic.

## How will we verify the migration?

The migration will rename scenarios, convert plain scenarios to outlines, and
may reshape some feature files. Source lines and scenario names will therefore
change even when runnable behavior does not.

The migration should include a small comparison script built directly on the
Behave parser. It is migration tooling, not the production suite parser,
because it must understand both the old non-canonical shapes and the new
canonical shapes.

The script accepts:

* a checkout of the revision before migration;
* a checkout containing the migrated suite;
* a mapping for intentional scenario renames or moves.

It produces a case manifest for each checkout containing:

* the feature path;
* the source scenario line, so duplicate old names remain distinguishable;
* the scenario name;
* every parameter value;
* the target;
* the ordered, rendered step text;
* all effective tags.

The rename map keys an old scenario by feature path and source line, then names
its expected feature path and scenario name after migration. This works even
when several old definitions shared the same name.

After applying the map, the comparison treats each manifest as a multiset,
ignores source lines, and checks that the runnable cases are unchanged. Using
a multiset ensures that accidentally deleting one of two equivalent cases is
still visible.

Any remaining difference requires human review and an explanation. This keeps
manual review focused on intentional consolidation or restructuring instead
of asking reviewers to compare the entire suite by hand.

The script should emit a machine-readable diff and a concise review summary.
It should return a failure when unexplained cases were added, removed, or
changed. Reviewers can run it against two local worktrees without executing
the integration tests.

## What the finished design must establish

The design is ready when we can demonstrate all of the following against real
feature files:

* every authored scenario block is represented;
* every runnable row remains a separate case with all of its values;
* cases that share a target remain distinct;
* scenario names are unique within each feature;
* every `Examples` block owns its cases and coverage intent;
* every parameter group contains at least one row;
* every row has explicit release and machine-type values and has a unique
  complete parameter binding within its scenario;
* the six plain scenarios use the canonical outline-and-examples shape;
* current coverage, intended coverage, and target availability remain
  separate;
* unreadable or ambiguous source produces an explicit error and no partial
  output;
* coverage differences do not masquerade as structural errors;
* execution can refer to one exact case without depending on coverage
  summaries.

## Appendix: plain scenarios to normalize

* `features/api/security.feature:4`
  * `Call Livepatched CVEs endpoint`
  * `xenial` on `lxd-vm`
* `features/cli/enable.feature:586`
  * `Cloud overrides for a generic aws Focal instance`
  * `focal` on `aws.generic`
* `features/cli/security_status.feature:94`
  * `Check for livepatch CVEs in security-status on an Ubuntu machine`
  * `xenial` on `lxd-vm`
* `features/cli/security_status.feature:112`
  * `Run security status in an Ubuntu machine`
  * `xenial` on `lxd-container`
* `features/livepatch.feature:256`
  * `Attached enable livepatch on a machine with fips active`
  * `bionic` on `lxd-vm`
* `features/proxy_config.feature:945`
  * `Enable realtime kernel through proxy on a machine with no internet`
  * `jammy` on `lxd-vm`

## Appendix: duplicate scenario names to clean up

The following exact-name duplicates violate the uniqueness rule. Their cleanup
may consolidate tests, split behavior, or assign more descriptive names.

* `features/_version.feature`
  * `Check pro version`: lines 4 and 94
* `features/api/fix_plan.feature`
  * `Fix command on an unattached machine`: lines 3, 184, 768, 1910,
    and 2991
* `features/apt_messages.feature`
  * `APT News`: lines 202 and 669
  * `APT news selectors`: lines 764 and 1168
* `features/cc_eal.feature`
  * `Attached enable Common Criteria service in an ubuntu lxd container`:
    lines 5 and 51
* `features/cis.feature`
  * `Attached enable of cis service in a ubuntu machine`: lines 4 and 89
* `features/cli/attach.feature`
  * `Attach and Check for contract change in status checking`: lines 207
    and 254
* `features/cli/security_status.feature`
  * `Run security status in an Ubuntu machine`: lines 112, 455, and 1108
* `features/cli/status.feature`
  * `Attached status in a ubuntu machine`: lines 379 and 426
  * `Simulate status in a ubuntu machine`: lines 1131, 1176, 1219, 1260,
    and 1301
  * `Simulate status with expired token in a ubuntu machine`: lines 1342,
    1392, 1440, and 1486
  * `Unattached status in a ubuntu machine`: lines 661, 747, 828, 905,
    and 982
* `features/daemon.feature`
  * `daemon does not start when not on gcpgeneric or azuregeneric`:
    lines 302 and 330
* `features/esm.feature`
  * `enable esm in a machine with -updates disabled`: lines 3 and 36
* `features/fix.feature`
  * `Fix command on a machine without security/updates source lists`:
    lines 926 and 1150
  * `Fix command on an unattached machine`: lines 35, 231, 619, and 978
* `features/i18n.feature`
  * `Translation works`: lines 3 and 40
* `features/install_uninstall.feature`
  * `Package ubuntu-advantage-tools now install`: lines 117 and 129
* `features/retry_auto_attach.feature`
  * `auto-attach retries for a month and updates status`: lines 3 and 133

## Decision notes

Decisions made on 2026-11-16:

* Require scenario names to be unique within a feature. Existing duplicates
  will be consolidated, split, or renamed in a small cleanup effort.
* Identify a case by scenario identity and its complete parameter binding.
  Keep the row line as a selectable source pointer. Revision and dirty state
  belong to the checkout or execution record, not the case.
* Define a target as release and machine type. Those are the environment
  properties used by current execution and coverage.
* Give each `Examples` block complete, independent coverage intent.
  Inheritance and cross-block grouping make intent harder to review.
* Require non-empty parameter groups with explicit release and machine-type
  values. Duplicate parameter bindings within a scenario are structural
  errors.
* Require rolling declarations to state machine types explicitly. They are
  never inferred from current rows.
* Let fixed declarations use their rows as the fixed target set. The tag
  requires no historical check when an author edits or removes a row.
* Keep optional reasons in the shared model, but allow the Behave adapter and
  normalized records to leave them empty.
* Convert plain scenarios to one-row outlines. One canonical source shape is
  simpler than implied groups.
* Retain all authored tags initially. We can remove obsolete tags after
  migration evidence exists. Preserve their authored scope and derive
  effective tags separately.
* Define known targets from release and machine-type catalogs. Applicability
  data gives every known pair an applicable or not-applicable answer.
* Fail without normalized output on structural errors. Partial output is not
  required and could be mistaken for a complete suite.
* Verify migration with before-and-after case manifests, an explicit rename
  map, and human review of any remaining differences.

