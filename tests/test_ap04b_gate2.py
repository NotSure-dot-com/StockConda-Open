"""Decision-contract tests for the AP-04B Gate-2 runner."""

from __future__ import annotations

import hashlib
from importlib import util
from pathlib import Path
import tarfile
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def runner() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "ap04b_gate2.py"
    if not path.exists():
        pytest.fail("AP-04B Gate-2 runner is not implemented")
    spec = util.spec_from_file_location("ap04b_gate2", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semantic_comparison_rejects_any_nested_difference() -> None:
    """Catches accepting a bestmove-equal but trace-different candidate."""

    module = runner()
    baseline = {"score": 12, "bestmove": "e2e4", "trace": [1, {"i": 2}]}
    module.compare_semantics(baseline, baseline)
    with pytest.raises(ValueError, match="semantic parity mismatch"):
        module.compare_semantics(
            baseline,
            {"score": 12, "bestmove": "e2e4", "trace": [1, {"i": 3}]},
        )


@pytest.mark.parametrize(
    "scenario,counts",
    [
        ("tt", {"captures": 0, "killers": 0, "other": 0}),
        ("capture", {"captures": 1, "killers": 0, "other": 0}),
        ("killer", {"captures": 1, "killers": 1, "other": 0}),
    ],
)
def test_laziness_counter_accepts_only_no_later_stage_work(
    scenario: str,
    counts: dict[str, int],
) -> None:
    """Catches a validator that does not enforce each cutoff boundary."""

    runner().validate_laziness(scenario, counts)


def test_laziness_counter_negative_control_rejects_eager_other_stage() -> None:
    """Catches accepting the exact eager-Other mutation Gate 2 targets."""

    with pytest.raises(ValueError, match="later stage work"):
        runner().validate_laziness(
            "killer",
            {"captures": 1, "killers": 1, "other": 1},
        )


def test_output_directory_must_be_new(tmp_path: Path) -> None:
    """Catches overwriting or resuming an existing evidence directory."""

    module = runner()
    output = tmp_path / "fresh"
    module.new_output(output)
    assert output.is_dir()
    with pytest.raises(FileExistsError):
        module.new_output(output)


def test_counter_result_requires_all_three_controlled_scenarios() -> None:
    """Catches a counter gate that silently omits one cutoff boundary."""

    module = runner()
    valid = {
        "tt": {"captures": 0, "killers": 0, "other": 0},
        "capture": {"captures": 1, "killers": 0, "other": 0},
        "killer": {"captures": 1, "killers": 1, "other": 0},
    }
    module.validate_counter_results(valid)
    with pytest.raises(ValueError, match="counter scenarios"):
        module.validate_counter_results({"tt": valid["tt"]})


def test_gate2_runner_must_match_frozen_candidate_source() -> None:
    module = runner()
    manifest: dict[str, Any] = {
        "runner_sha256": "runner-sha",
        "runtime_policy_sha256": "policy-sha",
        "engines": {
            "candidate": {
                "source": {
                    "scripts/ap04b_gate2.py": "different-sha",
                    "scripts/ap04b_runtime_policy.py": "policy-sha",
                }
            }
        },
    }

    with pytest.raises(RuntimeError, match="runner is not the frozen candidate"):
        module.validate_runner_binding(manifest)

    manifest["engines"]["candidate"]["source"][
        "scripts/ap04b_gate2.py"
    ] = "runner-sha"
    module.validate_runner_binding(manifest)

    manifest["runtime_policy_sha256"] = "different-policy"
    with pytest.raises(RuntimeError, match="runtime policy"):
        module.validate_runner_binding(manifest)


def test_worker_identity_binds_python_chess_and_numpy_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runner()
    root = Path(__file__).parents[1]
    chess_identity = {
        "root": "/bound/chess",
        "version": "fixture",
        "files": {"__init__.py": "a" * 64},
    }
    numpy_identity = {"root": "/bound/numpy", "version": "2.1.0"}
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "imported_package_identity",
        lambda name: chess_identity if name == "chess" else pytest.fail(name),
        raising=False,
    )
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "imported_package_location_identity",
        lambda name: numpy_identity if name == "numpy" else pytest.fail(name),
        raising=False,
    )
    monkeypatch.setattr(module.RUNTIME_POLICY, "runtime_identity", lambda: {})
    monkeypatch.setattr(module.RUNTIME_POLICY, "validate_runtime_identity", lambda _: None)
    monkeypatch.setattr(module, "sha", lambda _: "digest")

    def fake_import(name: str) -> SimpleNamespace:
        if name == "chess":
            return SimpleNamespace(__version__="fixture")
        suffix = ".so" if ".cython_ext." in name else ".py"
        path = root / "src" / Path(*name.split("."))
        return SimpleNamespace(__file__=str(path.with_suffix(suffix)))

    monkeypatch.setattr(module.importlib, "import_module", fake_import)

    identity = module.imported_identity(root)

    assert identity["packages"] == {
        "chess": chess_identity,
        "numpy": numpy_identity,
    }


def test_identity_worker_runs_without_site_and_loads_bound_native_modules() -> None:
    """Catches Gate 2 reverting to a site-started worker process."""

    module = runner()
    root = Path(__file__).parents[1]
    identity = module.invoke(root, {"mode": "identity"})

    assert identity["runtime"]["flags"]["no_site"] == 1
    assert identity["runtime"]["startup_modules"] == {}
    assert identity["packages"]["chess"]["files"]
    assert all(
        value["path"].endswith(".so")
        for name, value in identity["imported"].items()
        if ".cython_ext." in name
    )


def test_archive_members_must_match_the_build_root(tmp_path: Path) -> None:
    """Catches independently valid archive/root identities that do not match."""

    module = runner()
    root = tmp_path / "build"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    archive = tmp_path / "engine.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="src/engine.py")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    assert module.verify_archive(root, archive, digest) == {
        "src/engine.py": hashlib.sha256(source.read_bytes()).hexdigest()
    }
    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="archive/source mismatch"):
        module.verify_archive(root, archive, digest)

    source.unlink()
    with pytest.raises(RuntimeError, match="archive/source mismatch"):
        module.verify_archive(root, archive, digest)


def test_archive_rejects_additional_runtime_source_in_build_root(
    tmp_path: Path,
) -> None:
    """Catches a post-freeze source injected through the engine PYTHONPATH."""

    module = runner()
    root = tmp_path / "build"
    source = root / "src" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    archive = tmp_path / "engine.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="src/engine.py")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    module.verify_archive(root, archive, digest)

    (root / "src" / "sitecustomize.py").write_text(
        "INJECTED = True\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="unexpected build-root source"):
        module.verify_archive(root, archive, digest)
