# Release coverage model

TODO tidy this document up and remove introduced syntax like S, r. Use natural human language instead of a mathematical formalism.

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
ships"). Both default to unbounded if unstated: neither is ever inferred
from `S`'s current coverage, since a bound derived that way would move
whenever coverage changes and so could never detect a deleted release. A
`reason` is optional on both, worth attaching whenever a bound is
*explicitly* narrowed, since that's exactly the kind of fact a future
reader needs explained rather than mistaken for an oversight. This mirrors
`exceptions`' `reason` field, so all three "why doesn't this apply here"
mechanisms -- `since`, `until`, `exceptions` -- share the same shape. The
reason is carried as metadata only; it plays no part in computing `R(S)`.

**`machine_types(S)`** -- the set of machine_types `S` applies to.
Defaults to whatever machine_types are already covered anywhere in `S` if
unstated (see Limitation F). Each declared machine_type is further
constrained by `applicable(m, r)` -- see "External classification facts"
below.

**`exceptions(S)`** -- a set of `(release, machine_type?, reason,
expires?)` records: deliberate holes within an otherwise-applicable
(release, machine_type) space. `machine_type` omitted = the whole release.
`expires` omitted = permanent. Once `expires` passes, the exception lapses
and the pair becomes a live gap again.

### No field ever defaults from `S`'s own current coverage

`tracks` has no safe default -- guessing it is the exact circularity this
model exists to avoid, and any default (empty, full, or inferred from
existing coverage) would either suppress the most valuable check or
silently degrade over time as releases age between statuses (a scenario
that happens to cover a release which later ages into ESM would start
being checked against ESM with no decision ever having been made). So
absence is its own visible state.

`since`/`until` unstated means unbounded -- the more inclusive direction,
deliberately, even though that costs an occasional noisy finding (an
ancient release nobody's thought about in years suddenly needs a decision).

`machine_types` is the one field that still defaults from `S`'s current
coverage (whatever's covered anywhere in `S`), because there is no
equivalent "unbounded" alternative for it the way there is for releases --
most scenarios are legitimately scoped to a specific subset of
machine_types (a GCP-specific behavior shouldn't default to being checked
against every cloud). This carries a structural risk: deleting every row
of one machine_type from a scenario silently narrows what's required
instead of flagging the deletion (see Limitation F). Declaring the full
relevant set explicitly avoids this risk entirely, and is cheap to do now
that `applicable(m, r)` prunes it down to what was actually available,
without needing exceptions to narrow it by hand.

## External classification facts

Two facts about the world feed this model. Both are treated as given --
computed by something outside this model, evaluated as of today -- and
both are genuinely the same *kind* of fact: a classification that changes
as a release ages.

- **`status(r)`** -- a release's current phase: `devel`, `supported`,
  `esm`, `legacy`, or `eol`. Time-varying: the same release moves through
  these phases over its life. (`line(r)`, used alongside it throughout
  this doc, is different in kind -- permanent, not a phase: `lts`/`interim`
  never changes for a release once it exists.)
- **`applicable(m, r)`** -- whether machine_type `m` was, or is, actually
  offered as a real product or environment for release `r`. The same kind
  of fact as `status(r)`: a machine_type can be not-yet-applicable,
  applicable, or (for some) retired, depending on `r`.

This model only needs these as classifications -- how they're actually
produced is a separate, later concern, deliberately out of scope here. For
`status`/`line`, this repo happens to source them from `distro-info`'s
`ubuntu.csv`; that's a sourcing choice, not part of the model itself -- the
model works identically if that classification came from somewhere else.
`applicable` needs a source of the same kind; see
[machine_type_applicability.md](../reference/machine_type_applicability.md)
for its shape.

## Derivation

```
R_line(S, L) for a line L present in tracks(S):
    statuses = tracks(S)[L]
    return { r : line(r) == L, status(r) in statuses,
             since(S,L) <= r <= until(S,L)-or-now }

R(S)        = { (r, m) : r in union over L in tracks(S) of R_line(S, L),
                m in machine_types(S), applicable(m, r) }
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
   only resolute is missing.

2. **Bounded-forward product feature** (`anbox.feature`). `tracks =
   {lts: {supported}}`, `since` unstated (unbounded). `R(S)` = every
   currently-`supported` LTS release = `{jammy, noble, resolute}` --
   bionic/focal/xenial never enter `R(S)` regardless of the unbounded
   `since`, because none of them is currently `supported` (`esm` was never
   declared as a tracked status here). Covered = `{jammy, noble}`. Missing
   = `{resolute}`.

3. **Lifecycle-tracked security feature** (`fix.feature`'s "Fix command on
   a machine without security/updates source lists"). `tracks =
   {lts: {supported, esm}}`, covers only bionic today. `R(S)` = every LTS
   release currently `supported` or `esm` = `{bionic, focal, jammy, noble,
   resolute}` (xenial is currently `legacy`, not `supported`/`esm`, so it
   doesn't enter `R(S)` here either). Missing = `{focal, jammy, noble,
   resolute}` -- **not just focal**: every currently-`supported`-or-`esm`
   LTS release is checked, not only the newest.

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

12. **Machine_type introduced or retired partway through the window** (e.g.
    `gcp.pro` only available since focal; `gcp.pro-fips` only available
    through focal). Not a scenario-level concern -- declare the full
    relevant `machine_types(S) = {aws.pro, azure.pro, gcp.pro}`;
    `applicable(gcp.pro, r)` is false for xenial/bionic, so `R(S)`
    excludes them automatically. A retired type like `gcp.pro-fips`, where
    `applicable` goes false again after focal, is excluded the same way
    once it ages out -- no exception list to maintain as new releases
    ship.

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

**F. Detecting a deleted machine_type.** Unlike `since`/`until`,
`machine_types(S)` still defaults from `S`'s own current coverage (see "No
field ever defaults from `S`'s own current coverage" above) because there's
no meaningful "unbounded" machine_types default. If every row of one
machine_type is removed from a scenario, the default silently narrows along
with it -- the deletion never surfaces as a gap. Declaring `machine_types`
explicitly closes this for a given scenario; nothing currently protects a
scenario that leaves it unstated.
