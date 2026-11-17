# What the end-to-end test suite contains

The end-to-end tests under `features/` describe Ubuntu Pro Client behavior on
different Ubuntu releases and machine types. This document gives names to the
parts of those tests so that the test runner, coverage checks, and other tools
describe the same thing.

This document describes the vocabulary of our tests. Behave is how the tests
are written today, but the basic model does not depend on Behave.

## A simple test

Consider this shortened test:

```gherkin
Feature: Ua fix command behaviour

  Scenario Outline: Useful SSL failure message
    Given a `<release>` `<machine_type>` machine
    When I run `pro fix`
    Then I see a useful error

    Examples: ubuntu release
      | release | machine_type  |
      | focal   | lxd-container |
      | noble   | lxd-container |
```

This file contains:

* one **feature**, which groups related behaviors;
* one **scenario**, which names the behavior being tested;
* one **parameter group**, the `Examples` block;
* two **cases**, one for each row;
* two **targets**, `focal` on `lxd-container` and `noble` on
  `lxd-container`.

Running one case creates a **job**. When that job finishes, it produces a
**result**.

## The parts of the suite

### Feature

A feature is a file that groups related scenarios. Its identity is its path
relative to the repository, for example `features/fix.feature`.

### Scenario

A scenario is one named behavior that a maintainer recognizes, selects, and
reports on, for example `Fix command on an unattached machine`.

Scenario names must be unique within a feature file. When similar behavior
needs different setup or assertions, each authored `Scenario` or
`Scenario Outline` must have a name that explains the difference.

### Parameter group

A parameter group is one authored set of cases that shares one coverage
declaration. In Behave, it is an `Examples` block.

A scenario can have more than one parameter group. Each group keeps its own
cases and coverage declaration. Tools must not merge the declarations of
separate groups.

Every parameter group contains at least one row. Each row has explicit
`release` and `machine_type` values.

### Case

A case is one concrete test with all of its parameter values filled in. In a
Scenario Outline, one `Examples` row produces one case.

The case is the smallest test that a tool can select or report independently.
Its parameters include all columns in the row, not only `release` and
`machine_type`.

Two cases can use the same target. For example, two rows might both run on
`noble` and `lxd-vm` but test different package versions. Those rows are
different cases and must not be collapsed into one.

A case is identified by its scenario and complete set of parameter names and
values. Its source line is a convenient way to select it in Behave, but moving
the row does not change which logical case it describes.

Two rows in one scenario cannot contain the same complete parameters, even
across separate parameter groups, because they would describe the same case
twice.

### Target

A target describes the environment used by a case. The current suite uses two
target fields:

* `release`, such as `noble`;
* `machine_type`, such as `lxd-vm`.

A target is not a case. It does not include the behavior under test or the
case's other parameter values.

### Job and result

A job is one attempt to run one case. Retrying a case creates another job.

A result records what a completed job established, such as passed, failed,
skipped, or error. The case still exists before it is run and after any one job
finishes.

## Canonical scenario shape

Release-dependent tests use a `Scenario Outline` with an `Examples` block:

```gherkin
Scenario Outline: Call Livepatched CVEs endpoint
  Given a `<release>` `<machine_type>` machine

  Examples: ubuntu release
    | release | machine_type |
    | xenial  | lxd-vm       |
```

The suite does not encode its target as literal values in a scenario step.
Using the same outline-and-table shape for single-case and multi-case scenarios
keeps parsing, coverage declarations, and maintenance consistent.

## Checking test coverage

Coverage is checked separately for each parameter group. The tool compares
three facts about that `Examples` block:

1. **Current cases:** the cases that are present in the feature files now.
2. **Coverage intent:** the targets that a parameter group is meant to cover.
3. **Applicability:** the targets that the repository can actually use.

These facts answer different questions. A missing target might be a real gap,
an intentional exception, or a target that is not available.

### Which targets are present now?

The tool reads every row in the `Examples` block and collects its distinct
targets.

Several cases may use the same target. For example, two rows may both use
`noble` on `lxd-vm` while testing different services. Both cases remain in the
suite, but the coverage check counts the Noble LXD target once.

This coverage check concerns the `release` and `machine_type` fields. It does
not claim that every possible combination of the other row parameters is
present.

### Which targets should be present?

Each parameter group has its own coverage declaration.

A rolling declaration follows release lifecycle information. It names the
release groups to follow, such as supported LTS releases, and explicitly names
the intended machine types. It may also set release bounds or record
exceptions.

The tool resolves a rolling declaration for the date of the analysis. As
Ubuntu releases move through their lifecycles, the intended release set can
change without changing the declaration. For example, an Ubuntu release can
move from standard support to ESM and might no longer be applicable to a test
that tracks only standard support.

A fixed declaration means the block's current rows are correct as written.
The declaration does not expand when a new release becomes supported.

### Which targets can be used?

Applicability data says which Ubuntu releases are available for each machine
type. The coverage tool does not infer availability from the rows that happen
to exist.

An absent target is a coverage gap only when the parameter group intends to
cover it and the applicability data states that the release and machine type
can be used together.

### Example

Suppose a parameter group declares supported LTS coverage on `lxd-vm`, but its
only current row is `jammy` on `lxd-vm`.

If both Jammy and Noble are supported LTS releases on the analysis date, and
both are available on `lxd-vm`, the coverage tool reports Noble on `lxd-vm` as
missing.

If Noble is not available on that machine type, its absence is not a gap. If
the parameter group uses a fixed declaration instead, the Jammy row is the
complete intent and no Noble row is expected.

### What differences can the tool report?

After the source passes structural validation, coverage analysis can report:

* an intended and applicable target with no current case;
* a current target outside the declaration;
* a case on a release and machine type that are not available together;
* an exception whose expiry date has passed.

These differences are coverage findings. They identify work that maintainers
may choose to correct without changing whether the suite can be parsed.

## Errors and coverage findings

A **structural error** means a tool cannot safely describe part of the suite.
Examples include a feature file that cannot be parsed, an invalid declaration,
or a release-dependent row without enough information to identify its target.
Structural errors make validation fail and identify the affected source.

A **coverage finding** means the source is valid but the current cases differ
from the declared intent or applicable targets. A missing intended target is a
coverage finding. It does not make the suite impossible to parse.

Keeping these separate lets CI reject source that tools cannot understand
without making a policy decision about how much coverage is enough.

## How Behave maps to these names

| Behave source | Suite concept |
| --- | --- |
| `.feature` file | Feature |
| `Scenario` or `Scenario Outline` node | Scenario |
| `Examples` block | Parameter group |
| `Examples` row | Case |
| `release` and `machine_type` values | Target |
| Coverage tags on an `Examples` block | Coverage declaration |
| File and line information | Source reference |
