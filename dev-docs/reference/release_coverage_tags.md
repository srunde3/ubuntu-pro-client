# Release coverage tags reference

How the [release coverage model](../explanation/release_coverage_model.md)'s
fields (`tracks`, `since`/`until`, `machine_types`, `exceptions`) are
expressed as `@releases.*` tags in `.feature` files. The model is
syntax-independent; this is where it compromises for what Gherkin can
actually carry. `tools/release_tags.py` implements this vocabulary; keep
it in sync with this document when either changes.

## Why tags, not comments or the Examples table

- **The Examples table stays untouched.** It drives real test execution;
  metadata that isn't meant to run doesn't belong in it (see the golden
  test structure doc's rejection of inlining skip rows there).
- **Comments are free text but invisible to tooling.** `describe_feature`
  surfaces parsed Gherkin structure, not raw comments -- behave's parser
  doesn't expose them in an attributable way, and hand-rolling a second
  parser to read them is exactly the duplication this whole project has
  been avoiding. Comments are for humans only.
- **Tags are the one structured extension point that's already free.**
  `describe_feature` already returns every scenario's `tags: list[str]` --
  zero MCP changes needed to consume this. The tradeoff: Gherkin tags
  cannot contain whitespace, so free text (a `reason`) can never live in a
  tag. That's fine -- `reason` is documentation for a human deciding what
  to do about a flagged gap, not an input to the deterministic
  computation. It lives in a comment next to the tag it explains.

Net effect: **tags carry every fact `Missing(S)` is computed from; comments
carry every `reason`.** Nothing load-bearing is ever comment-only.

## Tag vocabulary

Namespace: `@releases.*`, alongside the repo's existing `@uses.config.*`
convention. `<line>` is `lts` or `interim`; `<status>` is `supported`,
`esm`, or `legacy`; `<release>` is a series codename; `<date>` is ISO
8601. Two delimiters, each meaning one specific thing, never interchanged:

- `:` -- "keyword, then a literal value." Machine_type names contain
  their own dots (`aws.pro`, `gcp.pro-fips`), so `:` separates the tag's
  keyword path from the raw value to avoid it being misread as further
  namespacing.
- `+` -- "these two identifiers are paired into one compound key," used
  only where an exception needs both a release *and* a specific
  machine_type. Reusing `:` for this too would conflate "value follows"
  with "combine these," which are different relationships.

| Tag | Meaning |
| --- | --- |
| `@releases.<line>.<status>` | one tracked bucket; repeat per (line, status) tracked |
| `@releases.fixed` | `tracks(S) = {}` explicitly -- deliberately tracks nothing, forever |
| `@releases.since.<line>.<release>` | lower bound for `line` |
| `@releases.until.<line>.<release>` | upper bound for `line` |
| `@releases.machine_types:<machine_type>` | one machine_type in the applicable set; repeat per type |
| `@releases.skip.<release>` | permanent exception, whole release |
| `@releases.skip.<release>.until.<date>` | temporary exception, whole release, expires `<date>` |
| `@releases.skip.<release>+<machine_type>` | permanent exception, one (release, machine_type) pair |
| `@releases.skip.<release>+<machine_type>.until.<date>` | temporary exception, one pair, expires `<date>` |

`UNCLASSIFIED(S)` = neither any `@releases.<line>.<status>` tag nor
`@releases.fixed` is present. `@releases.fixed` and
`@releases.<line>.<status>` are mutually exclusive.

## Tag placement: always on `Examples:`, never on `Scenario Outline:`

`@releases.*` tags go above an `Examples:` block, not above
`Scenario Outline:` -- behave supports tags on `Examples:` independently of
the scenario's own tags, and this is the only place `@releases.*` tags are
read from. A scenario with one `Examples:` block (the common case) tags
that one block; a scenario whose substrate coverage splits along a policy
boundary the release axis alone can't express (e.g. "clouds are only
tested on LTS releases, standard substrates on every release") tags each
block independently. Same rule either way -- there's no separate
"whole-scenario" mechanism to reach for.

- **A `@releases.*` tag on `Scenario Outline:` itself is a `TAG_ERROR`,**
  not silently ignored and not a fallback -- exactly the kind of
  wrong-location mistake that's easy to make out of habit and easy to miss
  if it's just quietly unread.
- **Untagged blocks are `UNCLASSIFIED`, not an error.** If some blocks in a
  scenario are tagged and others aren't, the untagged ones are
  independently undecided -- the same honest state an untagged scenario is
  in today.
- **Each block's tag set is complete on its own**, with no inheritance
  from anywhere else -- a tagged `Examples:` block declares its own
  `tracks`/`since`/`until`/`machine_types`/`exceptions` from scratch.

## Encoding rules

- **One tag per fact.** No packed/structured tag values beyond the `:`
  value-escape and `+` pairing described above. Keeps parsing trivial
  (split on `.`, check against a small fixed keyword set) and keeps diffs
  small when one fact changes.
- **Consistency across aggregated nodes is still required.** The
  precondition-split pattern (`fix.feature`'s three `Scenario Outline`s
  sharing one name) means `@releases.*` tags are a property of the
  *behavior*, not the node -- when one scenario name is split across
  multiple `Scenario Outline` nodes, every node's `Examples:` block(s) must
  carry identical `@releases.*` tags. A mismatch is a data hygiene bug
  worth flagging, the same way an accidental name collision would be. This
  is a different axis from multiple `Examples:` blocks *within one node*
  carrying deliberately *different* tags (see "Tag placement" above) --
  that's the sub-grouping mechanism working as intended, not a mismatch.
- **Reasons go in a comment on its own line directly above the tag (or the
  Examples table) they explain**, not in a fixed format -- they're for
  humans. **Never inline on the same line as a tag.** Verified against
  this repo's actual `reformat-gherkin`: it merges multiple short tag
  lines onto one line, and an inline trailing comment is silently
  *deleted* in that merge --

  ```diff
  -  @releases.skip.noble.until.2026-08-15  # noble skipped: esm down due to CVE
  -  @releases.lts.supported
  +  @releases.skip.noble.until.2026-08-15 @releases.lts.supported
  ```

  A comment on its own line above the tags survives the same merge intact
  -- confirmed with the same tool. This isn't a style preference, it's the
  difference between a reason surviving the next `reformat-gherkin` pass
  (a required pre-commit step per SKILL.md) and being silently destroyed
  by it.
- **Dates are ISO 8601**, matching every other date already in this
  codebase (`ubuntu.csv`, skip-adjacent fields elsewhere).
- **No interaction with behave's `--tags` execution filtering.** Nothing
  in CI currently references `@releases.*`, so adding these tags is inert
  for test selection. Worth a real check once these land in a file, but
  not expected to be an issue.
- **`reformat-gherkin` should run with `--multi-line-tags`** rather than
  its default single-line merge. A scenario using several fields from this
  model can end up with 3-6 `@releases.*` tags, and one-per-line reads and
  diffs far better than one long merged line -- confirmed the flag exists
  (`TagLineMode` in `reformat_gherkin/options.py`) and keeps tags one per
  line without otherwise changing behavior. This is a repo-wide
  `.pre-commit-config.yaml`/`tox.ini` configuration change (not yet made,
  affects existing tag formatting across all 72 feature files, not just
  new `@releases.*` ones) -- tracked here as a decision, separate from
  actually flipping the config. It does not change the comment-placement
  rule above; that's independent of single- vs multi-line tag mode (see
  the destructive-inline-comment finding).

