# Release coverage tags reference

How the [release coverage model](../explanation/release_coverage_model.md)'s
fields (`tracks`, `since`/`until`, `machine_types`, `exceptions`) are
expressed as `@releases.*` tags in `.feature` files. `features/tools/
release_tags.py` implements this vocabulary; keep it in sync with this
document when either changes.

Tags carry every fact `Missing(S)` is computed from. `reason` text
(Gherkin tags can't contain whitespace) lives in a comment on its own line
directly above the tag(s) or `Examples:` table it explains -- never inline
on the same line as a tag: `reformat-gherkin` merges multi-line tags onto
one line and silently deletes an inline trailing comment in that merge.

## Delimiters

- `:` -- keyword, then a literal value. Machine_type names contain their
  own dots (`aws.pro`, `gcp.pro-fips`), so `:` separates the tag's keyword
  path from the raw value to avoid it being misread as further namespacing.
- `+` -- pairs two identifiers into one compound key, used only where an
  exception needs both a release *and* a specific machine_type.

## Vocabulary

Namespace: `@releases.*`, alongside the repo's existing `@uses.config.*`
convention. `<line>` is `lts` or `interim`; `<status>` is `supported`,
`esm`, or `legacy`; `<release>` is a series codename; `<date>` is ISO 8601.

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

`machine_type` values and their own release-availability windows are
governed by `applicable(m, r)`, sourced from
`features/tools/machine_types.yaml` (see the file's own header comment
for its schema, and
[the how-to guide](../how-to/maintain_feature_test_coverage.md) for
keeping it current).

`UNCLASSIFIED(S)` = neither any `@releases.<line>.<status>` tag nor
`@releases.fixed` is present. `@releases.fixed` and
`@releases.<line>.<status>` are mutually exclusive.

## Tag placement

`@releases.*` tags go above an `Examples:` block, never above `Scenario
Outline:` -- a tag on `Scenario Outline:` is a `TAG_ERROR`. A scenario
with one `Examples:` block tags that one block; a scenario whose coverage
splits along a policy boundary the release axis alone can't express (e.g.
"clouds tested only on LTS, standard substrates on every release") tags
each block independently, each block's tag set complete on its own with
no inheritance between blocks. A block with no `@releases.*` tags is
`UNCLASSIFIED`, not an error.

## Encoding rules

- One tag per fact -- no packed/structured values beyond the `:` and `+`
  uses above.
- When one scenario name spans multiple `Scenario Outline` nodes (the
  precondition-split pattern), every node's `Examples:` block(s) must
  carry identical `@releases.*` tags -- a mismatch is `TAG_ERROR`. This
  doesn't apply to multiple `Examples:` blocks *within* one node
  deliberately carrying different tags (see "Tag placement" above).
- `reformat-gherkin` should run with `--multi-line-tags` (`TagLineMode` in
  `reformat_gherkin/options.py`) so a scenario using several
  `@releases.*` tags reads and diffs one-per-line; not yet configured
  repo-wide, so expect multi-tag scenarios to get merged onto one line
  until it is.
- Adding `@releases.*` tags doesn't interact with behave's `--tags`
  execution filtering -- nothing in CI currently selects on them.

## Examples

**Ordinary LTS tracking**:

```gherkin
@releases.lts.supported
Examples: ubuntu release
  | release | machine_type |
  | ...
```

**Lifecycle-tracked** (`esm` too):

```gherkin
@releases.lts.supported
@releases.lts.esm
Examples: ubuntu release
  | release | machine_type |
  | ...
```

**Closed window**:

```gherkin
@releases.lts.supported
@releases.until.lts.resolute
Examples: ...
```

**Explicit bound with a reason**:

```gherkin
# apt changed its output format starting in kernel 5.5, which focal ships
@releases.lts.supported
@releases.since.lts.focal
Examples: ...
```

**Cloud-scoped**:

```gherkin
@releases.lts.supported
@releases.machine_types:aws.pro
@releases.machine_types:azure.pro
@releases.machine_types:gcp.pro
Examples: ...
```

**Temporary mid-window hole**:

```gherkin
# noble skipped: ESM was down for months due to a CVE response;
# revisit after 2026-08-15
@releases.lts.supported
@releases.lts.esm
@releases.skip.noble.until.2026-08-15
Examples: ...
```

**Two `Examples:` blocks, different testing policies**:

```gherkin
Scenario Outline: Check pro version
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

**Deliberately fixed**:

```gherkin
@releases.fixed
Examples: ...
```

**Unclassified**: no `@releases.*` tags on the `Examples:` block(s) at all.
