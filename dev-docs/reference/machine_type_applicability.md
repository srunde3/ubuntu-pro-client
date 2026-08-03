# Machine type applicability reference

Sources `applicable(m, r)` -- the classification
[release_coverage_model.md](../explanation/release_coverage_model.md)'s
"External classification facts" treats as given: whether machine_type `m`
was, or is, actually offered as a real product or environment for release
`r`. That doc defines the model's need for this fact without saying how
it's produced; this doc is that "later, separate concern."

## Why this needs its own source

For releases, `status(r)`/`line(r)` come from `distro-info`'s `ubuntu.csv`
-- an externally maintained, authoritative file. There is no equivalent
upstream source for machine_type availability: whether `gcp.pro` existed
on `bionic`, or when `wsl` support ended, is Ubuntu Pro product history
that lives nowhere in a structured, queryable form yet. This reference
defines the shape that history takes once captured; populating it with
real dates is a separate, later step.

## Schema

One entry per machine_type, except `lxd-container`/`lxd-vm`, which get
none -- their availability is fully captured by the release catalog
already (a release either exists or it doesn't; there's no separate
substrate-specific window). Every other value in `ALLOWED_MACHINE_TYPES`
-- the cloud `*.generic`/`*.pro` types, the `*.pro-fips` types, and `wsl`
-- gets a real entry:

```
machine_type -> (since: Release | None, until: Release | None)
```

- `since=None` -- available from the start (no lower bound).
- `until=None` -- still available today (no upper bound).
- Both bounds are release series names (e.g. `focal`), interpreted the
  same inclusive way `since(S, line)`/`until(S, line)` already are in the
  coverage model: `since <= r <= until-or-now`.

One entry per literal machine_type string, not a cross-cutting "capability"
dimension (e.g. FIPS modeled once and combined with cloud provider) -- the
same atomic unit `Combo`, `exceptions`, and `ALLOWED_MACHINE_TYPES` already
use, so nothing downstream needs a second way to identify a machine_type.

## Current state

Every non-`lxd` entry exists with `since=None, until=None` -- present, but
unbounded, which is a deliberate placeholder, not a guess. Real dates need
to come from whoever has the actual Ubuntu Pro product-availability
history. Until they're filled in, `applicable(m, r)` is `True` for every
declared machine_type at every release -- an inert default, not a guess,
so nothing regresses while the data gets populated.
