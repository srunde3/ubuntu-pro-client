# How end-to-end test coverage is determined

Coverage is checked separately for each parameter group. In Behave, a
parameter group is one `Examples` block. The other suite terms used here are
defined in the
[end-to-end test suite reference](../reference/e2e_test_suite.md).

The coverage tool compares three facts about the parameter group:

1. **Current cases:** the cases that are present in the feature files now.
2. **Coverage intent:** the targets that the parameter group is meant to cover.
3. **Applicability:** the targets that the repository can actually use.

These facts answer different questions. A missing target might be a real gap,
an intentional exception, or a target that is not available.

## Which targets are present now?

The coverage tool reads every row in the `Examples` block and collects its
distinct targets.

Several cases may use the same target. For example, two rows may both use
`noble` on `lxd-vm` while testing different services. Both cases remain in the
suite, but the coverage check counts the Noble LXD target once.

This coverage check concerns the `release` and `machine_type` fields. It does
not claim that every possible combination of the other row parameters is
present.

## Which targets should be present?

Each parameter group has its own coverage declaration.

A rolling declaration follows release lifecycle information. It names the
release groups to follow, such as supported LTS releases, and explicitly names
the intended machine types. It may also set release bounds or record
exceptions.

The coverage tool resolves a rolling declaration for the date of the analysis.
As Ubuntu releases move through their lifecycles, the intended release set can
change without changing the declaration. For example, an Ubuntu release can
move from standard support to ESM and might no longer be expected by a test
that tracks only standard support.

A fixed declaration means the block's current rows are correct as written.
The declaration does not expand when a new release becomes supported.

## Which targets can be used?

Applicability data says which Ubuntu releases are available for each machine
type. The coverage tool does not infer availability from the rows that happen
to exist.

An absent target is a coverage gap only when the parameter group intends to
cover it and the applicability data states that the release and machine type
can be used together.

## Example

Suppose a parameter group declares supported LTS coverage on `lxd-vm`, but its
only current row is `jammy` on `lxd-vm`.

If both Jammy and Noble are supported LTS releases on the analysis date, and
both are available on `lxd-vm`, the coverage tool reports Noble on `lxd-vm` as
missing.

If Noble is not available on that machine type, its absence is not a gap. If
the parameter group uses a fixed declaration instead, the Jammy row is the
complete intent and no Noble row is expected.

## What differences can the coverage tool report?

After the source passes structural validation, coverage analysis can report:

* an intended and applicable target with no current case;
* a current target outside the declaration;
* a case on a release and machine type that are not available together;
* an exception whose expiry date has passed.

These differences are **coverage findings**. They identify work that
maintainers may choose to correct without changing whether the suite can be
parsed.

A **structural error** is different. It means a tool cannot safely describe
part of the suite, such as a row without enough information to identify its
target. Structural errors make validation fail. Coverage findings describe
valid source and do not make structural validation fail.
