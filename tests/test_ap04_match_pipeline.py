from importlib import util
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest


def pipeline() -> Any:
    path = Path(__file__).parents[1] / "scripts/ap04_match_pipeline.py"
    assert path.exists(), "AP04 match pipeline has not been implemented"
    spec = util.spec_from_file_location("ap04_match_pipeline", path)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template() -> dict[str, Any]:
    engine = {
        "name": "old", "cmd": "/old", "options": [["Book", "false"], ["Hash", "16"]],
        "limit": {"tc": {"time": 1, "increment": 2, "fixed_time": 0,
                           "moves": 0, "timemargin": 0}},
    }
    return {"engines": [dict(engine), dict(engine)], "stats": {"old": {}},
            "pgn": {"file": "/old/games.pgn"}, "log": {"file": "/old/protocol.log"}}


def test_input_config_binds_engines_and_exact_integer_clocks(tmp_path: Path) -> None:
    module = pipeline()
    config = module.input_config(template(), tmp_path, "10+0.1")

    assert config["stats"] == {}
    assert [engine["name"] for engine in config["engines"]] == module.NAMES
    assert [engine["cmd"] for engine in config["engines"]] == [
        str(module.CANDIDATE_LAUNCHER), str(module.BASELINE_LAUNCHER)]
    assert all(engine["limit"]["tc"] == {
        "time": 10000, "increment": 100, "fixed_time": 0,
        "moves": 0, "timemargin": 0,
    } for engine in config["engines"])
    assert config["pgn"]["file"] == str(tmp_path / "games.pgn")
    assert config["log"]["file"] == str(tmp_path / "protocol.log")


def test_command_uses_paired_openings_without_decimal_clock_override(tmp_path: Path) -> None:
    command = pipeline().match_command(tmp_path, games=200, concurrency=4)

    assert command[command.index("-rounds") + 1] == "100"
    assert command[command.index("-concurrency") + 1] == "4"
    assert "-repeat" in command and "-strict" in command
    assert "tc=10+0.1" not in command
    assert "file=" + str(tmp_path / "input.json") in command


def test_stage_plan_is_fixed() -> None:
    assert pipeline().STAGES == (
        ("preflight", 4, 1, 600, False),
        ("orientation", 200, 4, 3600, False),
        ("release", 2000, 4, 43200, True),
    )


def test_resume_starts_at_named_stage_without_reusing_prior_games() -> None:
    module = pipeline()
    assert module.stages_from("orientation") == module.STAGES[1:]
    assert module.stages_from("release") == module.STAGES[2:]


