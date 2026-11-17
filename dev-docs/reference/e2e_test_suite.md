# End-to-end test suite reference

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

## Structural errors

A **structural error** means a tool cannot safely describe part of the suite.
Examples include a feature file that cannot be parsed, an invalid declaration,
or a release-dependent row without enough information to identify its target.
Structural errors make validation fail and identify the affected source.

Differences between current cases and coverage intent are checked only after
the source passes structural validation. See
[How end-to-end test coverage is determined](../explanation/e2e_test_coverage.md).

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
