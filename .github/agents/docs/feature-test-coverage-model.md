# Feature Test Coverage Model

Status: draft reference. Not yet distilled into a `rules/*.mdc` component -- see
[open-plugins.com](https://open-plugins.com/agent-builders/components/rules)'s
rules spec (`.mdc`, frontmatter, ~500-token body budget) before splitting this
into one or more loadable rules.

## Golden test structure

The default, expected shape for any scenario that expresses release-specific
behavior:

- One `Scenario Outline` per distinct behavior, uniquely named within its
  feature file.
- One `Examples:` block under it.
- `release` and `machine_type` are always placeholders (`<release>`,
  `<machine_type>`) resolved from the Examples table -- never hardcoded into
  a `Given`/`When`/`Then` step.
- Rows ordered chronologically by release (oldest first).

Reference example: `features/api/security.feature`'s
`Call package manifest endpoint for machine`:

```gherkin
Scenario Outline: Call package manifest endpoint for machine
  Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
  ...
  Examples: ubuntu release
    | release  | machine_type  | package_name   | ... |
    | xenial   | lxd-container | libgnutls30    | ... |
    | bionic   | lxd-container | libgnutls30    | ... |
    | focal    | lxd-container | libgnutls30    | ... |
    | jammy    | lxd-container | libgnutls30    | ... |
    | noble    | lxd-container | libgnutls30t64 | ... |
    | resolute | lxd-container | libgnutls30t64 | ... |
```

Under this shape, one `Scenario Outline` maps to exactly one coverage record:
the set of `(release, machine_type)` rows in its Examples table.

## What belongs in step/setup helpers vs. the Examples table

Not every difference between releases belongs in the Examples table. Two
questions decide whether a difference can be abstracted into a step/setup
helper, or must stay visible -- as a parameterized column, or a separate
scenario per the precondition-split pattern above:

1. **Is it part of getting the machine into position, or part of what's
   being asserted?** Setup/plumbing -- launching the right base image,
   picking the right interpreter, installing a tool -- is incidental to the
   behavior under test and is fair game to centralize in a helper.
2. **If it's in the assertion path, is the difference meaningful to the
   behavior being verified, or merely presentational?** A difference a user
   of the product would experience as a functional change must stay
   visible. A difference that's cosmetic noise around the same functional
   result (formatting, coloring, whitespace) can be normalized away.

If both answers are "this doesn't change what the test is actually
verifying," abstraction is appropriate. If either answer is "yes, this
reflects genuinely different behavior," it MUST stay visible -- as an
Examples column (see `package_name`/`manifest_pattern`/`oval_def_id` in the
golden example above, which vary because the packages themselves genuinely
differ per release, e.g. `libgnutls30` vs `libgnutls30t64`) or a separate
scenario, not smoothed over in a helper.

**OK to abstract:**

- Interpreter/tooling differences needed to reach the same starting state
  (e.g. Python 2 vs 3 across releases).
- Installing/configuring a tool used by the test but not itself under test
  (e.g. `security_tools.py`'s oscap installation).
- Insignificant presentational differences in output that don't change the
  functional result (e.g. ANSI coloring).

**MUST stay visible, not abstracted:**

- Differences in the actual behavior, output, or package/service surface
  being verified -- even if abstracting them would let one Examples row
  stand in for several releases.

**The failure mode to avoid:** adding abstraction *because* it shrinks or
simplifies the Examples table, rather than because the thing being hidden is
genuinely incidental. A clean matrix is a side effect of good
parameterization, not a goal to abstract toward. If a helper's only
justification is "this keeps the table smaller," that's a sign a real
difference is being hidden rather than genuinely abstracted away.

## Release categories

Every release currently on `main` falls into exactly one category (see
`.github/agents/tools/release_catalog.py`):

- `devel` -- in development, not yet released.
- `active_lts` -- an LTS release in standard support.
- `active_interim` -- a non-LTS release in standard support.
- `esm` -- past standard support, in Extended Security Maintenance.
- `legacy` -- fully EOL, no ESM. Should not persist on `main` -- see
  Retirement, below.

`active_lts`, `active_interim`, and `devel` are single forward-looking
targets: is the newest release in the category covered? `esm` is an
existence invariant over the whole set: has any currently-ESM release lost
coverage, not just whether the newest one has it?

## Explicit skip records

A scenario may deliberately have no row for a release that's otherwise a
target. This is recorded, not inferred:

- One record per `(feature_file, scenario_name, release)`.
- Records are append-only. A changed decision is a **new** record with a
  later `confirmed_on` date, not an edit -- the most recent record for a
  given key wins.
- A record only ever asserts something about the one release it names. It
  never implies anything about later releases on the same track, and it
  never "expires" on its own -- if a release's eligibility changes, that's a
  new record, not a stale one silently overridden.

## Update scenarios

**A new release ships** (e.g. stonking for 26.10).
For every scenario where the previous target release (on the LTS or interim
track, whichever applies) is covered, add a row for the new release,
following existing precedent for `machine_type` and any other columns. If
it's not yet clear whether the behavior applies, that ambiguity is resolved
explicitly -- add a row, or record a skip -- not left unresolved.

**A release becomes eligible for something it previously wasn't** (lifecycle
or product change, e.g. a feature gets backported).
Add the row. If an old skip record exists for that `(scenario, release)`
pair, it's superseded by the new coverage -- no need to delete it, the row
itself is now the current source of truth, and the record remains a true
historical statement about what was known when it was made.

**A behavior genuinely needs different preconditions per release-family**
(real example: `features/fix.feature`'s `Fix command on an unattached
machine`, split into three `Scenario Outline` blocks because one requires
`contract_token` and the others don't).
This is a legitimate variant of the golden shape, not a violation of it.
Coverage for the behavior is the **union** of `(release, machine_type)`
combos across every `Scenario`/`Scenario Outline` node sharing that exact
name in that file. Hard bound: identical names within one feature file MUST
represent identical behavior -- a naming collision that isn't intentional
grouping is a bug, not a valid instance of this pattern.

**A release or machine_type is hardcoded into a step instead of
parameterized via Examples** (real example, flagged as debt:
`features/api/security.feature`'s `Call Livepatched CVEs endpoint`, a plain
`Scenario` pinned to `xenial`/`lxd-vm`).
Not a supported pattern going forward. Refactor into a one-row
`Scenario Outline`, matching the golden shape, rather than treating it as a
permanent exception.

**Retirement** (a release goes fully `legacy` and should be branched off
`main`).
Deferred -- not addressed by this document yet.

## Out of scope here

- Coverage is measured per-`release`; `machine_type` follows existing
  precedent for a row rather than being independently gap-checked.
- Retirement / branch-cut detection.
- The gap-detection algorithm and its tool interfaces (`coverage_gaps.py`).
