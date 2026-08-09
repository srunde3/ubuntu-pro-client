# Feature test coverage

Ubuntu ships a new release twice a year, and every release moves
through a lifecycle -- development, standard support, ESM, legacy, then
end of life. The behave suite under `features/` tests specific `(release, machine_type)` combinations, e.g. "`pro attach` on `noble` `lxd-container`".
As time passes, these combinations needs to be updated: a new release ships and
needs a row added, an older one ages into a support tier the scenario cares
about and still needs checking, and the oldest release may enter EOL. We may
also ship additional machine types that need coverage on some or all scenarios.

This document explains the three pieces that make coverage checks possible: how
a scenario expresses what it currently tests, how it declares what it's
supposed to test, and how the gap between the two gets found.

## Terminology

- **Release** -- one Ubuntu release, identified by its codename (e.g.
  `noble`). Also called a *series*.
- **Line** -- which release cadence a release belongs to: `lts` or
  `interim`. Permanent -- unlike status, a release's line never changes.
- **Status** (or **support tier**) -- a release's current lifecycle
  phase, which *does* change over time: `devel`, `supported`, `esm`,
  `legacy`, or `eol`. It iscomputed from the release/EOL dates. Only
  `supported`, `esm`, and `legacy` are things a scenario can declare it
  tracks (`@releases:lts_supported`, `@releases:lts_esm`,
  `@releases:lts_legacy`, `@releases:interim`) -- `devel` and `eol` aren't
  trackable tiers, they are just states a release passes through on the way in
  and out of the ones that are.
- **Machine type** -- the substrate/environment a scenario runs against,
  e.g. `lxd-container`, `aws.pro`.
- **Tracks** -- the set of (line, status) buckets a scenario declares it
  must currently cover, via one or more tracked-bucket tags (e.g.
  `@releases:lts_supported`). This is the core declaration everything
  else refines.
- **Since / until** -- an optional lower or upper release bound on one
  line, narrowing `tracks` for a behavior that only exists from some
  release onward, or stopped mattering after one. Unstated means
  unbounded on that side.
- **Applicable** -- whether a machine type is actually offered
  as a real product for a given release. Sourced from
  `features/machine_types.yaml`, independent of anything a
  scenario declares.
- **Exception** (or **skip**) -- a deliberate, explained hole in
  otherwise-required coverage, declared with `@releases:skip:*` and a
  reason. Optionally scoped to one machine type, optionally temporary.
- **Coverage** -- the set of `(release, machine_type)` pairs a
  scenario's `Examples:` table actually has rows for right now.
