import pytest

from features.tools.release_tags import (
    Bound,
    SkipException,
    TagValidationError,
    parse_tags,
)

# Each case below mirrors a worked translation in
# dev-docs/reference/release_coverage_tags.md, so the parser stays pinned
# to that document's own examples.


class TestVocabulary:
    def test_anbox(self):
        d = parse_tags(["releases:lts_supported"])
        assert d.tracks == {"lts": {"supported"}}
        assert not d.is_unclassified

    def test_fix_feature_lifecycle_tracked(self):
        d = parse_tags(["releases:lts_supported", "releases:lts_esm"])
        assert d.tracks == {"lts": {"supported", "esm"}}

    def test_closed_window(self):
        d = parse_tags(
            ["releases:lts_supported", "releases:until:lts:resolute"]
        )
        assert d.tracks == {"lts": {"supported"}}
        assert d.until == {"lts": Bound("resolute")}
        assert d.since == {}

    def test_cloud_scoped(self):
        d = parse_tags(
            [
                "releases:lts_supported",
                "machine_types:aws.pro",
                "machine_types:azure.pro",
            ]
        )
        assert d.machine_types == {"aws.pro", "azure.pro"}

    def test_temporary_mid_window_hole(self):
        d = parse_tags(
            [
                "releases:lts_supported",
                "releases:lts_esm",
                "releases:skip:noble:until:2026-08-15",
            ]
        )
        assert d.exceptions == [SkipException("noble", None, "2026-08-15")]

    def test_gcp_added_in_focal(self):
        d = parse_tags(
            [
                "releases:lts_supported",
                "machine_types:aws.pro",
                "machine_types:azure.pro",
                "machine_types:gcp.pro",
                "releases:skip:xenial+gcp.pro",
                "releases:skip:bionic+gcp.pro",
            ]
        )
        assert d.machine_types == {"aws.pro", "azure.pro", "gcp.pro"}
        assert d.exceptions == [
            SkipException("xenial", "gcp.pro"),
            SkipException("bionic", "gcp.pro"),
        ]

    def test_explicit_bound_with_reason_ignores_the_reason(self):
        # The reason ("apt changed its output format...") lives in a
        # comment, never in the tag -- the parser only ever sees the bound.
        d = parse_tags(["releases:lts_supported", "releases:since:lts:focal"])
        assert d.since == {"lts": Bound("focal")}
        assert d.since["lts"].reason is None

    def test_deliberately_fixed(self):
        d = parse_tags(["releases:fixed"])
        assert d.tracks == {}
        assert not d.is_unclassified

    def test_unclassified_no_tags(self):
        d = parse_tags([])
        assert d.tracks is None
        assert d.is_unclassified

    def test_unrelated_namespaces_are_ignored(self):
        d = parse_tags(["uses.config.landscape", "releases:lts_supported"])
        assert d.tracks == {"lts": {"supported"}}

    def test_permanent_skip_whole_release(self):
        d = parse_tags(["releases:lts_supported", "releases:skip:noble"])
        assert d.exceptions == [SkipException("noble", None, None)]

    def test_both_lines(self):
        d = parse_tags(["releases:lts_supported", "releases:interim"])
        assert d.tracks == {"lts": {"supported"}, "interim": {"supported"}}

    def test_returns_a_fresh_declaration_instance(self):
        # No shared mutable state leaking between calls via dataclass defaults.
        first = parse_tags(["releases:lts_supported"])
        second = parse_tags([])
        assert first.tracks == {"lts": {"supported"}}
        assert second.tracks is None


class TestValidation:
    def test_fixed_conflicts_with_tracked_bucket(self):
        with pytest.raises(TagValidationError, match="cannot co-occur"):
            parse_tags(["releases:fixed", "releases:lts_supported"])

    def test_fixed_conflicts_regardless_of_order(self):
        with pytest.raises(TagValidationError, match="cannot co-occur"):
            parse_tags(["releases:lts_supported", "releases:fixed"])

    def test_unknown_line_in_since(self):
        with pytest.raises(TagValidationError, match="unknown line"):
            parse_tags(["releases:since:trusty:jammy"])

    def test_unknown_bucket(self):
        with pytest.raises(TagValidationError, match="unrecognized"):
            parse_tags(["releases:lts_bogus"])

    def test_unknown_machine_type_in_machine_types_tag(self):
        with pytest.raises(TagValidationError, match="unknown machine_type"):
            parse_tags(["machine_types:hyperv"])

    def test_unknown_machine_type_in_skip_pairing(self):
        with pytest.raises(TagValidationError, match="unknown machine_type"):
            parse_tags(["releases:skip:noble+hyperv"])

    def test_duplicate_since_for_same_line(self):
        with pytest.raises(TagValidationError, match="duplicate"):
            parse_tags(
                ["releases:since:lts:focal", "releases:since:lts:jammy"]
            )

    def test_duplicate_until_for_same_line(self):
        with pytest.raises(TagValidationError, match="duplicate"):
            parse_tags(
                ["releases:until:lts:jammy", "releases:until:lts:noble"]
            )

    def test_since_for_different_lines_is_fine(self):
        d = parse_tags(
            ["releases:since:lts:focal", "releases:since:interim:questing"]
        )
        assert d.since == {"lts": Bound("focal"), "interim": Bound("questing")}

    def test_duplicate_skip_same_release_whole(self):
        with pytest.raises(TagValidationError, match="duplicate"):
            parse_tags(["releases:skip:noble", "releases:skip:noble"])

    def test_duplicate_skip_same_release_and_machine_type(self):
        with pytest.raises(TagValidationError, match="duplicate"):
            parse_tags(
                [
                    "releases:skip:noble+aws.pro",
                    "releases:skip:noble+aws.pro",
                ]
            )

    def test_skip_same_release_different_machine_type_is_fine(self):
        d = parse_tags(
            ["releases:skip:noble+aws.pro", "releases:skip:noble+azure.pro"]
        )
        assert len(d.exceptions) == 2

    def test_skip_whole_release_and_specific_machine_type_are_distinct_keys(
        self,
    ):
        d = parse_tags(["releases:skip:noble", "releases:skip:noble+aws.pro"])
        assert len(d.exceptions) == 2

    def test_malformed_expiry_date(self):
        with pytest.raises(TagValidationError, match="ISO 8601"):
            parse_tags(["releases:skip:noble:until:not-a-date"])

    def test_unrecognized_tag_shape(self):
        with pytest.raises(TagValidationError, match="unrecognized"):
            parse_tags(["releases:garbage"])

    def test_since_missing_release(self):
        with pytest.raises(TagValidationError):
            parse_tags(["releases:since:lts:"])

    def test_too_many_segments_in_bucket_tag(self):
        with pytest.raises(TagValidationError, match="unrecognized"):
            parse_tags(["releases:lts_supported:extra"])
