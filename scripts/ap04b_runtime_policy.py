"""Shared executable-root and Python-runtime policy for AP04B gates."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import importlib
import os
from pathlib import Path
import sys
from typing import Any


AUTHORED_SUFFIXES = {".py", ".pyi", ".pyx", ".pxd", ".h"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".pth"}
NATIVE_SUFFIXES = {".so", ".pyd", ".dll", ".dylib"}
EXPECTED_NATIVE_STEMS = {"cython_chess", "evaluation", "transposition"}
STARTUP_STEMS = {"sitecustomize", "usercustomize"}
LOADER_ENVIRONMENT = ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH")
CONTROLLED_PYTHON_FLAGS = ("-S", "-s", "-B", "-P")
CONTROLLED_PYTHON_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED",
    "PYTHONNOUSERSITE",
    "PYTHONOPTIMIZE",
    "PYTHONPATH",
    "PYTHONSAFEPATH",
}
PYTHON_STARTUP_ENVIRONMENT = CONTROLLED_PYTHON_ENVIRONMENT | {
    "PYTHONASYNCIODEBUG",
    "PYTHONBREAKPOINT",
    "PYTHONCASEOK",
    "PYTHONCOERCECLOCALE",
    "PYTHONDEBUG",
    "PYTHONDEVMODE",
    "PYTHONEXECUTABLE",
    "PYTHONFAULTHANDLER",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONINTMAXSTRDIGITS",
    "PYTHONIOENCODING",
    "PYTHONLEGACYWINDOWSFSENCODING",
    "PYTHONLEGACYWINDOWSSTDIO",
    "PYTHONMALLOC",
    "PYTHONMALLOCSTATS",
    "PYTHONNODEBUGRANGES",
    "PYTHONPROFILEIMPORTTIME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR",
    "PYTHONSTARTUP",
    "PYTHONTRACEMALLOC",
    "PYTHONUNBUFFERED",
    "PYTHONUSERBASE",
    "PYTHONUTF8",
    "PYTHONVERBOSE",
    "PYTHONWARNDEFAULTENCODING",
    "PYTHONWARNINGS",
}
UNCONTROLLED_PYTHON_ENVIRONMENT = tuple(
    sorted(PYTHON_STARTUP_ENVIRONMENT - CONTROLLED_PYTHON_ENVIRONMENT)
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_authored_source(relative: str) -> bool:
    suffix = Path(relative).suffix
    return suffix in AUTHORED_SUFFIXES or relative.endswith(
        "cython_chess_backend.cpp"
    )


def _native_stem(path: Path) -> str:
    return path.name.split(".", 1)[0]


def inventory_executables(root: Path) -> dict[str, str]:
    """Return every admitted executable/source artifact below one build root."""
    artifacts: dict[str, str] = {}
    native_stems: set[str] = set()
    for parent_name in ("src", "scripts"):
        parent = root / parent_name
        if not parent.is_dir():
            continue
        for startup_stem in STARTUP_STEMS:
            startup_package = parent / startup_stem
            if startup_package.is_dir():
                relative = startup_package.relative_to(root).as_posix()
                raise RuntimeError("forbidden executable artifact: " + relative)
        for path in sorted(parent.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            suffix = path.suffix.lower()
            stem = _native_stem(path)
            if suffix in FORBIDDEN_SUFFIXES or stem in STARTUP_STEMS:
                raise RuntimeError("forbidden executable artifact: " + relative)
            if suffix in NATIVE_SUFFIXES:
                expected_parent = "src/chess_engine/cython_ext/"
                if (
                    not relative.startswith(expected_parent)
                    or stem not in EXPECTED_NATIVE_STEMS
                ):
                    raise RuntimeError("unexpected native module: " + relative)
                native_stems.add(stem)
                artifacts[relative] = sha256(path)
            elif is_authored_source(relative):
                artifacts[relative] = sha256(path)
    missing = sorted(EXPECTED_NATIVE_STEMS - native_stems)
    if missing:
        raise RuntimeError("missing native module: " + missing[0])
    return artifacts


def verify_executable_root(root: Path, frozen: Mapping[str, str]) -> None:
    current = inventory_executables(root)
    if current != dict(frozen):
        changed = sorted(set(current) ^ set(frozen))
        if not changed:
            changed = sorted(
                relative
                for relative, digest in current.items()
                if frozen.get(relative) != digest
            )
        detail = changed[0] if changed else "unknown"
        raise RuntimeError("executable root changed: " + detail)


def runtime_environment(
    root: Path,
    base: Mapping[str, str] | None = None,
    *,
    import_paths: Iterable[Path] = (),
) -> dict[str, str]:
    environment = validated_environment(base)
    paths = [root.resolve() / "src"]
    paths.extend(path.resolve() for path in import_paths)
    environment.update(
        PYTHONPATH=os.pathsep.join(str(path) for path in paths),
        STOCKCONDA_TEST_SOURCE=str(paths[0]),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
        PYTHONNOUSERSITE="1",
        PYTHONOPTIMIZE="0",
        PYTHONSAFEPATH="1",
    )
    return environment


def controlled_python_command(
    script: Path,
    *arguments: str,
    executable: str | None = None,
) -> list[str]:
    """Run a script without importing site or processing any .pth file."""
    return [
        sys.executable if executable is None else executable,
        *CONTROLLED_PYTHON_FLAGS,
        str(script.resolve()),
        *arguments,
    ]


def require_controlled_startup() -> None:
    """Refuse evidence generation after an uncontrolled site startup."""
    if (
        sys.flags.no_site != 1
        or sys.flags.no_user_site != 1
        or sys.flags.safe_path is not True
        or sys.dont_write_bytecode is not True
    ):
        raise RuntimeError("AP04B requires Python startup flags -S -s -B -P")


def package_import_paths(packages: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return explicit parents for already content-bound package roots."""
    parents: list[Path] = []
    for name in sorted(packages):
        identity = packages[name]
        if not isinstance(identity, Mapping):
            raise RuntimeError(f"package identity is incomplete: {name}")
        root = identity.get("root")
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise RuntimeError(f"package identity is incomplete: {name}")
        parents.append(Path(root).resolve().parent)
    return tuple(parents)