## Worked translations

**Anbox** (`tracks={lts:{supported}}`, everything else defaulted):

```gherkin
  Scenario Outline: Enable Anbox cloud service in a VM
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    ...

    @releases.lts.supported
    Examples: ubuntu release
      | release | machine_type |
      | ...
```

**`fix.feature`'s lifecycle-tracked scenario** (`tracks={lts:{supported,
esm}}`):

```gherkin
  Scenario Outline: Fix command on a machine without security/updates source lists
    ...

    @releases.lts.supported
    @releases.lts.esm
    Examples: ubuntu release
      | release | machine_type |
      | ...
```

**Closed window** (hypothetical -- no longer Pro-gated after resolute):

```gherkin
  Scenario Outline: ...
    ...

    @releases.lts.supported
    @releases.until.lts.resolute
    Examples: ...
```

**Cloud-scoped** (hypothetical -- FIPS not offered on GCP):

```gherkin
  Scenario Outline: ...
    ...

    @releases.lts.supported
    @releases.machine_types:aws.pro
    @releases.machine_types:azure.pro
    Examples: ...
```

**Temporary mid-window hole** (the ESM-outage-from-a-CVE example):

```gherkin
  Scenario Outline: ...
    ...

    # noble skipped: ESM was down for months due to a CVE response;
    # revisit after 2026-08-15
    @releases.lts.supported
    @releases.lts.esm
    @releases.skip.noble.until.2026-08-15
    Examples: ...
