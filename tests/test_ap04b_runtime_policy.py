from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
import venv
from types import ModuleType

import pytest
import chess


ROOT = Path(__file__).resolve().parents[1]


def policy() -> ModuleType:
    path = ROOT / "scripts" / "ap04b_runtime_policy.py"
    assert path.exists(), "AP04B runtime policy has not been implemented"
    spec = importlib.util.spec_from_file_location("ap04b_runtime_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def executable_root(tmp_path: Path) -> Path:
    root = tmp_path / "engine"
    package = root / "src" / "chess_engine"
    native = package / "cython_ext"
    native.mkdir(parents=True)
    (package / "stockconda.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "runner.py").write_text("VALUE = 2\n", encoding="utf-8")
    for stem in ("cython_chess", "evaluation", "transposition"):
        (native / f"{stem}.cpython-312-x86_64-linux-gnu.so").write_bytes(
            stem.encode("ascii")
        )
    return root


@pytest.mark.parametrize(
    "relative",
    [
        "src/sitecustomize.pyc",
        "src/usercustomize.pyc",
        "src/chess_engine/__pycache__/stockconda.cpython-312.pyc",
        "src/injected.pth",
        "src/sitecustomize.py",
        "src/sitecustomize/__init__.py",
        "src/usercustomize/__init__.py",
    ],
)
def test_executable_inventory_rejects_unadmitted_startup_artifacts(
    tmp_path: Path,
    relative: str,
) -> None:
    module = policy()
    root = executable_root(tmp_path)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"injected")

    with pytest.raises(RuntimeError, match="forbidden executable artifact"):
        module.inventory_executables(root)


def test_executable_inventory_rejects_unexpected_native_module(
    tmp_path: Path,
) -> None:
    module = policy()
    root = executable_root(tmp_path)
    (root / "src" / "rogue.cpython-312-x86_64-linux-gnu.so").write_bytes(
        b"rogue"
    )

    with pytest.raises(RuntimeError, match="unexpected native module"):
        module.inventory_executables(root)


def test_executable_root_binds_every_allowed_native_module(
    tmp_path: Path,
) -> None:
    module = policy()
    root = executable_root(tmp_path)
    frozen = module.inventory_executables(root)
    native = next(path for path in frozen if path.endswith("evaluation.cpython-312-x86_64-linux-gnu.so"))
    (root / native).write_bytes(b"changed")

    with pytest.raises(RuntimeError, match="executable root changed"):
        module.verify_executable_root(root, frozen)


@pytest.mark.parametrize("package", ["sitecustomize", "usercustomize"])
def test_executable_root_rejects_startup_package_added_after_freeze(
    tmp_path: Path,
    package: str,
) -> None:
    module = policy()
    root = executable_root(tmp_path)
    frozen = module.inventory_executables(root)
    injected = root / "src" / package / "__init__.py"
    injected.parent.mkdir()
    injected.write_text("INJECTED = True\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forbidden executable artifact"):
        module.verify_executable_root(root, frozen)


def test_controlled_runtime_environment_sets_import_and_flag_controls(
    tmp_path: Path,
) -> None:
    module = policy()
    root = executable_root(tmp_path)
    environment = module.runtime_environment(root, {"PATH": "fixture"})
    assert environment == {
        "PATH": "fixture",
        "PYTHONPATH": str(root / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONOPTIMIZE": "0",
        "PYTHONSAFEPATH": "1",
        "STOCKCONDA_TEST_SOURCE": str(root / "src"),
    }

    command = [
        sys.executable,
        "-S",
        "-s",
        "-B",
        "-P",
        "-c",
        (
            "import json,sys; "
            "print(json.dumps({'optimize':sys.flags.optimize,"
            "'dont_write_bytecode':sys.dont_write_bytecode,"
            "'no_user_site':sys.flags.no_user_site,"
            "'no_site':sys.flags.no_site,"
            "'safe_path':sys.flags.safe_path}))"
        ),
    ]
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout) == {
        "optimize": 0,
        "dont_write_bytecode": True,
        "no_user_site": 1,
        "no_site": 1,
        "safe_path": True,
    }


def test_controlled_python_start_skips_global_pth_but_loads_bound_package(
    tmp_path: Path,
) -> None:
    """Catches global .pth import statements running before AP04B checks."""

    module = policy()
    environment_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment_root)
    executable = (
        environment_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    purelib_result = subprocess.run(
        [str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
        capture_output=True,
        check=True,
    )
    purelib = Path(purelib_result.stdout.strip())
    purelib.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "pth-ran"
    (purelib / "sentinel.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    package = purelib / "chess"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 17\n", encoding="utf-8")
    script = tmp_path / "probe.py"
    script.write_text("import chess; print(chess.VALUE)\n", encoding="utf-8")
    base = {"PATH": str(executable.parent)}

    uncontrolled = subprocess.run(
        [str(executable), str(script)],
        env=base,
        text=True,
        capture_output=True,
        check=True,
    )
    assert uncontrolled.stdout.strip() == "17"
    assert marker.read_text(encoding="utf-8") == "ran"
    marker.unlink()

    controlled = subprocess.run(
        module.controlled_python_command(script, executable=str(executable)),
        env=module.runtime_environment(
            tmp_path,
            base,
            import_paths=(purelib,),
        ),
        text=True,
        capture_output=True,
        check=True,
    )

    assert controlled.stdout.strip() == "17"
    assert not marker.exists()


@pytest.mark.parametrize(
    "name",
    ["LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"],
)
def test_runtime_environment_rejects_dynamic_loader_injection(
    tmp_path: Path,
    name: str,
) -> None:
    module = policy()
    root = executable_root(tmp_path)

    with pytest.raises(RuntimeError, match="dynamic loader"):
        module.runtime_environment(
            root,
            {"PATH": "fixture", name: "/tmp/injected.so"},
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PYTHONTRACEMALLOC", "1"),
        ("PYTHONMALLOC", "debug"),
        ("PYTHONPLATLIBDIR", "unexpected-lib"),
        ("PYTHONDEBUG", "1"),
        ("PYTHONWARNDEFAULTENCODING", "1"),
    ],
)
def test_runtime_environment_rejects_uncontrolled_python_startup_options(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    """Catches debug/timing options leaking into an AP04B worker."""

    module = policy()
    root = executable_root(tmp_path)

    with pytest.raises(RuntimeError, match="Python startup environment"):
        module.runtime_environment(
            root,
            {"PATH": "fixture", name: value},
        )


def test_runtime_identity_records_and_rejects_loader_influence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = policy()
    monkeypatch.setenv("LD_PRELOAD", "/tmp/injected.so")

    identity = module.runtime_identity()

    assert identity["environment"]["LD_PRELOAD"] == "/tmp/injected.so"
    with pytest.raises(RuntimeError, match="runtime controls"):
        module.validate_runtime_identity(identity)


@pytest.mark.parametrize("mutation", ["changed", "additional", "missing"])
def test_imported_package_identity_binds_same_version_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    module = policy()
    assert hasattr(module, "imported_package_identity"), (
        "runtime policy does not bind imported package contents"
    )
    package_name = "ap04b_fixture_chess"
    package = tmp_path / package_name
    package.mkdir()
    init = package / "__init__.py"
    helper = package / "helper.py"
    init.write_text("__version__ = '1.0'\n", encoding="utf-8")
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, package_name, raising=False)
    importlib.invalidate_caches()

    frozen = module.imported_package_identity(package_name)
    assert frozen["root"] == str(package.resolve())
    assert frozen["version"] == "1.0"
    assert set(frozen["files"]) >= {"__init__.py", "helper.py"}

    if mutation == "changed":
        helper.write_text("VALUE = 2\n", encoding="utf-8")
    elif mutation == "additional":
        (package / "extra.py").write_text("VALUE = 3\n", encoding="utf-8")
    else:
        helper.unlink()

    with pytest.raises(RuntimeError, match="package identity changed"):
        module.verify_imported_package(package_name, frozen)


@pytest.mark.parametrize("link_kind", ["root", "directory", "file"])
def test_imported_package_identity_rejects_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    module = policy()
    package_name = "ap04b_fixture_symlink_" + link_kind
    real_package = tmp_path / (package_name + "_real")
    real_package.mkdir()
    (real_package / "__init__.py").write_text(
        "__version__ = '1.0'\n", encoding="utf-8"
    )
    package = tmp_path / package_name
    if link_kind == "root":
        package.symlink_to(real_package, target_is_directory=True)
    else:
        package.mkdir()
        (package / "__init__.py").write_text(
            "__version__ = '1.0'\n", encoding="utf-8"
        )
        target = tmp_path / ("target_" + link_kind)
        if link_kind == "directory":
            target.mkdir()
            (target / "hidden.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "linked").symlink_to(target, target_is_directory=True)
        else:
            target.write_text("VALUE = 1\n", encoding="utf-8")
            (package / "linked.py").symlink_to(target)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, package_name, raising=False)
    importlib.invalidate_caches()

    with pytest.raises(RuntimeError, match="package symlink"):
        module.imported_package_identity(package_name)


@pytest.mark.parametrize("mutation", ["version", "root"])
def test_imported_package_location_rejects_version_or_root_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Catches NumPy replay from a different version or installation root."""

    module = policy()
    first = tmp_path / "first" / "numpy"
    second = tmp_path / "second" / "numpy"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    state: dict[str, Any] = {"root": first, "version": "2.1.0"}

    def imported(_name: str) -> SimpleNamespace:
        root = state["root"]
        return SimpleNamespace(
            __file__=str(root / "__init__.py"),
            __path__=[str(root)],
            __version__=state["version"],
        )

    monkeypatch.setattr(module.importlib, "import_module", imported)
    assert hasattr(module, "imported_package_location_identity"), (
        "runtime policy cannot freeze a package version and installation path"
    )
    frozen = module.imported_package_location_identity("numpy")
    assert frozen == {"root": str(first.resolve()), "version": "2.1.0"}

    if mutation == "version":
        state["version"] = "2.2.0"
    else:
        state["root"] = second

    with pytest.raises(RuntimeError, match="package location changed: numpy"):
        module.verify_imported_package_location("numpy", frozen)


def test_runtime_identity_binds_interpreter_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = policy()
    executable = tmp_path / "python"
    executable.write_bytes(b"interpreter-v1")
    monkeypatch.setattr(module.sys, "executable", str(executable))

    identity = module.runtime_identity()

    assert identity["executable"] == str(executable)
    assert identity["executable_sha256"] == hashlib.sha256(
        b"interpreter-v1"
    ).hexdigest()


def test_frozen_runtime_rejects_interpreter_replaced_at_same_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = policy()
    assert hasattr(module, "verify_runtime_identity"), (
        "runtime policy cannot compare a frozen interpreter identity"
    )
    executable = tmp_path / "python"
    executable.write_bytes(b"interpreter-v1")
    monkeypatch.setattr(module.sys, "executable", str(executable))
    frozen = module.runtime_identity()
    executable.write_bytes(b"interpreter-v2")

    with pytest.raises(RuntimeError, match="runtime identity changed"):
        module.verify_runtime_identity(frozen)


def test_runtime_identity_rejects_loaded_startup_customization() -> None:
    module = policy()
    identity = {
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
        "startup_modules": {
            "sitecustomize": {"path": "/tmp/rogue.py", "sha256": "0" * 64}
        },
    }
    with pytest.raises(RuntimeError, match="runtime controls"):
        module.validate_runtime_identity(identity)


def test_current_controlled_runtime_identity_is_admitted() -> None:
    module = policy()
    module.validate_runtime_identity(module.runtime_identity())


@pytest.mark.parametrize(
    "runner",
    [
        "ap04b_gate2.py",
        "ap04b_ordering_benchmark.py",
        "ap04b_gate3_audit.py",
        "ap04b_match_pipeline.py",
    ],
)
def test_runner_loads_in_clean_controlled_interpreter(runner: str) -> None:
    module = policy()
    completed = subprocess.run(
        module.controlled_python_command(ROOT / "scripts" / runner, "--help"),
        env=module.runtime_environment(ROOT, {"PATH": str(Path(sys.executable).parent)}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "runner",
    [
        "ap04b_gate2.py",
        "ap04b_ordering_benchmark.py",
        "ap04b_gate3_audit.py",
        "ap04b_match_pipeline.py",
    ],
)
def test_runner_refuses_uncontrolled_site_started_interpreter(
    runner: str,
) -> None:
    """Catches a top-level evidence runner accepting normal site startup."""

    module = policy()
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / runner), "--help"],
        env=module.runtime_environment(
            ROOT,
            {"PATH": str(Path(sys.executable).parent)},
            import_paths=(Path(chess.__file__).resolve().parent.parent,),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "requires Python startup flags -S -s -B -P" in completed.stderr
