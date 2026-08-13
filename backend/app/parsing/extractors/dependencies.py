"""Dependency manifest parsers (SPEC.md §6 Phase 2 task 3).

One parser per manifest format, dispatched by filename in `extract_dependencies`.
`package.json` and `pyproject.toml` are exercised precisely against
`tests/fixtures/mini_repo` (SPEC.md's "dependency counts match the manifest
exactly" acceptance criterion); the rest (Pipfile, go.mod, Cargo.toml,
pom.xml, build.gradle, Gemfile) are unit-tested against inline fixtures —
mini_repo is a Python+TS repo, so adding a Go/Rust/Java/Ruby manifest to it
wouldn't be a realistic combination. `build.gradle`/`Gemfile` are
deliberately best-effort regex, not full Groovy/Ruby parsing — those
build-file DSLs are Turing-complete; a regex can't be "correct" for them,
only good enough for the common `dependencies { }` / `gem '...'` forms.
"""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePosixPath

DependencyKind = str  # "runtime" | "dev" | "peer" | "optional"


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    version_spec: str | None
    kind: DependencyKind
    manifest_path: str


MANIFEST_FILENAMES = {
    "package.json",
    "pyproject.toml",
    "Pipfile",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
}


def is_manifest_filename(name: str) -> bool:
    return name in MANIFEST_FILENAMES or (name.startswith("requirements") and name.endswith(".txt"))


def extract_dependencies(manifest_path: str, content: str) -> list[Dependency]:
    name = PurePosixPath(manifest_path).name
    if name == "package.json":
        return _parse_package_json(content, manifest_path)
    if name.startswith("requirements") and name.endswith(".txt"):
        return _parse_requirements_txt(content, manifest_path)
    if name == "pyproject.toml":
        return _parse_pyproject_toml(content, manifest_path)
    if name == "Pipfile":
        return _parse_pipfile(content, manifest_path)
    if name == "go.mod":
        return _parse_go_mod(content, manifest_path)
    if name == "Cargo.toml":
        return _parse_cargo_toml(content, manifest_path)
    if name == "pom.xml":
        return _parse_pom_xml(content, manifest_path)
    if name in ("build.gradle", "build.gradle.kts"):
        return _parse_build_gradle(content, manifest_path)
    if name == "Gemfile":
        return _parse_gemfile(content, manifest_path)
    return []


# --- npm ---


def _parse_package_json(content: str, manifest_path: str) -> list[Dependency]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    deps = []
    sections: list[tuple[str, DependencyKind]] = [
        ("dependencies", "runtime"),
        ("devDependencies", "dev"),
        ("peerDependencies", "peer"),
        ("optionalDependencies", "optional"),
    ]
    for key, kind in sections:
        for name, version in (data.get(key) or {}).items():
            deps.append(Dependency("npm", name, version, kind, manifest_path))
    return deps


# --- pypi ---

_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9_.\-\[\]]+)\s*(.*)$")
_PEP508_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _split_pep508(spec: str) -> tuple[str, str | None]:
    match = _PEP508_NAME_RE.match(spec.strip())
    name = match.group(1) if match else spec.strip()
    rest = spec.strip()[len(name) :].strip()
    return name, rest or None


def _parse_requirements_txt(content: str, manifest_path: str) -> list[Dependency]:
    filename = PurePosixPath(manifest_path).name.lower()
    kind: DependencyKind = "dev" if ("dev" in filename or "test" in filename) else "runtime"
    deps = []
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_LINE_RE.match(line)
        if match:
            name, version_spec = match.group(1), match.group(2).strip()
            deps.append(Dependency("pypi", name, version_spec or None, kind, manifest_path))
    return deps


def _parse_pyproject_toml(content: str, manifest_path: str) -> list[Dependency]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return []
    deps = []

    project = data.get("project", {})
    for spec in project.get("dependencies", []) or []:
        name, version_spec = _split_pep508(spec)
        deps.append(Dependency("pypi", name, version_spec, "runtime", manifest_path))
    for group, specs in (project.get("optional-dependencies") or {}).items():
        kind: DependencyKind = "dev" if group.lower() in ("dev", "test", "tests") else "optional"
        for spec in specs:
            name, version_spec = _split_pep508(spec)
            deps.append(Dependency("pypi", name, version_spec, kind, manifest_path))

    # Older/Poetry-style convention.
    poetry = data.get("tool", {}).get("poetry", {})
    for name, spec in (poetry.get("dependencies") or {}).items():
        if name.lower() == "python":
            continue
        version = spec if isinstance(spec, str) else spec.get("version") if isinstance(spec, dict) else None
        deps.append(Dependency("pypi", name, version, "runtime", manifest_path))
    for name, spec in (poetry.get("dev-dependencies") or {}).items():
        version = spec if isinstance(spec, str) else spec.get("version") if isinstance(spec, dict) else None
        deps.append(Dependency("pypi", name, version, "dev", manifest_path))

    return deps


