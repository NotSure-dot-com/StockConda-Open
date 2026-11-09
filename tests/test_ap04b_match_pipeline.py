"""Resume-admission tests for the AP-04B match pipeline."""

from __future__ import annotations

import hashlib
from importlib import util
import json
from pathlib import Path
import platform
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest


def pipeline() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "ap04b_match_pipeline.py"
    spec = util.spec_from_file_location("ap04b_match_pipeline", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ModuleType:
    module = pipeline()
    assert module.__file__ is not None
    module_path = Path(module.__file__)

    controlled_runtime = {
        "python": sys.version,
        "executable": sys.executable,
        "executable_sha256": hashlib.sha256(
            Path(sys.executable).read_bytes()
        ).hexdigest(),
        "flags": {
            "optimize": 0,
            "dont_write_bytecode": True,
            "no_user_site": 1,
            "no_site": 1,
            "isolated": 0,
            "ignore_environment": 0,
            "safe_path": True,
        },
        "environment": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONOPTIMIZE": "0",
            "PYTHONSAFEPATH": "1",
        },
        "startup_modules": {},
    }
    monkeypatch.setattr(
        module.RUNTIME_POLICY, "runtime_identity", lambda: controlled_runtime
    )
    monkeypatch.setattr(
        module.RUNTIME_POLICY, "verify_imported_package", lambda *_args: None
    )
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "verify_imported_package_location",
        lambda *_args: None,
        raising=False,
    )

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    engines: dict[str, Any] = {}
    launcher_hashes: dict[str, str] = {}
    for label in ("candidate", "baseline"):
        root = tmp_path / label
        source = root / "src" / "engine.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
        native = root / "src" / "chess_engine" / "cython_ext"
        native.mkdir(parents=True)
        for stem in ("cython_chess", "evaluation", "transposition"):
            (native / f"{stem}.cpython-312-x86_64-linux-gnu.so").write_bytes(
                stem.encode("ascii")
            )
        frozen_sources = {"src/engine.py": digest(source)}
        if label == "candidate":
            runner_copy = root / "scripts" / "ap04b_match_pipeline.py"
            runner_copy.parent.mkdir()
            runner_copy.write_bytes(module_path.read_bytes())
            frozen_sources["scripts/ap04b_match_pipeline.py"] = digest(
                runner_copy
            )
            policy_copy = root / "scripts" / "ap04b_runtime_policy.py"
            policy_copy.write_bytes(
                module_path.with_name("ap04b_runtime_policy.py").read_bytes()
            )
            frozen_sources["scripts/ap04b_runtime_policy.py"] = digest(
                policy_copy
            )
        archive = root / "archive.tar"
        archive.write_bytes(b"fixture archive")
        launcher = root / "launch.sh"
        launcher.write_text(
            "#!/bin/sh\n"
            "unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH\n"
            "export PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 "
            "PYTHONNOUSERSITE=1 PYTHONOPTIMIZE=0 PYTHONSAFEPATH=1\n"
            f"export PYTHONPATH={root}/src:/bound\n"
            f"exec {sys.executable} -S -s -B -P -u -m chess_engine.stockconda\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(module, label.upper() + "_LAUNCHER", launcher)
        launcher_hashes[label] = digest(launcher)
        engines[label] = {
            "root": str(root),
            "archive": str(archive),
            "archive_sha256": digest(archive),
            "commit": label + "-revision",
            "source": frozen_sources,
            "executables": module.RUNTIME_POLICY.inventory_executables(root),
            "identity": {
                "python": sys.version,
                "platform": platform.platform(),
                "runtime": controlled_runtime,
                "executable": sys.executable,
                "packages": {
                    "chess": {
                        "root": "/bound/chess",
                        "version": "fixture",
                        "files": {"__init__.py": "a" * 64},
                    },
                    "numpy": {
                        "root": "/bound/numpy",
                        "version": "2.1.0",
                    },
                },
                "imported": {
                    "engine": {"path": str(source), "sha256": digest(source)}
                },
            },
        }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "engines": engines,
            "match_runner_sha256": digest(module_path),
            "runtime_policy_sha256": digest(
                module_path.with_name("ap04b_runtime_policy.py")
            ),
        }),
        encoding="utf-8",
    )
    performance = tmp_path / "performance.json"
    performance.write_text(
        json.dumps(
            {
                "status": "PASS",
                "manifest_sha256": digest(manifest),
                "raw_sha256": "raw-fixture",
                "preflight_sha256": "preflight-fixture",
                "samples": 1200,
                "aggregate_median": 0.95,
            }
        ),
        encoding="utf-8",
    )
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "audit": "PASS",
                "status": "PASS",
                "manifest_sha256": digest(manifest),
                "raw_sha256": "raw-fixture",
                "preflight_sha256": "preflight-fixture",
                "samples": 1200,
                "aggregate_median": 0.95,
            }
        ),
        encoding="utf-8",
    )
    for name, path in (
        ("MANIFEST", manifest),
        ("PERFORMANCE", performance),
        ("PERFORMANCE_AUDIT", audit),
    ):
        monkeypatch.setattr(module, name, path)
        monkeypatch.setattr(module, "EXPECTED_" + name, digest(path))
    monkeypatch.setattr(module, "EXPECTED_LAUNCHER_SHA256", launcher_hashes)
    for attribute, filename, auditor_source in (
        (
            "MATCH_AUDITOR",
            "ap03_match_audit.py",
            b"def audit_match(*args, **kwargs):\n    return {'status': 'PASS'}\n",
        ),
        ("UCI_AUDITOR", "ap03_uci_audit.py", b"def audit(events):\n    pass\n"),
    ):
        path = tmp_path / filename
        path.write_bytes(auditor_source)
        monkeypatch.setattr(module, attribute, path)
        monkeypatch.setattr(module, "EXPECTED_" + attribute, digest(path))
    for attribute in ("FASTCHESS", "BOOK"):
        path = tmp_path / attribute.lower()
        path.write_bytes(b"fixture")
        monkeypatch.setattr(module, attribute, path)
        monkeypatch.setattr(module, "EXPECTED_" + attribute, digest(path))
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "engines": [
                    {"limit": {"tc": {}}},
                    {"limit": {"tc": {}}},
                ],
                "stats": {},
                "pgn": {},
                "log": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "TEMPLATE", template)
    monkeypatch.setattr(module, "EXPECTED_TEMPLATE", digest(template))
    monkeypatch.setattr(module, "RESULTS", tmp_path / "results")
    module.RESULTS.mkdir()
    return module


