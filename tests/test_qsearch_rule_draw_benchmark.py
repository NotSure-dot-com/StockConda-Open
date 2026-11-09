import pytest

from scripts.qsearch_rule_draw_benchmark import git_identity, summarize_pairs


def test_benchmark_summary_uses_paired_ratios_and_fixed_median_gate():
    summary = summarize_pairs(
        reference=[2.0, 4.0, 5.0],
        candidate=[2.04, 3.96, 5.0],
        max_ratio=1.03,
        max_relative_mad=0.03,
    )

    assert summary == {
        "reference_seconds": [2.0, 4.0, 5.0],
        "candidate_seconds": [2.04, 3.96, 5.0],
        "paired_ratios": pytest.approx([1.02, 0.99, 1.0]),
        "median_paired_ratio": 1.0,
        "relative_mad": pytest.approx(0.01),
        "max_ratio": 1.03,
        "max_relative_mad": 0.03,
        "status": "PASS",
    }


def test_benchmark_summary_rejects_ratio_above_gate():
    summary = summarize_pairs(
        reference=[1.0, 1.0, 1.0],
        candidate=[1.031, 1.04, 1.02],
        max_ratio=1.03,
        max_relative_mad=0.03,
    )

    assert summary["median_paired_ratio"] == 1.031
    assert summary["status"] == "HOLD"


def test_benchmark_summary_rejects_unstable_pairs_below_ratio_gate():
    summary = summarize_pairs(
        reference=[1.0, 1.0, 1.0, 1.0, 1.0],
        candidate=[0.90, 0.95, 1.0, 1.05, 1.10],
        max_ratio=1.03,
        max_relative_mad=0.03,
    )

    assert summary["median_paired_ratio"] == 1.0
    assert summary["relative_mad"] == pytest.approx(0.05)
    assert summary["status"] == "HOLD"


@pytest.mark.parametrize(
    ("reference", "candidate"),
    [([], []), ([0.0], [1.0]), ([1.0], [1.0, 1.0])],
)
def test_benchmark_summary_rejects_invalid_samples(reference, candidate):
    with pytest.raises(ValueError):
        summarize_pairs(
            reference,
            candidate,
            max_ratio=1.03,
            max_relative_mad=0.03,
        )


def test_git_identity_retains_declared_revision_without_repository(tmp_path):
    identity = git_identity(tmp_path, "341de6f")

    assert identity == {
        "declared_revision": "341de6f",
        "git_available": False,
        "git_revision": None,
        "git_status": None,
    }