def _parse_pipfile(content: str, manifest_path: str) -> list[Dependency]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return []
    deps = []
    sections: list[tuple[str, DependencyKind]] = [("packages", "runtime"), ("dev-packages", "dev")]
    for key, kind in sections:
        for name, spec in (data.get(key) or {}).items():
            version = spec if isinstance(spec, str) else spec.get("version") if isinstance(spec, dict) else None
            deps.append(Dependency("pypi", name, version or None, kind, manifest_path))
    return deps


# --- go ---

_GO_REQUIRE_LINE_RE = re.compile(r"^\s*(\S+)\s+(v\S+)")


def _parse_go_mod(content: str, manifest_path: str) -> list[Dependency]:
    deps = []
    in_require_block = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("require") and stripped.endswith("("):
            in_require_block = True
            continue
        if in_require_block and stripped == ")":
            in_require_block = False
            continue
        line = stripped
        if not in_require_block:
            if not line.startswith("require "):
                continue
            line = line[len("require ") :].strip()
        match = _GO_REQUIRE_LINE_RE.match(line)
        if match:
            # "// indirect" = brought in transitively, not directly required —
            # closest fit among runtime/dev/peer/optional is "optional".
            kind: DependencyKind = "optional" if "// indirect" in raw_line else "runtime"
            deps.append(Dependency("go", match.group(1), match.group(2), kind, manifest_path))
    return deps


# --- cargo ---


def _parse_cargo_toml(content: str, manifest_path: str) -> list[Dependency]:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return []
    deps = []
    sections: list[tuple[str, DependencyKind]] = [
        ("dependencies", "runtime"),
        ("dev-dependencies", "dev"),
        ("build-dependencies", "dev"),
    ]
    for key, kind in sections:
        for name, spec in (data.get(key) or {}).items():
            version = spec if isinstance(spec, str) else spec.get("version") if isinstance(spec, dict) else None
            deps.append(Dependency("cargo", name, version, kind, manifest_path))
    return deps


# --- maven ---


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_pom_xml(content: str, manifest_path: str) -> list[Dependency]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    deps = []
    for dep_el in root.iter():
        if _local_tag(dep_el.tag) != "dependency":
            continue
        fields = {_local_tag(c.tag): (c.text or "").strip() for c in dep_el}
        artifact = fields.get("artifactId")
        if not artifact:
            continue
        group = fields.get("groupId", "")
        name = f"{group}:{artifact}" if group else artifact
        kind: DependencyKind = "dev" if fields.get("scope") == "test" else "runtime"
        deps.append(Dependency("maven", name, fields.get("version") or None, kind, manifest_path))
    return deps


_GRADLE_DEP_RE = re.compile(
    r"""(implementation|api|compile|testImplementation|testCompile|runtimeOnly|compileOnly)"""
    r"""\s+['"]([\w.\-]+):([\w.\-]+):([\w.\-]+)['"]"""
)


def _parse_build_gradle(content: str, manifest_path: str) -> list[Dependency]:
    deps = []
    for match in _GRADLE_DEP_RE.finditer(content):
        config, group, artifact, version = match.groups()
        kind: DependencyKind = "dev" if "test" in config.lower() else "runtime"
        deps.append(Dependency("maven", f"{group}:{artifact}", version, kind, manifest_path))
    return deps


# --- rubygems ---

_GEM_LINE_RE = re.compile(r"""gem\s+['"]([\w.\-]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""")


def _parse_gemfile(content: str, manifest_path: str) -> list[Dependency]:
    deps = []
    for line in content.splitlines():
        match = _GEM_LINE_RE.search(line)
        if not match:
            continue
        name, version = match.group(1), match.group(2)
        is_dev = bool(re.search(r"group:\s*:?(dev|development|test)", line)) or ":test" in line
        kind: DependencyKind = "dev" if is_dev else "runtime"
        deps.append(Dependency("rubygems", name, version, kind, manifest_path))
    return deps