def identity_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Real files and digests; replace external locations, not gate behavior."""
    module = pipeline()

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    entries: dict[str, Any] = {}
    launcher_hashes = {}
    for label in ("candidate", "baseline"):
        root = tmp_path / label
        (root / "src").mkdir(parents=True)
        source = root / "src/engine.py"
        source.write_text("pass\n")
        archive = root / "archive.tar"
        archive.write_bytes(b"fixture archive")
        launcher = root / "launch.sh"
        launcher.write_text(
            f"#!/bin/sh\nexport PYTHONPATH={root}/src\n"
            "exec /usr/bin/python -u -m chess_engine.stockconda\n"
        )
        monkeypatch.setattr(module, label.upper() + "_LAUNCHER", launcher)
        launcher_hashes[label] = digest(launcher)
        entries[label] = {
            "root": str(root), "archive": str(archive), "archive_sha256": digest(archive),
            "commit": label + "-revision", "source": {"src/engine.py": digest(source)},
            "identity": {
                "executable": "/usr/bin/python",
                "imported": {"engine": {"path": str(source), "sha256": digest(source)}},
            },
        }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"engines": entries}))
    performance = tmp_path / "performance.json"
    performance.write_text(json.dumps({
        "status": "PASS", "manifest_sha256": digest(manifest),
        "raw_sha256": "raw-fixture", "parity_sha256": "parity-fixture",
    }))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "audit": "PASS", "performance_verdict": "PASS",
        "result_sha256": digest(performance), "raw_sha256": "raw-fixture",
        "parity_sha256": "parity-fixture",
        "engine_identities": {label: entry["identity"] for label, entry in entries.items()},
    }))
    for name, path in (("MANIFEST", manifest), ("PERFORMANCE", performance),
                       ("PERFORMANCE_AUDIT", audit)):
        monkeypatch.setattr(module, name, path)
        monkeypatch.setattr(module, "EXPECTED_" + name, digest(path), raising=False)
    monkeypatch.setattr(module, "EXPECTED_LAUNCHER_SHA256", launcher_hashes, raising=False)
    match_auditor = tmp_path / "ap03_match_audit.py"
    match_auditor.write_bytes(
        b"def audit_match(*args, **kwargs):\n    return {'status': 'PASS'}\n"
    )
    monkeypatch.setattr(module, "MATCH_AUDITOR", match_auditor, raising=False)
    monkeypatch.setattr(module, "EXPECTED_MATCH_AUDITOR", digest(match_auditor), raising=False)
    uci_auditor = tmp_path / "ap03_uci_audit.py"
    uci_auditor.write_bytes(b"def audit(events):\n    pass\n")
    monkeypatch.setattr(module, "UCI_AUDITOR", uci_auditor, raising=False)
    monkeypatch.setattr(module, "EXPECTED_UCI_AUDITOR", digest(uci_auditor), raising=False)
    for name in ("FASTCHESS", "BOOK"):
        path = tmp_path / name.lower()
        path.write_bytes(b"external fixture")
        monkeypatch.setattr(module, name, path)
        monkeypatch.setattr(module, "EXPECTED_" + name, digest(path))
    monkeypatch.setattr(module, "RESULTS", tmp_path)
    config = tmp_path / "template.json"
    config.write_text(json.dumps(template()))
    monkeypatch.setattr(module, "TEMPLATE", config)
    return module


def test_identity_accepts_bound_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    assert set(module.verify_identity()["engines"]) == {"candidate", "baseline"}


@pytest.mark.parametrize("artifact", [
    "CANDIDATE_LAUNCHER", "BASELINE_LAUNCHER", "PERFORMANCE_AUDIT", "MATCH_AUDITOR", "UCI_AUDITOR",
])
def test_identity_rejects_replaced_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    path = getattr(module, artifact)
    replacement = '{"audit":"PASS"}' if artifact == "PERFORMANCE_AUDIT" else "#!/bin/sh\nexit 0\n"
    path.write_text(replacement)
    with pytest.raises(RuntimeError, match="identity|launcher"):
        module.verify_identity()


@pytest.mark.parametrize("label,wrong", [
    ("candidate", "root"), ("baseline", "root"),
    ("candidate", "interpreter"), ("baseline", "interpreter"),
])
def test_identity_binds_pinned_launcher_to_manifest_root_and_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, wrong: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    path = getattr(module, label.upper() + "_LAUNCHER")
    text = path.read_text()
    if wrong == "root":
        text = text.replace(str(tmp_path / label), "/wrong-source")
    else:
        text = text.replace("/usr/bin/python", "/wrong-python")
    path.write_text(text)
    # Even an explicitly re-pinned launcher must agree with the manifest.
    module.EXPECTED_LAUNCHER_SHA256[label] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="launcher"):
        module.verify_identity()


@pytest.mark.parametrize("field", ["result_sha256", "raw_sha256", "parity_sha256", "engine_identities"])
def test_identity_rejects_pinned_audit_for_different_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    audit = json.loads(module.PERFORMANCE_AUDIT.read_text())
    audit[field] = {} if field == "engine_identities" else "wrong-evidence"
    module.PERFORMANCE_AUDIT.write_text(json.dumps(audit))
    module.EXPECTED_PERFORMANCE_AUDIT = hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="audit"):
        module.verify_identity()


@pytest.mark.parametrize("artifact", ["CANDIDATE_LAUNCHER", "BASELINE_LAUNCHER", "PERFORMANCE_AUDIT"])
def test_stage_rejects_artifact_replaced_during_process_before_accepting_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)

    class Process:
        pid = 1234

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def wait(self, timeout: int) -> int:
            getattr(module, artifact).write_text("changed during match")
            return 0

    def unexpected_audit(*args: Any, **kwargs: Any) -> dict[str, str]:
        pytest.fail("match audit accepted inputs changed during stage execution")

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "audit_match", unexpected_audit)
    assert module.run_stage("preflight", 4, 1, 600, False) == "FAIL"
    run = json.loads((tmp_path / "ap04_preflight_1/run.json").read_text())
    assert run["status"] == "FAIL" and "identity" in run["reason"]


def test_stage_accepts_unchanged_inputs_and_records_pinned_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)

    class Process:
        pid = 1234

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def wait(self, timeout: int) -> int:
            return 0

    def successful_audit(*args: Any, **kwargs: Any) -> dict[str, str]:
        return {"status": "PASS"}

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "audit_match", successful_audit)
    assert module.run_stage("preflight", 4, 1, 600, False) == "PASS"
    run = json.loads((tmp_path / "ap04_preflight_1/run.json").read_text())
    assert run["exit_code"] == 0
    assert run["performance_audit_sha256"] == hashlib.sha256(module.PERFORMANCE_AUDIT.read_bytes()).hexdigest()


@pytest.mark.parametrize("artifact", [
    "CANDIDATE_LAUNCHER", "BASELINE_LAUNCHER", "PERFORMANCE_AUDIT", "MATCH_AUDITOR", "UCI_AUDITOR",
])
def test_stage_rejects_identity_change_during_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)

    class Process:
        pid = 1234

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def wait(self, timeout: int) -> int:
            return 0

    def replacing_audit(*args: Any, **kwargs: Any) -> dict[str, str]:
        getattr(module, artifact).write_text("replaced during audit")
        return {"status": "PASS"}

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "audit_match", replacing_audit)
    assert module.run_stage("preflight", 4, 1, 600, False) == "FAIL"
    run = json.loads((tmp_path / "ap04_preflight_1/run.json").read_text())
    assert run["status"] == "FAIL"
    assert "identity after audit" in run["reason"]


def test_audit_match_rejects_replaced_source_before_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    # Directly exercise the dynamic loader, including the old sibling-path lookup.
    monkeypatch.setattr(module, "__file__", str(tmp_path / "pipeline.py"))
    marker = tmp_path / "untrusted-auditor-executed"
    module.MATCH_AUDITOR.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def audit_match(*args, **kwargs):\n    return {'status': 'PASS'}\n"
    )
    with pytest.raises(RuntimeError, match="auditor identity"):
        module.audit_match(tmp_path, 4, 1, False)
    assert not marker.exists()


def test_audit_match_loads_unchanged_pinned_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = identity_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "pipeline.py"))
    assert module.audit_match(tmp_path, 4, 1, False) == {"status": "PASS"}


def trace_loader(module: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = Path(__file__).parents[1] / "scripts/ap04_resume_audit.py"
    spec = util.spec_from_file_location("ap04_resume_trust_test", path)
    assert spec is not None and spec.loader is not None
    resume = util.module_from_spec(spec)
    spec.loader.exec_module(resume)
    monkeypatch.setattr(resume, "load_identity_verifier", lambda: module)
    return resume.load_trace_auditor()


def trace_lines(names: list[str], *, malformed: bool = False) -> list[str]:
    lines = []
    for name in names:
        engine = f"[Engine] <0> {name}"
        lines.extend([
            f"{engine} <--- position startpos",
            f"{engine} <--- go wtime 1000 btime 1000",
            f"{engine} ---> bestmove e2e4",
        ])
    if malformed:
        lines.append(f"[Engine] <0> {names[0]} ---> info bogus")
    return lines


@pytest.mark.parametrize("ending", ["lf", "crlf", "mixed"])
def test_real_auditor_accepts_checkout_line_endings_without_repinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    module = pipeline()
    for attribute, name in (("MATCH_AUDITOR", "ap03_match_audit.py"), ("UCI_AUDITOR", "ap03_uci_audit.py")):
        source = (Path(__file__).parents[1] / "scripts" / name).read_bytes().replace(b"\r\n", b"\n")
        if ending == "crlf":
            source = source.replace(b"\n", b"\r\n")
        elif ending == "mixed":
            source = source.replace(b"\n", b"\r\n", 3)
        target = tmp_path / name
        target.write_bytes(source)
        monkeypatch.setattr(module, attribute, target, raising=False)
    loaded = trace_loader(module, monkeypatch)
    assert loaded.audit_trace(trace_lines(loaded.NAMES), 1, allow_interrupted=True)["searches"] == 2
    with pytest.raises(AssertionError, match="Malformed UCI"):
        loaded.audit_trace(trace_lines(loaded.NAMES, malformed=True), 1, allow_interrupted=True)


def helper_boundary_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = pipeline()
    # Use actual LF source, with the main pin re-bound solely to isolate the
    # dependency defect from the independent checkout-line-ending defect.
    for attribute, name in (("MATCH_AUDITOR", "ap03_match_audit.py"), ("UCI_AUDITOR", "ap03_uci_audit.py")):
        source = (Path(__file__).parents[1] / "scripts" / name).read_bytes().replace(b"\r\n", b"\n")
        path = tmp_path / name
        path.write_bytes(source)
        monkeypatch.setattr(module, attribute, path, raising=False)
        monkeypatch.setattr(module, "EXPECTED_" + attribute, hashlib.sha256(source).hexdigest(), raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    # Register an undo even when the key was absent: a faulty loader may import
    # the injected helper, which must not leak into subsequent tests.
    monkeypatch.setitem(sys.modules, "ap03_uci_audit", ModuleType("placeholder"))
    monkeypatch.delitem(sys.modules, "ap03_uci_audit", raising=False)
    return module


def test_replaced_uci_helper_is_rejected_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = helper_boundary_fixture(tmp_path, monkeypatch)
    marker = tmp_path / "replaced-helper-executed"
    module.UCI_AUDITOR.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def audit(events):\n    pass\n"
    )
    with pytest.raises(RuntimeError, match="UCI auditor identity"):
        trace_loader(module, monkeypatch)
    assert not marker.exists()


@pytest.mark.parametrize("shadow", ["path", "module-cache"])
def test_uci_helper_shadow_cannot_bypass_malformed_output_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: str,
) -> None:
    module = helper_boundary_fixture(tmp_path, monkeypatch)
    if shadow == "module-cache":
        cached = ModuleType("ap03_uci_audit")
        setattr(cached, "audit", lambda events: None)
        monkeypatch.setitem(sys.modules, "ap03_uci_audit", cached)
    else:
        shadow_root = tmp_path / "shadow"
        shadow_root.mkdir()
        (shadow_root / "ap03_uci_audit.py").write_text("def audit(events):\n    pass\n")
        monkeypatch.syspath_prepend(str(shadow_root))
    loaded = trace_loader(module, monkeypatch)
    with pytest.raises(AssertionError, match="Malformed UCI"):
        loaded.audit_trace(trace_lines(loaded.NAMES, malformed=True), 1, allow_interrupted=True)


@pytest.mark.parametrize("attribute,name", [
    ("MATCH_AUDITOR", "ap03_match_audit.py"), ("UCI_AUDITOR", "ap03_uci_audit.py"),
])
@pytest.mark.parametrize("change", ["bare-cr", "trailing-space", "semantic"])
def test_canonical_auditor_pin_rejects_non_crlf_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str, name: str, change: str,
) -> None:
    module = pipeline()
    source = (Path(__file__).parents[1] / "scripts" / name).read_bytes().replace(b"\r\n", b"\n")
    if change == "bare-cr":
        source = source.replace(b"\n", b"\r", 1)
    elif change == "trailing-space":
        source += b" "
    else:
        source += b"\naudit = lambda events: None\n"
    path = tmp_path / name
    path.write_bytes(source)
    monkeypatch.setattr(module, attribute, path)
    with pytest.raises(RuntimeError, match="auditor identity"):
        trace_loader(module, monkeypatch)
