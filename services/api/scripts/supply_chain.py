from __future__ import annotations

import argparse
import difflib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

API_DIR = Path(__file__).resolve().parents[1]
PYPROJECT = API_DIR / "pyproject.toml"
RUNTIME_LOCK = API_DIR / "requirements.lock"
DEV_LOCK = API_DIR / "requirements-dev.lock"
SBOM = API_DIR / "sbom.cdx.json"
LOCK_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\\\s]+)")


def ensure_target_environment() -> None:
    target = (
        sys.platform == "linux"
        and sys.version_info[:2] == (3, 11)
        and platform.python_implementation() == "CPython"
        and platform.machine().lower() in {"amd64", "x86_64"}
    )
    if not target:
        raise SystemExit(
            "Supply-chain artifacts must be generated on Linux x86_64 with CPython 3.11."
        )


def run(command: list[str]) -> None:
    # Callers construct commands from repository constants and boolean CLI modes only.
    subprocess.run(command, cwd=API_DIR, check=True)  # nosec B603


def ensure_tool_versions() -> None:
    with PYPROJECT.open("rb") as pyproject_file:
        configuration = tomllib.load(pyproject_file)

    configured_tools = [
        *configuration["build-system"]["requires"],
        *configuration["project"]["optional-dependencies"]["supply-chain"],
    ]
    for configured_tool in configured_tools:
        requirement = Requirement(configured_tool)
        exact_versions = [
            specifier.version for specifier in requirement.specifier if specifier.operator == "=="
        ]
        if len(exact_versions) != 1:
            raise SystemExit(f"Supply-chain tool must have one exact pin: {requirement.name}")
        installed_version = importlib.metadata.version(requirement.name)
        if installed_version != exact_versions[0]:
            raise SystemExit(
                f"Supply-chain tool version mismatch for {requirement.name}: "
                f"expected {exact_versions[0]}, installed {installed_version}."
            )


def compile_lock(
    destination: Path,
    current_lock: Path,
    *,
    extras: tuple[str, ...] = (),
    include_editable_build_deps: bool = False,
    upgrade: bool = False,
) -> None:
    if current_lock.exists() and not upgrade:
        shutil.copyfile(current_lock, destination)

    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        str(PYPROJECT),
        "--output-file",
        str(destination),
        "--resolver",
        "backtracking",
        "--generate-hashes",
        "--strip-extras",
        "--allow-unsafe",
        "--no-header",
        "--no-annotate",
        "--no-emit-index-url",
        "--no-emit-trusted-host",
        "--no-emit-find-links",
        "--no-emit-options",
        "--newline",
        "lf",
        "--no-config",
        "--quiet",
    ]
    command.append("--upgrade" if upgrade else "--reuse-hashes")
    for extra in extras:
        command.extend(("--extra", extra))
    if include_editable_build_deps:
        command.extend(("--build-deps-for", "editable"))
    run(command)


def install_runtime_metadata(runtime_lock: Path, destination: Path) -> None:
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--no-deps",
            "--ignore-installed",
            "--quiet",
            "--target",
            str(destination),
            "-r",
            str(runtime_lock),
        ]
    )


def read_locked_versions(runtime_lock: Path) -> dict[str, str]:
    locked_versions: dict[str, str] = {}
    for line in runtime_lock.read_text(encoding="utf-8").splitlines():
        if match := LOCK_REQUIREMENT.match(line):
            locked_versions[canonicalize_name(match.group(1))] = match.group(2)
    if not locked_versions:
        raise SystemExit("Runtime lock does not contain pinned requirements.")
    return locked_versions


def marker_applies(requirement: Requirement, active_extras: set[str]) -> bool:
    if requirement.marker is None:
        return True
    environment = default_environment()
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in {"", *active_extras}
    )


def dependency_graph(
    runtime_lock: Path, metadata_path: Path
) -> tuple[set[str], dict[str, set[str]]]:
    locked_versions = read_locked_versions(runtime_lock)
    distributions = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions(path=[str(metadata_path)])
        if distribution.metadata["Name"]
    }
    installed_versions = {
        name: distribution.version for name, distribution in distributions.items()
    }
    if installed_versions != locked_versions:
        missing = sorted(set(locked_versions) - set(installed_versions))
        unexpected = sorted(set(installed_versions) - set(locked_versions))
        mismatched = sorted(
            name
            for name in set(locked_versions) & set(installed_versions)
            if locked_versions[name] != installed_versions[name]
        )
        raise SystemExit(
            "Runtime metadata does not match the lock: "
            f"missing={missing}, unexpected={unexpected}, mismatched={mismatched}"
        )

    with PYPROJECT.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file)["project"]

    root_dependencies: set[str] = set()
    active_extras: defaultdict[str, set[str]] = defaultdict(set)
    for dependency in project["dependencies"]:
        requirement = Requirement(dependency)
        if marker_applies(requirement, set()):
            name = canonicalize_name(requirement.name)
            root_dependencies.add(name)
            active_extras[name].update(requirement.extras)

    graph: defaultdict[str, set[str]] = defaultdict(set)
    processed_extras: dict[str, frozenset[str]] = {}
    pending = set(locked_versions)
    while pending:
        name = pending.pop()
        extras = frozenset(active_extras[name])
        if processed_extras.get(name) == extras:
            continue
        processed_extras[name] = extras

        for raw_requirement in distributions[name].requires or ():
            requirement = Requirement(raw_requirement)
            if not marker_applies(requirement, set(extras)):
                continue
            dependency_name = canonicalize_name(requirement.name)
            if dependency_name not in locked_versions:
                continue
            graph[name].add(dependency_name)
            new_extras = set(requirement.extras) - active_extras[dependency_name]
            if new_extras:
                active_extras[dependency_name].update(new_extras)
                pending.add(dependency_name)

    return root_dependencies, dict(graph)