- **Finding** -- one reported problem from `coverage_gaps.py`: `gap`,
  `unclassified`, `tag_error`, or `non_standard_shape` (see "Finding
  what's missing" below).

## How a scenario expresses its current coverage

The default shape for any scenario whose behavior varies by release:

- One `Scenario Outline`, uniquely named within its feature file.
- One `Examples:` block under it, with `release` and `machine_type`
  columns.
- Every `Given`/`When`/`Then` step uses `<release>`/`<machine_type>`
  placeholders resolved from that table
- Rows ordered chronologically, oldest release first.

```gherkin
Scenario Outline: Call package manifest endpoint for machine
  Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
  ...
  Examples: ubuntu release
    | release | machine_type  | base_version    | CVE_ID      |
    | xenial  | lxd-container | 3.4.10-4ubuntu1 | 39991000000 |
    | bionic  | lxd-container | 3.5.18-1ubuntu1 | 55501000000 |
    | focal   | lxd-container | 3.6.13-2ubuntu1 | 55501000000 |
    | jammy   | lxd-container | 3.7.3-4ubuntu1  | 55501000000 |
```

The existing coverage for a scenario is simply the set of (release, machine_types)
present in the Examples block.

### When one behavior needs more than one Examples block

A single `Scenario Outline` node can also have more than one `Examples:`
block. This can be done simply for better visual organization, or when combined
with release tags (see below), it can be a means to express different start/end
bounds for certain machine types.

```gherkin
Scenario Outline: Check pro version
  Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
  ...

  Examples: standard
    | release | machine_type  |
    | xenial  | lxd-container |
    | ...     | ...           |

  Examples: clouds
    | release | machine_type |
    | xenial  | aws.pro      |
    | ...     | ...          |
```

There are two `Examples:` blocks and two independent sets of `(release,
machine_type)` rows. `standard` and `clouds` are each their own
coverage record, not one combined record for the node as a whole.

### When one behavior needs more than one Scenario Outline

Sometimes a behavior needs different preconditions per
release-family -- `fix.feature`'s `Fix command on an unattached machine`
is a real example, split into three `Scenario Outline` blocks because one
needs `contract_token` and the others don't. Coverage for the behavior is
the *union* of `(release, machine_type)` rows across every node sharing
that exact name in the file. Identical names within one feature file **must** represent identical behavior.

Note that this is only partially self-enforcing -- if the nodes sharing a name declare different `@releases:*`/`@machine_types:*` tags, that's caught as a `TAG_ERROR` (see below), but an accidental collision between two unrelated scenarios that happen to carry the same tags, or no tags at all, wouldn't be.

## What a scenario is supposed to cover: `@releases:*`/`@machine_types:*` tags

The Examples table says what's tested today. It can't say what *should*
be tested -- that has to be a separate, deliberate declaration, or there's
nothing to check the table against except itself. That declaration is a
set of `@releases:*`/`@machine_types:*` tags on the `Examples:` block.
Full syntax is in [the tag reference](../reference/release_coverage_tags.md);
in plain terms, a scenario can declare:

- **Which release lines and support tiers it tracks** -- e.g. "every LTS
  release currently in standard support" (`@releases:lts_supported`), or
  "LTS releases in standard support *or* ESM" (add
  `@releases:lts_esm`), or both the LTS and interim lines independently.
  This is the core declaration: it answers "which buckets of releases
  must currently be represented".
- **Exactly the newest release on a line** (`@releases:latest_lts`), for a
  scenario meant to track "whatever the latest LTS is" as a rolling
  pointer, rather than every release in a status tier -- LTS support
  windows overlap, so `lts_supported` alone can't express "just the
  newest one".
- **A lower or upper bound on a line** (`@releases:since:lts:focal`,
  `@releases:until:lts:resolute`), for a behavior that only exists from
  some release onward, or stopped mattering after one. If absent, this means
  unbounded on that side.
- **Which machine_types it applies to** (`@machine_types:aws.pro`,
  repeated per type). If unstated, it defaults to whatever machine_types
  the scenario already has rows for.
- **Deliberate, explained holes** (`@releases:skip:<release>`, optionally
  scoped to one machine_type, optionally expiring on a date) -- a release
  that's excepted from the declared tracking, with a reason in a comment
  above the tag, e.g. "ESM staging was unavailable for a few weeks due to
  outage".
- **`@releases:fixed`**, for a scenario tied to one specific historical
  fact that will never track new releases going forward.

A scenario with none of these tags is **unclassified**.

## Finding what's missing

Given a scenario's declared tags, the tool (`coverage_gaps.py`) works out
what it should currently cover in three steps:

1. **What's required now.** For each declared line/status bucket,
   find every release whose *current* line and status match it. Cross it
   with the declared or table-defaulted machine_types, and drop any
   `(release, machine_type)` pair that wasn't actually offered as a real
   product.
2. **What's covered.** The Examples table's own rows, read as-is.
3. **What's excepted.** Any `(release, machine_type)` pair
   covered by an unexpired `@releases:skip:*` tag. An exception with a past
   expiry date no longer counts; that pair goes back to being a live gap the moment the date passes.

What's required, minus what's covered, minus what's excepted, is what's
missing. Each leftover `(release, machine_type)` pair is a finding: either
a real row needs to be added, or the gap is deliberate and needs a skip
tag with a reason.

### Known limitations

1. Machine types can be inferred from what's in the table, so deleting all examples of a certain machine type will not flag a coverage gap unless machine types are explicitly tagged on the scenario.
2. There is no way to express recurring or periodic holes with a single tag. Instead, use multiple exception tags.