def rewrite_manifest_evidence(module: ModuleType, manifest: dict[str, Any]) -> None:
    module.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    expected_manifest = hashlib.sha256(module.MANIFEST.read_bytes()).hexdigest()
    setattr(module, "EXPECTED_MANIFEST", expected_manifest)
    for evidence in (module.PERFORMANCE, module.PERFORMANCE_AUDIT):
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["manifest_sha256"] = expected_manifest
        evidence.write_text(json.dumps(value), encoding="utf-8")
    setattr(
        module,
        "EXPECTED_PERFORMANCE",
        hashlib.sha256(module.PERFORMANCE.read_bytes()).hexdigest(),
    )
    setattr(
        module,
        "EXPECTED_PERFORMANCE_AUDIT",
        hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest(),
    )


def inject_extra_source(module: ModuleType) -> None:
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    root = Path(manifest["engines"]["candidate"]["root"])
    (root / "src" / "sitecustomize.py").write_text(
        "INJECTED = True\n", encoding="utf-8"
    )


def test_compile_auditor_preserves_assertions_when_parent_is_optimized() -> None:
    path = Path(__file__).parents[1] / "scripts" / "ap04b_match_pipeline.py"
    code = (
        "import importlib.util,pathlib; "
        f"p=pathlib.Path({str(path)!r}); "
        "s=importlib.util.spec_from_file_location('pipeline',p); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "ns={}; exec(m.compile_auditor(b'def reject():\\n assert False\\n',p),ns); "
        "\ntry: ns['reject']()\nexcept AssertionError: print('ASSERTED')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-O", "-c", code],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ASSERTED"


def test_match_runtime_rejects_optimized_or_uncontrolled_interpreter() -> None:
    module = pipeline()
    with pytest.raises(RuntimeError, match="runtime controls"):
        module.validate_match_runtime({
            "flags": {
                "optimize": 1,
                "dont_write_bytecode": False,
                "no_user_site": 0,
            },
            "environment": {},
        })