def complete_and_validate_sbom(
    destination: Path,
    runtime_lock: Path,
    metadata_path: Path,
) -> None:
    document = json.loads(destination.read_text(encoding="utf-8"))
    component_refs = {
        canonicalize_name(component["name"]): component["bom-ref"]
        for component in document["components"]
    }
    root_ref = document["metadata"]["component"]["bom-ref"]
    root_dependencies, graph = dependency_graph(runtime_lock, metadata_path)

    locked_names = set(read_locked_versions(runtime_lock))
    if set(component_refs) != locked_names:
        raise SystemExit("CycloneDX components do not match the runtime lock.")
    if not root_dependencies <= locked_names:
        raise SystemExit("A direct project dependency is missing from the runtime lock.")

    dependencies = []
    for name, ref in component_refs.items():
        entry: dict[str, object] = {"ref": ref}
        depends_on = sorted(component_refs[dependency] for dependency in graph.get(name, set()))
        if depends_on:
            entry["dependsOn"] = depends_on
        dependencies.append(entry)
    dependencies.append(
        {
            "ref": root_ref,
            "dependsOn": sorted(component_refs[name] for name in root_dependencies),
        }
    )
    document["dependencies"] = sorted(dependencies, key=lambda dependency: str(dependency["ref"]))

    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    validation_errors = JsonStrictValidator(SchemaVersion.V1_6).validate_str(
        serialized, all_errors=True
    )
    if validation_errors is not None:
        errors = "\n".join(str(error) for error in validation_errors)
        raise SystemExit(f"Generated CycloneDX SBOM is invalid:\n{errors}")
    destination.write_text(serialized, encoding="utf-8", newline="\n")


def generate_sbom(runtime_lock: Path, destination: Path, metadata_path: Path) -> None:
    install_runtime_metadata(runtime_lock, metadata_path)
    run(
        [
            sys.executable,
            "-W",
            "ignore",
            "-m",
            "cyclonedx_py",
            "requirements",
            str(runtime_lock),
            "--pyproject",
            str(PYPROJECT),
            "--mc-type",
            "application",
            "--spec-version",
            "1.6",
            "--output-reproducible",
            "--output-format",
            "JSON",
            "--output-file",
            str(destination),
            "--validate",
        ]
    )
    complete_and_validate_sbom(destination, runtime_lock, metadata_path)


def diff_artifact(expected: Path, generated: Path) -> bool:
    if not expected.exists():
        print(f"Missing generated artifact: {expected.relative_to(API_DIR)}", file=sys.stderr)
        return False

    expected_text = expected.read_text(encoding="utf-8").splitlines(keepends=True)
    generated_text = generated.read_text(encoding="utf-8").splitlines(keepends=True)
    if expected_text == generated_text:
        return True

    diff = difflib.unified_diff(
        expected_text,
        generated_text,
        fromfile=str(expected.relative_to(API_DIR)),
        tofile=f"generated/{expected.name}",
    )
    sys.stderr.writelines(diff)
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or verify MyRetail API Python locks and CycloneDX SBOM."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Fail if committed artifacts drift.")
    mode.add_argument("--write", action="store_true", help="Write generated artifacts.")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Resolve newer compatible versions; valid only together with --write.",
    )
    args = parser.parse_args()
    if args.upgrade and not args.write:
        parser.error("--upgrade requires --write")
    return args


def main() -> int:
    args = parse_args()
    ensure_target_environment()
    ensure_tool_versions()

    with tempfile.TemporaryDirectory(prefix="myretail-supply-chain-") as temporary:
        temporary_dir = Path(temporary)
        runtime_lock = temporary_dir / RUNTIME_LOCK.name
        dev_lock = temporary_dir / DEV_LOCK.name
        sbom = temporary_dir / SBOM.name
        runtime_metadata = temporary_dir / "runtime-site-packages"

        compile_lock(runtime_lock, RUNTIME_LOCK, upgrade=args.upgrade)
        compile_lock(
            dev_lock,
            DEV_LOCK,
            extras=("dev", "supply-chain"),
            include_editable_build_deps=True,
            upgrade=args.upgrade,
        )
        generate_sbom(runtime_lock, sbom, runtime_metadata)

        generated = ((RUNTIME_LOCK, runtime_lock), (DEV_LOCK, dev_lock), (SBOM, sbom))
        if args.write:
            for destination, source in generated:
                shutil.copyfile(source, destination)
            print("Updated requirements.lock, requirements-dev.lock, and sbom.cdx.json.")
            return 0

        artifacts_match = [diff_artifact(expected, actual) for expected, actual in generated]
        if all(artifacts_match):
            print("Python dependency locks and CycloneDX SBOM are reproducible and current.")
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
