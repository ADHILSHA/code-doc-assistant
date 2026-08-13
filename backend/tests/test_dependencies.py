from __future__ import annotations

from app.parsing.extractors.dependencies import extract_dependencies, is_manifest_filename
from tests.conftest import MINI_REPO


def test_is_manifest_filename():
    assert is_manifest_filename("package.json")
    assert is_manifest_filename("pyproject.toml")
    assert is_manifest_filename("requirements.txt")
    assert is_manifest_filename("requirements-dev.txt")
    assert not is_manifest_filename("setup.py")
    assert not is_manifest_filename("random.txt")


def test_package_json_matches_mini_repo_fixture_exactly():
    content = (MINI_REPO / "package.json").read_text()
    deps = extract_dependencies("package.json", content)
    by_name = {d.name: d for d in deps}

    assert len(deps) == 4
    assert by_name["express"].version_spec == "^4.19.2"
    assert by_name["express"].kind == "runtime"
    assert by_name["react"].kind == "runtime"
    assert by_name["typescript"].kind == "dev"
    assert by_name["vitest"].kind == "dev"
    assert all(d.ecosystem == "npm" for d in deps)


def test_pyproject_toml_matches_mini_repo_fixture_exactly():
    content = (MINI_REPO / "pyproject.toml").read_text()
    deps = extract_dependencies("pyproject.toml", content)
    by_name = {d.name: d for d in deps}

    assert len(deps) == 5
    assert by_name["fastapi"].version_spec == ">=0.115"
    assert by_name["fastapi"].kind == "runtime"
    assert by_name["flask"].kind == "runtime"
    assert by_name["requests"].kind == "runtime"
    assert by_name["pytest"].kind == "dev"
    assert by_name["ruff"].kind == "dev"
    assert all(d.ecosystem == "pypi" for d in deps)


def test_requirements_txt():
    content = "flask==3.0.0\n# a comment\n\nrequests>=2.32\n-e ./local-pkg\n"
    deps = extract_dependencies("requirements.txt", content)
    by_name = {d.name: d for d in deps}
    assert by_name["flask"].version_spec == "==3.0.0"
    assert by_name["requests"].version_spec == ">=2.32"
    assert "local-pkg" not in by_name  # `-e` lines are skipped
    assert all(d.kind == "runtime" for d in deps)


def test_requirements_dev_txt_marks_dev_kind():
    deps = extract_dependencies("requirements-dev.txt", "pytest>=8.0\n")
    assert deps[0].kind == "dev"


def test_pipfile():
    content = """
[packages]
flask = "*"
requests = { version = ">=2.32" }

[dev-packages]
pytest = "*"
"""
    deps = extract_dependencies("Pipfile", content)
    by_name = {d.name: d for d in deps}
    assert by_name["flask"].kind == "runtime"
    assert by_name["requests"].version_spec == ">=2.32"
    assert by_name["pytest"].kind == "dev"


def test_go_mod():
    content = """
module example.com/app

go 1.22

require (
    github.com/gin-gonic/gin v1.9.1
    github.com/stretchr/testify v1.9.0 // indirect
)

require golang.org/x/text v0.14.0
"""
    deps = extract_dependencies("go.mod", content)
    by_name = {d.name: d for d in deps}
    assert by_name["github.com/gin-gonic/gin"].version_spec == "v1.9.1"
    assert by_name["github.com/gin-gonic/gin"].kind == "runtime"
    assert by_name["github.com/stretchr/testify"].kind == "optional"  # indirect
    assert by_name["golang.org/x/text"].version_spec == "v0.14.0"
    assert all(d.ecosystem == "go" for d in deps)


def test_cargo_toml():
    content = """
[dependencies]
serde = "1.0"
tokio = { version = "1.38", features = ["full"] }

[dev-dependencies]
proptest = "1.4"
"""
    deps = extract_dependencies("Cargo.toml", content)
    by_name = {d.name: d for d in deps}
    assert by_name["serde"].version_spec == "1.0"
    assert by_name["tokio"].version_spec == "1.38"
    assert by_name["proptest"].kind == "dev"
    assert all(d.ecosystem == "cargo" for d in deps)


def test_pom_xml():
    content = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
      <version>3.2.0</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""
    deps = extract_dependencies("pom.xml", content)
    by_name = {d.name: d for d in deps}
    assert by_name["org.springframework.boot:spring-boot-starter-web"].version_spec == "3.2.0"
    assert by_name["org.springframework.boot:spring-boot-starter-web"].kind == "runtime"
    assert by_name["junit:junit"].kind == "dev"
    assert all(d.ecosystem == "maven" for d in deps)


def test_build_gradle():
    content = """
dependencies {
    implementation 'com.squareup.okhttp3:okhttp:4.12.0'
    testImplementation 'junit:junit:4.13.2'
}
"""
    deps = extract_dependencies("build.gradle", content)
    by_name = {d.name: d for d in deps}
    assert by_name["com.squareup.okhttp3:okhttp"].version_spec == "4.12.0"
    assert by_name["com.squareup.okhttp3:okhttp"].kind == "runtime"
    assert by_name["junit:junit"].kind == "dev"


def test_gemfile():
    content = """
source "https://rubygems.org"

gem "rails", "7.1.0"
gem "rspec", group: :test
"""
    deps = extract_dependencies("Gemfile", content)
    by_name = {d.name: d for d in deps}
    assert by_name["rails"].version_spec == "7.1.0"
    assert by_name["rails"].kind == "runtime"
    assert by_name["rspec"].kind == "dev"


def test_unknown_manifest_returns_empty():
    assert extract_dependencies("Makefile", "anything") == []


def test_malformed_manifests_return_empty_not_raise():
    assert extract_dependencies("package.json", "{not valid json") == []
    assert extract_dependencies("pyproject.toml", "not [ valid toml") == []
    assert extract_dependencies("pom.xml", "<not valid xml") == []
