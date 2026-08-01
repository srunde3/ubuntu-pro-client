# Release coverage model

This defines what facts must be knowable about a behave scenario to
deterministically compute "which (release, machine_type) pairs should this
cover, and which of those are missing." The model is syntax-independent;
see [release_coverage_tags.md](../reference/release_coverage_tags.md) for
how these facts are encoded as Gherkin tags.

## Fields, per scenario `S`

**`tracks(S)`** -- a mapping from release line (`lts`, `interim`) to the
set of status tiers (`supported`, `esm`, `legacy`) tracked on that line.
This is the single field that answers "which buckets of releases must
currently be represented" -- there is no separate mechanism for "stay
current with the newest release" versus "don't lose coverage as a release
ages into ESM/Legacy." Both are the same computation: is every release
whose current (line, status) matches a declared bucket actually covered?
A line simply absent from the mapping means that line isn't tracked at
all; a status tier absent from a line's declared set means that tier is
explicitly not this scenario's concern, not "not gotten to yet."

Examples:

```
Anbox (bounded product feature):  { lts: {supported} }
ESM-focused scenario:             { lts: {supported, esm} }
Basic CLI command:                { lts: {supported}, interim: {supported} }
```

Three distinct states, not two:

- a non-empty mapping -- tracks those buckets
- an explicitly declared empty mapping (`{}`) -- deliberately fixed, never
  tracks anything forward (a real, positive statement)
- **undeclared** -- unclassified. Distinct from both; must surface as its
  own output state, never silently resolved either way.

