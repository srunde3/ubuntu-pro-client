# Release coverage tags reference

How the [release coverage model](../explanation/release_coverage_model.md)'s
fields (`tracks`, `since`/`until`, `machine_types`, `exceptions`) are
expressed as `@releases:*`/`@machine_types:*` tags in `.feature` files.
`features/tools/release_tags.py` implements this vocabulary; keep it in
sync with this document when either changes.

Tags carry every fact `Missing(S)` is computed from. `reason` text
(Gherkin tags can't contain whitespace) lives in a comment on its own line
directly above the tag(s) or `Examples:` table it explains. Note that comments
cannot be on the same line as a tag: `reformat-gherkin` silently deletes an
inline trailing comment on a tag line.

## Delimiters

Tags are always `:`-delimited, never `.`-nested -- every segment after the
namespace is a single `:`-separated token.

- `:` -- separates every segment: namespace, keyword, and value. Machine
  type names contain their own dots (`aws.pro`, `gcp.pro-fips`), so `:`
  keeps those from being misread as further namespacing.
- `_` -- joins a bucket's `<line>` and `<status>` into one flat token
  (`lts_supported`), since there are few enough (line, status) combos to
  not need their own nesting.
- `+` -- pairs two identifiers into one compound key, used only where an
  exception needs both a release *and* a specific machine_type.

## Vocabulary

Two namespaces, alongside the repo's existing `@uses.config.*`
convention: `@releases:*` and `@machine_types:*` (its own top-level
namespace, not nested under `releases`). `<line>` is `lts` or `interim`;
`<release>` is a series codename; `<date>` is ISO 8601.

| Tag | Meaning |
| --- | --- |
| `@releases:lts_supported` | tracks `lts` in standard support |
| `@releases:lts_esm` | tracks `lts` in ESM |
| `@releases:lts_legacy` | tracks `lts` in legacy support |
| `@releases:interim` | tracks `interim` (one status only, so no suffix) |
| `@releases:fixed` | `tracks(S) = {}` explicitly -- deliberately tracks nothing, forever |
| `@releases:since:<line>:<release>` | lower bound for `line` |
| `@releases:until:<line>:<release>` | upper bound for `line` |
| `@machine_types:<machine_type>` | one machine_type in the applicable set; repeat per type |
| `@releases:skip:<release>` | permanent exception, whole release |
| `@releases:skip:<release>:until:<date>` | temporary exception, whole release, expires `<date>` |
| `@releases:skip:<release>+<machine_type>` | permanent exception, one (release, machine_type) pair |
| `@releases:skip:<release>+<machine_type>:until:<date>` | temporary exception, one pair, expires `<date>` |

`machine_type` values and their own release-availability windows are
governed by `applicable(m, r)`, sourced from
`features/machine_types.yaml` (see the file's own header comment
for its schema, and
[the how-to guide](../how-to/maintain_feature_test_coverage.md) for
keeping it current).

`UNCLASSIFIED(S)` = neither any tracked-bucket tag nor `@releases:fixed`
is present. `@releases:fixed` and a tracked-bucket tag are mutually
exclusive.

## Tag placement

`@releases:*`/`@machine_types:*` tags go above an `Examples:` block, never
above `Scenario Outline:` -- a tag on `Scenario Outline:` is a
`TAG_ERROR`. A scenario with one `Examples:` block tags that one block; a
scenario whose coverage splits along a policy boundary the release axis
alone can't express (e.g. "clouds tested only on LTS, standard substrates
on every release") tags each block independently, each block's tag set
complete on its own with no inheritance between blocks. A block with no
`@releases:*` tags is `UNCLASSIFIED`, not an error.

## Encoding rules

- One tag per fact -- no packed/structured values beyond the `:` and `+`
  uses above.
- When one scenario name spans multiple `Scenario Outline` nodes (the
  precondition-split pattern), every node's `Examples:` block(s) must
  carry identical `@releases:*`/`@machine_types:*` tags -- a mismatch is
  `TAG_ERROR`. This doesn't apply to multiple `Examples:` blocks *within*
  one node deliberately carrying different tags (see "Tag placement"
  above).
- Adding these tags doesn't interact with behave's `--tags` execution
  filtering -- nothing in CI currently selects on them.

## Examples

**Ordinary LTS tracking**:

```gherkin
@releases:lts_supported
Examples: ubuntu release
  | release | machine_type |
  | ...
```

**Lifecycle-tracked** (`esm` too):

```gherkin
@releases:lts_supported
@releases:lts_esm
Examples: ubuntu release
  | release | machine_type |
  | ...
```

**Closed window**:

```gherkin
@releases:lts_supported
@releases:until:lts:resolute
Examples: ...
```

**Explicit bound with a reason**:

```gherkin
# apt changed its output format starting in kernel 5.5, which focal ships
@releases:lts_supported
@releases:since:lts:focal
Examples: ...
```

**Cloud-scoped**:

```gherkin
@releases:lts_supported
@machine_types:aws.pro
@machine_types:azure.pro
@machine_types:gcp.pro
Examples: ...
```

**Temporary mid-window hole**:

```gherkin
# noble skipped: ESM was down for months due to a CVE response;
# revisit after 2026-08-15
@releases:lts_supported
@releases:lts_esm
@releases:skip:noble:until:2026-08-15
Examples: ...
```

**Two `Examples:` blocks, different testing policies**:

```gherkin
Scenario Outline: Check pro version
  ...

  @releases:lts_supported
  @releases:lts_esm
  @releases:interim
  Examples: standard
    | release | machine_type  |
    | ...     | lxd-container |

  @releases:lts_supported
  @machine_types:aws.pro
  @machine_types:azure.pro
  @machine_types:gcp.pro
  Examples: clouds
    | release | machine_type |
    | ...     | aws.pro      |
```

**Deliberately fixed**:

```gherkin
@releases:fixed
Examples: ...
```

**Unclassified**: no `@releases:*` tags on the `Examples:` block(s) at all.