def test_external_admission_binds_fresh_gate3_and_match_paths(
    tmp_path: Path,
) -> None:
    module = pipeline()
    paths: dict[str, Path] = {}
    for name in (
        "manifest", "performance", "performance_audit",
        "candidate_launcher", "baseline_launcher", "template",
    ):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        paths[name] = path
    admission = {
        name: {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, path in paths.items()
    }
    admission["results"] = {"path": str(tmp_path / "results")}
    admission["stage_audit_sha256"] = {}
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_sha = hashlib.sha256(admission_path.read_bytes()).hexdigest()

    module.configure_admission(admission_path, admission_sha)

    assert module.MANIFEST == paths["manifest"]
    assert module.EXPECTED_MANIFEST == admission["manifest"]["sha256"]
    assert module.RESULTS == tmp_path / "results"
    assert module.TEMPLATE == paths["template"]
    assert module.EXPECTED_TEMPLATE == admission["template"]["sha256"]
    assert module.EXPECTED_STAGE_AUDIT_SHA256 == {}
    with pytest.raises(RuntimeError, match="admission identity"):
        module.configure_admission(admission_path, "0" * 64)


def test_identity_rejects_changed_match_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    module.TEMPLATE.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="template identity"):
        module.verify_identity()


def test_identity_requires_launchers_to_clear_loader_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    for label in ("candidate", "baseline"):
        launcher = getattr(module, label.upper() + "_LAUNCHER")
        lines = launcher.read_text(encoding="utf-8").splitlines()
        lines.remove("unset LD_PRELOAD LD_AUDIT LD_LIBRARY_PATH")
        launcher.write_text("\n".join(lines) + "\n", encoding="utf-8")
        module.EXPECTED_LAUNCHER_SHA256[label] = hashlib.sha256(
            launcher.read_bytes()
        ).hexdigest()

    with pytest.raises(RuntimeError, match="launcher does not match"):
        module.verify_identity()


def test_identity_verifies_bound_python_chess_and_numpy_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    frozen_chess = {
        "root": "/bound/chess",
        "version": "fixture",
        "files": {"__init__.py": "a" * 64},
    }
    frozen_numpy = {"root": "/bound/numpy", "version": "2.1.0"}
    for entry in manifest["engines"].values():
        entry["identity"]["packages"] = {
            "chess": frozen_chess,
            "numpy": frozen_numpy,
        }
    module.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    expected_manifest = hashlib.sha256(module.MANIFEST.read_bytes()).hexdigest()
    setattr(module, "EXPECTED_MANIFEST", expected_manifest)
    for evidence in (module.PERFORMANCE, module.PERFORMANCE_AUDIT):
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["manifest_sha256"] = expected_manifest
        evidence.write_text(json.dumps(value), encoding="utf-8")
    setattr(
        module,
        "EXPECTED_PERFORMANCE",
        hashlib.sha256(module.PERFORMANCE.read_bytes()).hexdigest(),
    )
    setattr(
        module,
        "EXPECTED_PERFORMANCE_AUDIT",
        hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest(),
    )
    verified: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "verify_imported_package",
        lambda name, identity: verified.append((name, identity)),
        raising=False,
    )
    verified_locations: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        module.RUNTIME_POLICY,
        "verify_imported_package_location",
        lambda name, identity: verified_locations.append((name, identity)),
        raising=False,
    )

    module.verify_identity()

    assert verified == [
        ("chess", frozen_chess),
        ("chess", frozen_chess),
    ]
    assert verified_locations == [
        ("numpy", frozen_numpy),
        ("numpy", frozen_numpy),
    ]


@pytest.mark.parametrize(
    "mutation",
    ["python", "platform", "runtime", "executable-content"],
)
def test_identity_rejects_frozen_interpreter_or_runtime_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    identity = manifest["engines"]["candidate"]["identity"]
    if mutation == "python":
        identity["python"] = "different-python"
    elif mutation == "platform":
        identity["platform"] = "different-platform"
    elif mutation == "runtime":
        identity["runtime"]["environment"]["PYTHONHASHSEED"] = "7"
    else:
        identity["runtime"]["executable_sha256"] = "0" * 64
    rewrite_manifest_evidence(module, manifest)

    with pytest.raises(RuntimeError, match="interpreter identity"):
        module.verify_identity()