def validated_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment only when loader injection is absent."""
    environment = dict(os.environ if base is None else base)
    active = [name for name in LOADER_ENVIRONMENT if environment.get(name)]
    if active:
        raise RuntimeError(
            "dynamic loader environment is not admitted: " + ", ".join(active)
        )
    python_options = [
        name
        for name in UNCONTROLLED_PYTHON_ENVIRONMENT
        if environment.get(name)
    ]
    if python_options:
        raise RuntimeError(
            "uncontrolled Python startup environment is not admitted: "
            + ", ".join(python_options)
        )
    for name in LOADER_ENVIRONMENT:
        environment.pop(name, None)
    for name in UNCONTROLLED_PYTHON_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def imported_package_identity(name: str) -> dict[str, Any]:
    """Bind the exact imported package tree, not only its version string."""
    module = importlib.import_module(name)
    module_file = getattr(module, "__file__", None)
    package_paths = getattr(module, "__path__", None)
    if not isinstance(module_file, str) or package_paths is None:
        raise RuntimeError(f"imported package has no concrete root: {name}")
    raw_roots = [Path(value) for value in package_paths]
    if any(path.is_symlink() for path in raw_roots):
        raise RuntimeError(f"package symlink is not admitted: {name}")
    roots = [path.resolve() for path in raw_roots]
    if len(roots) != 1 or Path(module_file).resolve().parent != roots[0]:
        raise RuntimeError(f"imported package has ambiguous root: {name}")
    root = roots[0]
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"package symlink is not admitted: {name}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha256(path)
    if not files:
        raise RuntimeError(f"imported package is empty: {name}")
    version = getattr(module, "__version__", None)
    return {"root": str(root), "version": version, "files": files}


def verify_imported_package(name: str, frozen: Mapping[str, Any]) -> None:
    current = imported_package_identity(name)
    if current != dict(frozen):
        raise RuntimeError(f"package identity changed: {name}")


def imported_package_location_identity(name: str) -> dict[str, str]:
    """Bind one imported package's version and concrete installation root."""
    module = importlib.import_module(name)
    module_file = getattr(module, "__file__", None)
    package_paths = getattr(module, "__path__", None)
    version = getattr(module, "__version__", None)
    if (
        not isinstance(module_file, str)
        or package_paths is None
        or not isinstance(version, str)
    ):
        raise RuntimeError(f"package location is incomplete: {name}")
    raw_roots = [Path(value) for value in package_paths]
    if len(raw_roots) != 1 or raw_roots[0].is_symlink():
        raise RuntimeError(f"package location is incomplete: {name}")
    root = raw_roots[0].resolve()
    if Path(module_file).resolve().parent != root:
        raise RuntimeError(f"package location is incomplete: {name}")
    return {"root": str(root), "version": version}


