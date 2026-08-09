# Maintain feature test coverage

Tasks for keeping the behave suite's release/machine_type coverage
current as Ubuntu releases ship and age. For how this all fits together
and why, see [the explanation](../explanation/feature_test_coverage.md);
for exact `@releases:*`/`@machine_types:*` tag syntax, see
[the reference](../reference/release_coverage_tags.md).

## Find what's missing

Run the coverage checker against your checkout:

```bash
uv run --project features features/tools/coverage_gaps.py --repo-root . --format table
```

(`--format json` for machine-readable output.) It scans every `.feature`
file and prints one finding per problem, grouped by status.

To check one file's scenarios instead of the whole suite, there's no
built-in filter -- pipe the table output through `grep` on the feature
file path, or use `--format json` and filter with `jq`:

```sh
uv run --project features features/tools/coverage_gaps.py --repo-root . --format json | jq '.[] | select(.feature_file == "features/_version.feature")'
```

## Interpret a finding

- **`gap`** -- a `(release, machine_type)` pair the scenario's tags say it
  should cover, that isn't covered and isn't excepted. See "Fix a gap"
  below.
- **`unclassified`** -- no `@releases:*` tags at all. See "Classify an untagged
  scenario" below.
- **`tag_error`** -- malformed or conflicting tags: an unrecognized tag,
  `@releases:fixed` alongside a tracked bucket, a `since`/`until` release
  the catalog doesn't know, or (most common) nodes sharing a scenario
  name with mismatched `@releases:*`/`@machine_types:*` tags. Use the
  finding's detail message to fix the tag.
- **`non_standard_shape`** -- the scenario resolves release coverage some
  other way than the golden `Scenario Outline` + `Examples:` shape (e.g.
  a release hardcoded into a step). See "Stop hardcoding a release or
  machine_type" below.

## Classify an untagged scenario

To move a scenario out of `unclassified`, add `@releases:*` tags above
its `Examples:` block:

1. Decide which release line(s) and support tier(s) the behavior needs to
   keep working on: `@releases:lts_supported`, add `@releases:lts_esm` if
   it also needs to keep working as a release ages into ESM, add
   `@releases:interim` if it applies to the interim line too.
   When this isn't obvious from the scenario alone, look for a bug
   reference or explanatory comment near it, check `git log`/`git blame`
   for why the row was added, and check sibling scenarios in the same
   file for precedent. A scenario that's a regression test pinned to the
   release it was reported/reproduced against -- rather than a behavior
   expected to hold across every release -- is usually `@releases:fixed`,
   not a tracked bucket. If it's still ambiguous, try to get additional
   information from past team members or other authorities on the domain.
2. If the behavior only exists from some release onward, or stopped
   applying after one, add `@releases:since:<line>:<release>` and/or
   `@releases:until:<line>:<release>`. Otherwise leave both unstated --
   unstated means unbounded, not "unknown."
3. If the scenario is intentionally scoped to a subset of machine_types
   (e.g. cloud-only), declare them explicitly with
   `@machine_types:<machine_type>`, one tag per type. If every
   currently-applicable machine_type is meant to be covered, this can be
   left unstated -- but see the explanation doc's "Known limitations"
   before relying on that default for a scenario you might later narrow.
4. Re-run the checker. Any `(release, machine_type)` pair it now reports
   as `gap` needs either a real `Examples:` row or a skip exception (next
   section).

## Fix a gap

For each `gap` finding, decide whether it's a real hole or a deliberate
exception:

- **Real hole:** add an `Examples:` row for that `(release, machine_type)`
  pair, following the existing rows' pattern for any other columns.
- **Deliberate exception:** add a comment on its own line above the tags
  explaining why, then `@releases:skip:<release>` (whole release) or
  `@releases:skip:<release>+<machine_type>` (just that pair). Add
  `:until:<date>` if it's temporary -- the exception stops counting after
  that date and the pair becomes a live gap again automatically. Never
  put the reason on the same line as the tag -- `reformat-gherkin` silently
  deletes an inline trailing comment on a tag line regardless of
  formatting mode; only a comment on its own line survives.

## Add a new release

When a new Ubuntu release ships, run the checker. Once the new release's
status matches a scenario's declared bucket (e.g. it enters standard
support and the scenario tracks `lts_supported`), a missing row shows up
as an ordinary `gap` finding on its own -- no separate "new release" step
needed. Fix each the same way as any other gap.

If it's not yet clear whether a behavior applies to the new release,
resolve that explicitly -- add a row, or add a dated skip with a reason.

## Keep `machine_types.yaml` current

`features/tools/machine_types.yaml` is the hand-maintained record of which
machine_types are actually offered on which releases. New releases must be
added to the applicable machine types when they are ready to be tested.

Occasionally an older release might be available for a machine type that it
was not previously available for (e.g., fips). The release should be added
once it is available on that machine type and is ready to be tested.

Rarely, a new machine type may be added. Add a new top-level entry for the
machine type and list the releases it is applicable to. In this case, it is
also necessary to update the `Examples:` tables to include coverage for the
machine type where relevant.

## Handle a release becoming newly eligible

A release can start applying to a scenario without a new release
shipping, e.g., a lifecycle change, or a backported feature. Add the new row.
If an old skip record exists for that `(scenario, release)` pair, it's
superseded by the new coverage; delete the skip record.

## Split a scenario when preconditions diverge by release

If a behavior genuinely needs different preconditions for different
release-families (real example: `fix.feature`'s `Fix command on an
unattached machine`, split into three `Scenario Outline` blocks because
one needs `contract_token` and the others don't), splitting into multiple
`Scenario Outline` nodes sharing the exact same name is the supported
pattern.

## Stop hardcoding a release or machine_type

A plain `Scenario` (or a step) pinned to one hardcoded release or
machine_type isn't a supported pattern -- it can't be checked by any of
this tooling and won't show up as `unclassified` or anything else; it's
just invisible. Refactor it into a one-row `Scenario Outline` with
`<release>`/`<machine_type>` placeholders, matching the golden shape, then
tag it like any other scenario.