def test_identity_rejects_match_runner_not_admitted_by_gate3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    assert module.__file__ is not None
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    root = Path(manifest["engines"]["candidate"]["root"])
    frozen_runner = root / "scripts" / "ap04b_match_pipeline.py"
    frozen_runner.parent.mkdir(exist_ok=True)
    frozen_runner.write_bytes(Path(module.__file__).read_bytes())
    manifest["engines"]["candidate"]["source"][
        "scripts/ap04b_match_pipeline.py"
    ] = hashlib.sha256(frozen_runner.read_bytes()).hexdigest()
    manifest["match_runner_sha256"] = "0" * 64
    module.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    expected_manifest = hashlib.sha256(module.MANIFEST.read_bytes()).hexdigest()
    setattr(
        module,
        "EXPECTED_MANIFEST",
        expected_manifest,
    )
    for evidence in (module.PERFORMANCE, module.PERFORMANCE_AUDIT):
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["manifest_sha256"] = expected_manifest
        evidence.write_text(json.dumps(value), encoding="utf-8")
    setattr(
        module,
        "EXPECTED_PERFORMANCE",
        hashlib.sha256(module.PERFORMANCE.read_bytes()).hexdigest(),
    )
    setattr(
        module,
        "EXPECTED_PERFORMANCE_AUDIT",
        hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest(),
    )

    with pytest.raises(RuntimeError, match="match runner identity"):
        module.verify_identity()


def test_identity_rejects_runtime_policy_not_admitted_by_gate3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    manifest = json.loads(module.MANIFEST.read_text(encoding="utf-8"))
    manifest["runtime_policy_sha256"] = "0" * 64
    module.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    expected_manifest = hashlib.sha256(module.MANIFEST.read_bytes()).hexdigest()
    setattr(module, "EXPECTED_MANIFEST", expected_manifest)
    for evidence in (module.PERFORMANCE, module.PERFORMANCE_AUDIT):
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["manifest_sha256"] = expected_manifest
        evidence.write_text(json.dumps(value), encoding="utf-8")
    setattr(
        module,
        "EXPECTED_PERFORMANCE",
        hashlib.sha256(module.PERFORMANCE.read_bytes()).hexdigest(),
    )
    setattr(
        module,
        "EXPECTED_PERFORMANCE_AUDIT",
        hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest(),
    )

    with pytest.raises(RuntimeError, match="runtime policy"):
        module.verify_identity()


def test_identity_rejects_additional_source_after_gate3_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    module.verify_identity()
    inject_extra_source(module)

    with pytest.raises(RuntimeError, match="unexpected engine source"):
        module.verify_identity()


def test_run_stage_rejects_additional_source_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    inject_extra_source(module)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "match process started before source identity validation"
        ),
    )

    with pytest.raises(RuntimeError, match="unexpected engine source"):
        module.run_stage("preflight", 4, 1, 600, False)


def test_resume_validation_rejects_additional_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    inject_extra_source(module)

    with pytest.raises(RuntimeError, match="unexpected engine source"):
        module.validate_skipped_stages("preflight")


def configure_identity(
    module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "RESULTS", tmp_path)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "engines": [
                    {"limit": {"tc": {}}},
                    {"limit": {"tc": {}}},
                ],
                "stats": {},
                "pgn": {},
                "log": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "TEMPLATE", template)
    assert module.__file__ is not None
    runner_sha256 = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    manifest_data = {
        "match_runner_sha256": runner_sha256,
        "engines": {
            "candidate": {"commit": "candidate-revision"},
            "baseline": {"commit": "baseline-revision"},
        },
    }
    for attribute, content in (
        ("MANIFEST", json.dumps(manifest_data)),
        ("PERFORMANCE", "performance fixture"),
        ("PERFORMANCE_AUDIT", "performance audit fixture"),
        ("CANDIDATE_LAUNCHER", "candidate fixture"),
        ("BASELINE_LAUNCHER", "baseline fixture"),
    ):
        path = tmp_path / (attribute.lower() + ".txt")
        path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(module, attribute, path)
        if attribute in {"MANIFEST", "PERFORMANCE", "PERFORMANCE_AUDIT"}:
            monkeypatch.setattr(
                module, "EXPECTED_" + attribute, hashlib.sha256(path.read_bytes()).hexdigest()
            )
    monkeypatch.setattr(
        module,
        "EXPECTED_LAUNCHER_SHA256",
        {
            label: hashlib.sha256(getattr(module, label.upper() + "_LAUNCHER").read_bytes()).hexdigest()
            for label in ("candidate", "baseline")
        },
    )
    monkeypatch.setattr(module, "EXPECTED_STAGE_AUDIT_SHA256", {})
    controlled_runtime = {
        "flags": {
            "optimize": 0,
            "dont_write_bytecode": True,
            "no_user_site": 1,
            "no_site": 1,
            "safe_path": True,
        },
        "environment": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONOPTIMIZE": "0",
            "PYTHONSAFEPATH": "1",
        },
        "startup_modules": {},
    }
    monkeypatch.setattr(
        module.RUNTIME_POLICY, "runtime_identity", lambda: controlled_runtime
    )
    monkeypatch.setattr(
        module,
        "verify_identity",
        lambda: manifest_data,
    )


