"""Discover feature files and fingerprint them for staleness checks."""

from pathlib import Path


def discover_feature_files(repo_root: Path) -> list[str]:
    """Return repo-relative paths of every ``*.feature`` file, sorted."""
    features_dir = repo_root / "features"
    if not features_dir.exists():
        return []
    return sorted(
        str(path.relative_to(repo_root)).replace("\\", "/")
        for path in features_dir.rglob("*.feature")
    )


def source_fingerprint(repo_root: Path, feature_files: list[str]) -> str:
    """A cheap staleness signal for a set of feature files.

    Each file's path and mtime, joined -- not a content hash, but enough to
    detect edits, additions, and removals, which is all
    ``TestCatalog.open_or_build`` needs to decide whether to rebuild.
    """
    parts = []
    for relative_path in feature_files:
        mtime = (repo_root / relative_path).stat().st_mtime
        parts.append(f"{relative_path}:{mtime}")
    return "|".join(parts)
