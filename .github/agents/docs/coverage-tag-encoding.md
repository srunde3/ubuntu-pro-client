# Encoding the Coverage Information Model in Behave

How `coverage-information-model.md`'s fields (`tracks`, `since`/`until`,
`machine_types`, `exceptions`) get expressed in actual `.feature` files.
The model came first and is syntax-independent by design; this is where we
compromise for what Gherkin can actually carry.

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

## Encoding rules

- **One tag per fact.** No packed/structured tag values beyond the `:`
  value-escape and `+` pairing described above. Keeps parsing trivial
  (split on `.`, check against a small fixed keyword set) and keeps diffs
  small when one fact changes.
- **Consistency across aggregated nodes is required, not just
  recommended.** The precondition-split pattern (`fix.feature`'s three
  `Scenario Outline`s sharing one name) means `@releases.*` tags are a
  property of the *behavior*, not the node -- every node sharing a name
  must carry identical `@releases.*` tags. A mismatch is itself a data
  hygiene bug worth flagging, the same way an accidental name collision
  would be.
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
  @releases.lts.supported
  Scenario Outline: Enable Anbox cloud service in a VM
```

**`fix.feature`'s lifecycle-tracked scenario** (`tracks={lts:{supported,
esm}}`):

```gherkin
  @releases.lts.supported
  @releases.lts.esm
  Scenario Outline: Fix command on a machine without security/updates source lists
```

**Closed window** (hypothetical -- no longer Pro-gated after resolute):

```gherkin
  @releases.lts.supported
  @releases.until.lts.resolute
  Scenario Outline: ...
```

**Cloud-scoped** (hypothetical -- FIPS not offered on GCP):

```gherkin
  @releases.lts.supported
  @releases.machine_types:aws.pro
  @releases.machine_types:azure.pro
  Scenario Outline: ...
```

**Temporary mid-window hole** (the ESM-outage-from-a-CVE example):

```gherkin
  @releases.lts.supported
  @releases.lts.esm
  @releases.skip.noble.until.2026-08-15
  Scenario Outline: ...
    # noble skipped: ESM was down for months due to a CVE response;
    # revisit after 2026-08-15
```

**Machine_type introduced partway through the window** (GCP added in
focal):

```gherkin
  @releases.lts.supported
  @releases.machine_types:aws.pro
  @releases.machine_types:azure.pro
  @releases.machine_types:gcp.pro
  @releases.skip.xenial+gcp.pro
  @releases.skip.bionic+gcp.pro
  Scenario Outline: ...
    # gcp.pro added starting in focal; xenial/bionic never supported it
```

**Explicit bound with a reason** (apt output format changed in kernel
5.5, which focal ships):

```gherkin
  @releases.lts.supported
  @releases.since.lts.focal
  Scenario Outline: ...
    # apt changed its output format starting in kernel 5.5, which focal ships
```

**Deliberately fixed** (tied to one historical CVE, never expected to
grow):

```gherkin
  @releases.fixed
  Scenario Outline: ...
```

**Unclassified** (the honest current state of most of the suite): no
`@releases.*` tags at all. Nothing to show -- that's the point.

## Open items

- `coverage_gaps.py` needs a tag-parsing layer that turns
  `ScenarioSummary.tags` into `tracks`/`since`/`until`/`machine_types`/
  `exceptions` per the vocabulary above, applied *after* the existing
  aggregation-by-name step (aggregation still happens on raw
  `describe_feature` output; the consistency check across aggregated
  nodes' tags happens once, on the merged record).
- No validation yet for malformed or conflicting tags (e.g. both
  `@releases.fixed` and `@releases.lts.supported` on the same scenario,
  or mismatched tags across aggregated nodes). Worth a lint pass before
  this is relied on, not designed here.
- Migration (tagging the ~180 existing scenario behaviors) is a separate,
  bounded task -- see the auto-suggestion idea from the evaluator
  discussion (a scenario's own historical `combos` strongly suggest its
  `tracks` value in most cases).