def write_stage_evidence(
    module: ModuleType,
    stage: str,
    games: int,
    concurrency: int,
    *,
    audit_status: str = "PASS",
    manifest_sha256: str | None = None,
) -> None:
    output = module.RESULTS / ("ap04_" + stage + "_1")
    output.mkdir()
    stage_index = next(
        index for index, item in enumerate(module.STAGES) if item[0] == stage
    )
    predecessor_audit: dict[str, str] | None = None
    if stage_index:
        predecessor = module.STAGES[stage_index - 1][0]
        predecessor_path = (
            module.RESULTS / ("ap04_" + predecessor + "_1") / "audit.json"
        )
        predecessor_audit = {
            "stage": predecessor,
            "sha256": hashlib.sha256(predecessor_path.read_bytes()).hexdigest(),
        }
    run = {
        "status": "NEEDS_AUDIT",
        "stage": stage,
        "games": games,
        "concurrency": concurrency,
        "exit_code": 0,
        "manifest_sha256": manifest_sha256 or module.EXPECTED_MANIFEST,
        "performance_sha256": module.EXPECTED_PERFORMANCE,
        "performance_audit_sha256": module.EXPECTED_PERFORMANCE_AUDIT,
        "auditor_sha256": module.EXPECTED_MATCH_AUDITOR,
        "uci_auditor_sha256": module.EXPECTED_UCI_AUDITOR,
        "runner_sha256": module.verify_identity()["match_runner_sha256"],
        "runtime": module.RUNTIME_POLICY.runtime_identity(),
        "launcher_sha256": {
            module.NAMES[0]: module.EXPECTED_LAUNCHER_SHA256["candidate"],
            module.NAMES[1]: module.EXPECTED_LAUNCHER_SHA256["baseline"],
        },
        "engine_commits": {
            module.NAMES[0]: "candidate-revision",
            module.NAMES[1]: "baseline-revision",
        },
        "predecessor_audit": predecessor_audit,
    }
    run_path = output / "run.json"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    hashes = {"run.json": hashlib.sha256(run_path.read_bytes()).hexdigest()}
    for name in (
        "protocol.log",
        "games.pgn",
        "console.log",
        "autosave.json",
        "input.json",
    ):
        path = output / name
        path.write_text(name, encoding="utf-8")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    audit = {"status": audit_status, "games": games, "sha256": hashes}
    audit_path = output / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    module.EXPECTED_STAGE_AUDIT_SHA256[stage] = hashlib.sha256(
        audit_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("defect", ["missing", "failed", "wrong-identity"])
def test_resume_rejects_unadmitted_skipped_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    if defect != "missing":
        write_stage_evidence(
            module,
            "preflight",
            4,
            1,
            audit_status="FAIL" if defect == "failed" else "PASS",
            manifest_sha256="wrong" if defect == "wrong-identity" else None,
        )
    with pytest.raises(RuntimeError, match="skipped preflight"):
        module.validate_skipped_stages("orientation")


def test_resume_accepts_identity_bound_passed_skipped_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    write_stage_evidence(module, "preflight", 4, 1)
    write_stage_evidence(module, "orientation", 200, 4)

    module.validate_skipped_stages("release")


@pytest.mark.parametrize("defect", ["missing", "changed"])
def test_resume_rejects_missing_or_changed_predecessor_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    write_stage_evidence(module, "preflight", 4, 1)
    write_stage_evidence(module, "orientation", 200, 4)
    output = tmp_path / "ap04_orientation_1"
    run_path = output / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if defect == "missing":
        run.pop("predecessor_audit")
    else:
        run["predecessor_audit"]["sha256"] = "0" * 64
    run_path.write_text(json.dumps(run), encoding="utf-8")
    audit_path = output / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["sha256"]["run.json"] = hashlib.sha256(
        run_path.read_bytes()
    ).hexdigest()
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    module.EXPECTED_STAGE_AUDIT_SHA256["orientation"] = hashlib.sha256(
        audit_path.read_bytes()
    ).hexdigest()

    with pytest.raises(RuntimeError, match="skipped orientation"):
        module.validate_skipped_stages("release")


@pytest.mark.parametrize("defect", ["missing", "changed", "failed"])
def test_run_stage_rejects_predecessor_before_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    if defect != "missing":
        write_stage_evidence(
            module,
            "preflight",
            4,
            1,
            audit_status="FAIL" if defect == "failed" else "PASS",
        )
        if defect == "changed":
            (tmp_path / "ap04_preflight_1" / "games.pgn").write_text(
                "changed", encoding="utf-8"
            )
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "match process started before predecessor validation"
        ),
    )

    with pytest.raises(RuntimeError, match="skipped preflight"):
        module.run_stage("orientation", 200, 4, 3600, False)