```

**Cloud type with its own availability window** (GCP Pro only available
since focal):

```gherkin
  Scenario Outline: ...
    ...

    @releases.lts.supported
    @releases.machine_types:aws.pro
    @releases.machine_types:azure.pro
    @releases.machine_types:gcp.pro
    Examples: ...
```

No exceptions needed. `gcp.pro`'s own availability window is an
`applicable(m, r)` fact (see
[release_coverage_model.md](../explanation/release_coverage_model.md)'s
"External classification facts"), external to this scenario and never
encoded in a tag -- it already excludes xenial/bionic from `R(S)`.
Declaring the full relevant `machine_types` set is enough.

**Two Examples blocks with different testing policies** (clouds tested
only while `supported`, standard substrates also tracked through `esm` and
across the `interim` line):

```gherkin
Scenario Outline: Check pro version
  Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
  ...

  @releases.lts.supported
  @releases.lts.esm
  @releases.interim.supported
  Examples: standard
    | release | machine_type  |
    | ...     | lxd-container |

  @releases.lts.supported
  @releases.machine_types:aws.pro
  @releases.machine_types:azure.pro
  @releases.machine_types:gcp.pro
  Examples: clouds
    | release | machine_type |
    | ...     | aws.pro      |
```

Each block's tags are independent -- "standard" also tracks `esm` and
`interim`, "clouds" doesn't, reflecting a genuine difference in testing
policy between the two groups. `Scenario Outline: Check pro version`
itself carries no `@releases.*` tags -- putting any there would be a
`TAG_ERROR`.

**Explicit bound with a reason** (apt output format changed in kernel
5.5, which focal ships):

```gherkin
  Scenario Outline: ...
    ...

    # apt changed its output format starting in kernel 5.5, which focal ships
    @releases.lts.supported
    @releases.since.lts.focal
    Examples: ...
```

**Deliberately fixed** (tied to one historical CVE, never expected to
grow):

```gherkin
  Scenario Outline: ...
    ...

    @releases.fixed
    Examples: ...
```

**Unclassified** (the honest current state of most of the suite): no
`@releases.*` tags on the `Examples:` block(s) at all. Nothing to show --
that's the point.

## Open items

- Parsing and validation are implemented: `tools/release_tags.py` turns a
  set of tags into `tracks`/`since`/`until`/`machine_types`/`exceptions`
  per the vocabulary above and rejects malformed or conflicting tags
  (unknown tokens, `@releases.fixed` co-occurring with a
  `@releases.<line>.<status>` tag, an unresolvable `since`/`until`
  release). `tools/coverage_gaps.py` applies this after aggregation and
  additionally checks that every `Scenario Outline` node sharing a
  scenario name carries identical `@releases.*` tags on its `Examples:`
  block(s).
- Reading tags from `Examples:` blocks (rather than `Scenario Outline:`)
  is new to this document -- `features/behave_features.py`/
  `features/tools/coverage_gaps.py` don't implement "Tag placement" above
  yet.
- `applicable(m, r)` (referenced in the "cloud type with its own
  availability window" translation above) isn't sourced anywhere yet --
  see `dev-docs/reference/machine_type_applicability.md`.
- Migration (tagging the ~180 existing scenario behaviors) is a separate,
  bounded task -- a scenario's own historical `combos` strongly suggest its
  `tracks` value in most cases, which could seed a first pass.