def verify_imported_package_location(
    name: str,
    frozen: Mapping[str, Any],
) -> None:
    current = imported_package_location_identity(name)
    if current != dict(frozen):
        raise RuntimeError(f"package location changed: {name}")


def startup_modules_identity() -> dict[str, dict[str, str]]:
    identity: dict[str, dict[str, str]] = {}
    for name in sorted(STARTUP_STEMS):
        module = sys.modules.get(name)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            continue
        path = Path(module_file).resolve()
        identity[name] = {"path": str(path), "sha256": sha256(path)}
    return identity


def runtime_identity() -> dict[str, Any]:
    executable = Path(sys.executable)
    return {
        "python": sys.version,
        "executable": sys.executable,
        "executable_sha256": sha256(executable),
        "flags": {
            "optimize": sys.flags.optimize,
            "dont_write_bytecode": sys.dont_write_bytecode,
            "no_user_site": sys.flags.no_user_site,
            "no_site": sys.flags.no_site,
            "isolated": sys.flags.isolated,
            "ignore_environment": sys.flags.ignore_environment,
            "safe_path": sys.flags.safe_path,
        },
        "environment": {
            name: os.environ.get(name)
            for name in (
                "PYTHONDONTWRITEBYTECODE",
                "PYTHONHASHSEED",
                "PYTHONNOUSERSITE",
                "PYTHONOPTIMIZE",
                "PYTHONSAFEPATH",
                *UNCONTROLLED_PYTHON_ENVIRONMENT,
                *LOADER_ENVIRONMENT,
            )
        },
        "startup_modules": startup_modules_identity(),
    }


def validate_runtime_identity(identity: Mapping[str, Any]) -> None:
    flags = identity.get("flags")
    environment = identity.get("environment")
    if not isinstance(flags, Mapping) or not isinstance(environment, Mapping):
        raise RuntimeError("runtime identity is incomplete")
    executable = identity.get("executable")
    executable_sha256 = identity.get("executable_sha256")
    executable_matches = False
    if isinstance(executable, str) and isinstance(executable_sha256, str):
        try:
            executable_matches = (
                executable == sys.executable
                and sha256(Path(executable)) == executable_sha256
            )
        except OSError:
            executable_matches = False
    if (
        not executable_matches
        or identity.get("python") != sys.version
        or flags.get("optimize") != 0
        or flags.get("dont_write_bytecode") is not True
        or flags.get("no_user_site") != 1
        or flags.get("no_site") != 1
        or flags.get("safe_path") is not True
        or environment.get("PYTHONDONTWRITEBYTECODE") != "1"
        or environment.get("PYTHONHASHSEED") != "0"
        or environment.get("PYTHONNOUSERSITE") != "1"
        or environment.get("PYTHONOPTIMIZE") != "0"
        or environment.get("PYTHONSAFEPATH") != "1"
        or any(
            environment.get(name) not in (None, "")
            for name in UNCONTROLLED_PYTHON_ENVIRONMENT
        )
        or any(environment.get(name) not in (None, "") for name in LOADER_ENVIRONMENT)
        or identity.get("startup_modules") != {}
    ):
        raise RuntimeError("runtime controls are not enforced")


def verify_runtime_identity(frozen: Mapping[str, Any]) -> None:
    current = runtime_identity()
    validate_runtime_identity(current)
    if current != dict(frozen):
        raise RuntimeError("runtime identity changed")