def test_fresh_sequence_binds_predecessor_audit_before_next_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "STAGES",
        (
            ("preflight", 4, 1, 600, False),
            ("orientation", 200, 4, 3600, False),
        ),
    )
    monkeypatch.setattr(sys, "argv", ["pipeline"])

    class CompletedProcess:
        pid = 12345

        def wait(self, timeout: int) -> int:
            del timeout
            return 0

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: CompletedProcess(),
    )

    def controlled_audit(
        output: Path,
        games: int,
        concurrency: int,
        elo: bool,
    ) -> dict[str, Any]:
        del concurrency, elo
        for name in ("protocol.log", "games.pgn", "autosave.json"):
            (output / name).write_text(name, encoding="utf-8")
        hashes = {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in (
                "run.json",
                "protocol.log",
                "games.pgn",
                "console.log",
                "autosave.json",
                "input.json",
            )
        }
        audit = {"status": "PASS", "games": games, "sha256": hashes}
        (output / "audit.json").write_text(
            json.dumps(audit), encoding="utf-8"
        )
        return audit

    monkeypatch.setattr(module, "audit_match", controlled_audit)

    assert module.main() == 0
    preflight_audit = tmp_path / "ap04_preflight_1" / "audit.json"
    orientation_run = json.loads(
        (tmp_path / "ap04_orientation_1" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert orientation_run["predecessor_audit"] == {
        "stage": "preflight",
        "sha256": hashlib.sha256(preflight_audit.read_bytes()).hexdigest(),
    }


def test_resume_rejects_skipped_stage_without_admitted_audit_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    write_stage_evidence(module, "preflight", 4, 1)
    module.EXPECTED_STAGE_AUDIT_SHA256.clear()

    with pytest.raises(RuntimeError, match="skipped preflight"):
        module.validate_skipped_stages("orientation")


def test_resume_rejects_artifact_changed_after_pass_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pipeline()
    configure_identity(module, tmp_path, monkeypatch)
    write_stage_evidence(module, "preflight", 4, 1)
    (tmp_path / "ap04_preflight_1" / "games.pgn").write_text(
        "changed", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="audited artifact changed"):
        module.validate_skipped_stages("orientation")


def test_main_validates_skipped_stages_before_starting_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = pipeline()
    monkeypatch.setattr(sys, "argv", ["pipeline", "--start", "orientation"])
    monkeypatch.setattr(
        module,
        "validate_skipped_stages",
        lambda _stage: (_ for _ in ()).throw(RuntimeError("resume rejected")),
    )
    monkeypatch.setattr(
        module,
        "run_stage",
        lambda *_args: pytest.fail("process started before resume validation"),
    )

    with pytest.raises(RuntimeError, match="resume rejected"):
        module.main()
