# Machine type applicability reference

Certain machine types only have certain releases available.

TODO: identify if this document is necessary at all, or if the releases file is self-documenting.

## Schema

One entry per `ALLOWED_MACHINE_TYPES` value, each mapping to the complete,
explicit list of release series it was, or is, offered on:

```yaml
machine_type:
  - release_series
  - release_series
  ...
```

No ranges, no implicit "still open" bound -- every applicable release is
spelled out by name, so the file itself is a literal, auditable fact table
("`jammy` exists on `aws.pro-fips`") rather than a rule to evaluate. The
cost of that: when a new Ubuntu series ships, it has to be added by hand to
every still-applicable machine_type's list, or that series reads as not
applicable by omission. That's a small, expected part of the twice-yearly
release process, in exactly one file.

A machine_type with no entry is treated as unbounded (applicable to every
release) by the lookup logic -- but every value in `ALLOWED_MACHINE_TYPES`
should have a real entry; that fallback exists only for machine types not
yet added there.

One entry per literal machine_type string, not a cross-cutting "capability"
dimension (e.g. FIPS modeled once and combined with cloud provider) -- the
same atomic unit `Combo`, `exceptions`, and `ALLOWED_MACHINE_TYPES` already
use, so nothing downstream needs a second way to identify a machine_type.

## Current state

Implemented in `features/tools/machine_type_applicability.py` (lookup logic
only, read by `features/tools/coverage_gaps.py`'s `compute_r`). The actual
data lives in its own file, `features/tools/machine_type_applicability.yaml`
-- kept separate so updating it as real product-availability history gets
filled in never requires touching the lookup logic. `lxd-container`/
`lxd-vm` and the cloud `*.generic`/`*.pro` types currently list every
release in the catalog's relevant window, since nothing is known to have
ever excluded them.

Populated so far, from this repo's own Examples tables (the only source of
record available -- see "Why this needs its own source"):

| machine_type | releases | basis |
| --- | --- | --- |
| `aws.pro-fips` | `xenial`, `bionic`, `focal` | offered xenial-focal; not offered jammy+ today, may resume on a future release |
| `azure.pro-fips` | `xenial`, `bionic`, `focal` | same as `aws.pro-fips` |
| `gcp.pro-fips` | `bionic`, `focal` | never offered on xenial; otherwise same as above |
| `wsl` | `bionic`, `focal`, `jammy` | not offered before bionic; not offered noble+ |

The three `.pro-fips` lists are evaluated as of today, the same way
`status(r)` is -- not a permanent historical fact. If FIPS cloud images
resume on some future release, the list gets that release added, not a
per-scenario exception.
