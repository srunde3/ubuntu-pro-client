# Maintain feature test coverage

Tasks for keeping the behave suite's release and machine_type coverage
current as Ubuntu releases ship and age. For how this fits together and
why, see [the explanation](../explanation/feature_test_coverage.md). For
the exact `@releases:*`/`@machine_types:*` tag syntax, see
[the reference](../reference/release_coverage_tags.md).

## Find what's missing

Run the coverage checker against your checkout:

```bash
uv run --project features features/tools/coverage_gaps.py --repo-root . --format table
```

Add `--format json` for machine-readable output. The checker scans every
`.feature` file and prints one finding per problem, grouped by status.

To check one file's scenarios instead of the whole suite, there is no
built-in filter. Pipe the table output through `grep` on the feature file
path, or use `--format json` and filter with `jq`:

```sh
uv run --project features features/tools/coverage_gaps.py --repo-root . --format json | jq '.[] | select(.feature_file == "features/_version.feature")'
```

## Interpret a finding

- **`gap`** -- a `(release, machine_type)` pair that the scenario's tags
  say it must cover, but that is not covered and has no exception. See
  "Fix a gap" below.
- **`unclassified`** -- the scenario has no `@releases:*` tags at all. See
  "Classify an untagged scenario" below.
- **`tag_error`** -- a tag is malformed or two tags conflict. Common
  causes: an unrecognized tag, `@releases:fixed` alongside a tracked
  bucket, a `since`/`until` release the catalog does not know, or (most
  often) two nodes that share a scenario name but carry mismatched
  `@releases:*`/`@machine_types:*` tags. Use the finding's detail message
  to fix the tag.
- **`non_standard_shape`** -- the scenario resolves release coverage some
  way other than the standard `Scenario Outline` and `Examples:` shape,
  for example a release hardcoded into a step. See "Stop hardcoding a
  release or machine_type" below.

## Classify an untagged scenario

To move a scenario out of `unclassified`, add `@releases:*` tags above its
`Examples:` block:

1. Decide which release line and support tier the behavior needs to keep
   working on. Start from `@releases:lts_supported`, add `@releases:lts_esm`
   if the behavior also needs to keep working as a release ages into ESM,
   and add `@releases:interim` if it applies to the interim line too.

   When this is not obvious from the scenario alone, look for a bug
   reference or explanatory comment near it, check `git log` or
   `git blame` for why the row was added, and check sibling scenarios in
   the same file for precedent. A scenario that is a regression test
   pinned to the release where it was reported or reproduced, rather than
   a behavior expected to hold across every release, is usually
   `@releases:fixed`, not a tracked bucket. If it is still not clear, try
   to get more information from past team members or other authorities on
   the domain.

   Treat `@releases:lts_legacy` with particular caution. Do not infer it
   just because a currently-legacy release, for example `xenial`, has a
   row -- that row may just be a release that is mid-deprecation and has
   not been pruned yet, not a deliberate policy of testing into legacy.
   Add `lts_legacy` only when there is real evidence the behavior needs
   checking that far into a release's life. If a scenario's name or
   comments say it targets "the latest LTS" specifically, a rolling
   pointer meant to be updated in place as new LTS releases ship, use
   `@releases:latest_lts` instead of `@releases:lts_supported`.
   `@releases:lts_supported` currently matches several LTS releases at
   once and cannot express "just the newest one".
2. If the behavior only exists from some release onward, or stopped
   applying after one, add `@releases:since:<line>:<release>` and/or
   `@releases:until:<line>:<release>`. Otherwise leave both unstated --
   unstated means unbounded, not "unknown".
3. If the scenario is intentionally scoped to a subset of machine_types,
   for example cloud-only, declare them explicitly with
   `@machine_types:<machine_type>`, one tag per type. If every
   currently-applicable machine_type is meant to be covered, this can be
   left unstated, but see the explanation doc's "Known limitations"
   section before relying on that default for a scenario you might later
   narrow.
4. Re-run the checker. Any `(release, machine_type)` pair it now reports
   as `gap` needs either a real `Examples:` row or a skip exception, as
   described in the next section.
5. Before deciding a missing release or machine_type is unexplained, check
   the surrounding lines, not just directly above the `Examples:` block.
   A pre-existing "why this row is missing" comment sometimes ends up
   misplaced below the `Examples:` table or above the next scenario,
   instead of directly above the tags it explains. This is a recurring
   authoring slip in this repo, not a one-off. If you find one, do not
   treat the gap as unexplained. Instead, fold its reason into a proper
   `@releases:skip:*` tag, moving and rewording the comment to sit
   directly above the tag, as described in "Fix a gap" below.

## Fix a gap

For each `gap` finding, decide whether it is a real hole or a deliberate
exception:

- **Real hole:** add an `Examples:` row for that `(release, machine_type)`
  pair, following the existing rows' pattern for any other columns.
- **Deliberate exception:** add a comment on its own line above the tags
  explaining why, then add `@releases:skip:<release>` to skip the whole
  release, or `@releases:skip:<release>+<machine_type>` to skip just that
  pair. Add `:until:<date>` if the exception is temporary -- it stops
  counting after that date, and the pair becomes a live gap again
  automatically. Never put the reason on the same line as the tag:
  `reformat-gherkin` silently deletes an inline trailing comment on a tag
  line regardless of formatting mode, and only a comment on its own line
  survives.

## Add a new release

When a new Ubuntu release ships, run the checker. Once the new release's
status matches a scenario's declared bucket, for example it enters
standard support and the scenario tracks `lts_supported`, a missing row
shows up as an ordinary `gap` finding on its own. No separate "new
release" step is needed. Fix each the same way you would fix any other
gap.

If it is not yet clear whether a behavior applies to the new release,
resolve that explicitly: add a row, or add a dated skip with a reason.

## Keep `machine_types.yaml` current

`features/machine_types.yaml` is the hand-maintained record of which
machine_types are actually offered on which releases. Add new releases to
the applicable machine types once they are ready to be tested.

Occasionally an older release becomes available on a machine type it was
not previously available on, for example fips. Add it once it is
available on that machine type and ready to be tested.

Rarely, a new machine type gets added. Add a new top-level entry for the
machine type and list the releases it applies to. When this happens, also
update the `Examples:` tables to include coverage for the machine type
where relevant.

## Handle a release becoming newly eligible

A release can start applying to a scenario without a new release
shipping, for example because of a lifecycle change or a backported
feature. Add the new row. If an old skip record exists for that
`(scenario, release)` pair, the new coverage supersedes it, so delete the
skip record.

## Split a scenario when preconditions diverge by release

If a behavior genuinely needs different preconditions for different
release families, split it into multiple `Scenario Outline` nodes that
share the exact same name -- that is the supported pattern. Real example:
`fix.feature`'s `Fix command on an unattached machine` is split into
three `Scenario Outline` blocks, because one needs `contract_token` and
the others do not.

## Stop hardcoding a release or machine_type

A plain `Scenario`, or a step, pinned to one hardcoded release or
machine_type is not a supported pattern. It cannot be checked by any of
this tooling and will not show up as `unclassified` or anything else; it
is simply invisible. Refactor it into a one-row `Scenario Outline` with
`<release>`/`<machine_type>` placeholders, matching the standard shape,
then tag it like any other scenario.