**`since(S, line)`**, **`until(S, line)`** -- per line present in
`tracks(S)`, the earliest and latest release that line's bucket
membership is evaluated over, each optionally paired with a `reason` (e.g.
"apt changed its output format starting in kernel 5.5, which focal
ships"). `since` defaults to the earliest release already covered on that
line if unstated -- no reason needed, the boundary is self-evident from
existing data. `until` defaults to unbounded if unstated. A `reason` is
optional on both, but worth attaching whenever a bound is *explicitly*
narrowed, since that's exactly the kind of fact a future reader needs
explained rather than mistaken for an oversight. This mirrors
`exceptions`' `reason` field, so all three "why doesn't this apply here"
mechanisms -- `since`, `until`, `exceptions` -- share the same shape. The
reason is carried as metadata only; it plays no part in computing `R(S)`.

**`machine_types(S)`** -- the set of machine_types `S` applies to.
Defaults to whatever machine_types are already covered anywhere in `S` if
unstated. Not windowed by release (see Limitation A).

**`exceptions(S)`** -- a set of `(release, machine_type?, reason,
expires?)` records: deliberate holes within an otherwise-applicable
(release, machine_type) space. `machine_type` omitted = the whole release.
`expires` omitted = permanent. Once `expires` passes, the exception lapses
and the pair becomes a live gap again.

### Default asymmetry, deliberately

`tracks` has no safe default -- guessing it is the exact circularity this
model exists to avoid, and any default (empty, full, or inferred from
existing coverage) would either suppress the most valuable check or
silently degrade over time as releases age between statuses (a scenario
that happens to cover a release which later ages into ESM would start
being checked against ESM with no decision ever having been made). So
absence is its own visible state.
`since`/`until` and `machine_types` default towards *not* flagging when
unstated, because a missed flag is far cheaper than the false-positive
floods both "assume broadly" defaults produced empirically.

## Derivation

```
R_line(S, L) for a line L present in tracks(S):
    statuses = tracks(S)[L]
    return { r : line(r) == L, status(r) in statuses,
             since(S,L) <= r <= until(S,L)-or-now }

R(S)        = { (r, m) : r in union over L in tracks(S) of R_line(S, L),
                m in machine_types(S) }
Excepted(S) = { (r, m) : an unexpired exception covers (r, m) or (r, None) }

Missing(S) = R(S) - Covered(S) - Excepted(S)
```

If `tracks(S)` is undeclared, output is `UNCLASSIFIED(S)`, not `Missing(S)
= {}` -- there is no `R(S)` to compute.

## Worked examples

1. **Ordinary open-ended LTS tracking** (most of the suite, e.g.
   `cli/enable.feature`'s "Running pro enable --auto"). `tracks =
   {lts: {supported}}`. `R(S)` = every LTS release *currently* in standard
   support -- today that's jammy, noble, *and* resolute simultaneously
   (LTS support windows overlap across the 2-year release cadence), not
   just the newest one. This scenario already covers jammy and noble, so
   in practice only resolute is missing -- but the mechanism now checks
   all three, not just the newest, which matters whenever a scenario has
   a hole further back (see #3).

2. **Bounded-forward product feature** (`anbox.feature`). `tracks =
   {lts: {supported}}`, `since` defaulted to jammy (its earliest existing
   row). `R(S)` = currently-supported LTS releases from jammy onward =
   `{jammy, noble, resolute}`. Covered = `{jammy, noble}`. Missing =
   `{resolute}`. Bionic/focal/xenial never enter `R(S)` -- `esm` was never
   declared as a tracked status for this scenario.

3. **Lifecycle-tracked security feature** (`fix.feature`'s "Fix command on
   a machine without security/updates source lists"). `tracks =
   {lts: {supported, esm}}`, covers only bionic today. `R(S)` = LTS
   releases currently `supported` or `esm`, from bionic onward =
   `{bionic, focal, jammy, noble, resolute}`. Missing = `{focal, jammy,
   noble, resolute}` -- **not just focal**. This is the concrete case
   where the old newest-only mechanism was wrong: it only ever checked
   this scenario against the single newest LTS (resolute) via one check
   and against the ESM set via a separate check, and never noticed jammy
   and noble were missing too, because "check the newest" silently skipped
   the releases in between.

4. **Closed window** (hypothetical: a feature stops being Pro-gated after
   resolute). `tracks = {lts: {supported}}`, `until(S, lts) = resolute`.
   Produces the same `R(S)` as #1 today, since resolute is already the
   newest supported LTS -- the bound only starts mattering once a release
   newer than resolute enters `supported` status, at which point it's
   excluded rather than silently expected.

5. **Cloud-scoped feature** (hypothetical: FIPS not offered on GCP).
   `tracks = {lts: {supported}}`, `machine_types = {aws.pro, azure.pro}`
   declared explicitly. `gcp.pro` is never a candidate in `R(S)`.

6. **Temporary mid-window hole** (the ESM-outage-from-a-CVE example).
   `exceptions = {(noble, None, "esm down due to CVE", expires=2026-08-15)}`.
   Noble excluded from `Missing(S)` until that date, then reappears
   automatically.

7. **Permanent mid-window hole** (rare -- most permanent boundaries are
   better expressed as `since`/`until`, but a genuinely one-off internal
   anomaly is possible). Same as #6 with `expires` omitted. Never
   re-flagged.

8. **Interim-only tracking** (e.g. a scenario specifically about non-LTS
   behavior, like `daemon.feature`'s "daemon does not start on gcp,azure
   generic non lts"). `tracks = {interim: {supported}}`. `R(S)` = the
   interim release currently in standard support, which today is *none*
   (a real gap in the release calendar between questing's expiry and
   stonking's release) -- correctly produces no findings right now, not
   an error.

9. **Both lines** (`_version.feature`'s "Check pro version"). `tracks =
   {lts: {supported}, interim: {supported}}`. `R(S)` = currently-supported
   LTS releases union currently-supported interim releases, independently
   evaluated per line.

10. **Deliberately fixed** (hypothetical: a scenario tied to one specific
    historical CVE, never expected to grow). `tracks = {}`, explicitly
    declared. `R(S) = {}` forever -- distinguishable in the source from
    "nobody's classified this yet."

11. **Unclassified** (the honest current state of most of the suite before
    any migration). No `tracks` declared. Output: `UNCLASSIFIED(S)`, a
    finding of its own, not a guess in either direction.

12. **Machine_type introduced partway through the window** (e.g. "AWS and
    Azure since xenial, GCP only since focal"). Not a separate mechanism --
    declare the full eventual `machine_types(S) = {aws.*, azure.*, gcp.*}`,
    then add one permanent exception (`expires` omitted) per
    `(release, gcp.*, "not added to GCP until focal")` for each release
    before focal. This is cheap specifically because those exceptions are
    for *closed* history: a fixed, small, one-time list that never needs
    revisiting, not something that grows going forward. Once focal ships,
    real rows satisfy `R(S)` with no further exceptions needed.

13. **Explicit bound with a reason** (apt's output format changed starting
    in kernel 5.5, which focal ships). `tracks = {lts: {supported}}`,
    `since(S, lts) = (focal, "apt changed its output format starting in
    kernel 5.5, which focal ships")`. `R(S)` is bounded exactly as an
    unreasoned `since=focal` would be -- the reason changes nothing about
    what's computed, it just means the next person reading the file
    understands why bionic/xenial were never in scope, instead of it
    looking like an unexplained oversight.

## What this model explicitly cannot handle

**A. Recurring/periodic holes** (e.g. "only tested every other release").
`exceptions` are discrete, individually-dated records, not a repeating
pattern.

**B. Applicability conditioned on external state** (e.g. "applies to
jammy, but only when landscape credentials are configured"). The model
only reasons about release x machine_type identity. `requires_config` is
visible in the underlying data but not integrated into this model.

**C. Multiple required variants within one (release, machine_type) pair.**
Several real scenarios carry extra Examples columns beyond
release/machine_type (`package_name`, `cis_script`, `landscape`, etc.) --
this model treats any row for a given `(release, machine_type)` as fully
covering it, and has no notion of "this release needs 3 parameter variants
to be considered complete." A release with one thin row and a release with
three thorough ones look identical here.

**D. Cross-scenario or whole-file constraints** (e.g. "don't add resolute
to this file until questing is dropped from that one"). Every scenario's
applicability is evaluated independently; there's no dependency graph
between them.

**E. Whether a scenario should exist at all.** This model only evaluates
completeness of *existing* scenarios across releases/machine_types. It has
nothing to say about missing behaviors -- product surface with no test
scenario at all is invisible to it, by construction. This is a
release/machine_type completeness tool, not a test-coverage-in-the-general-
sense tool.
