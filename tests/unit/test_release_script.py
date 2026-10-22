import importlib.util
import tomllib
from pathlib import Path

import pytest

import semula

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]


def load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


release = load()


def project(tmp_path: Path, *, py="0.1.0", lock="0.1.0", changelog: str | None = None) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "semula"\nversion = "{py}"\n')
    (tmp_path / "uv.lock").write_text(
        f'[[package]]\nname = "other"\nversion = "9.9.9"\n\n'
        f'[[package]]\nname = "semula"\nversion = "{lock}"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(
        changelog
        if changelog is not None
        else "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-22\n\n- First release.\n\n"
        "## [0.0.1] - 2026-01-01\n\n- Earlier.\n"
    )
    return tmp_path


def test_a_release_whose_files_agree_passes(tmp_path: Path) -> None:
    assert release.check("v0.1.0", project(tmp_path)) == "0.1.0"


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0-rc.1", "v0.1.0.1", "latest", ""])
def test_only_plain_version_tags_are_releases(tag: str) -> None:
    with pytest.raises(release.ReleaseError, match="not a release tag"):
        release.version_of(tag)


def test_each_disagreement_is_named(tmp_path: Path) -> None:
    root = project(tmp_path, py="0.2.0", lock="0.0.1")
    with pytest.raises(release.ReleaseError) as caught:
        release.check("v0.1.0", root)
    message = str(caught.value)
    assert "pyproject.toml says 0.2.0" in message
    assert "uv.lock says 0.0.1" in message and "uv lock" in message


def test_a_missing_empty_or_undated_changelog_section_fails(tmp_path: Path) -> None:
    for body, expect in [
        ("# Changelog\n\n## [Unreleased]\n\n- x\n", "no filled section"),
        ("# Changelog\n\n## [0.1.0] - 2026-10-22\n\n## [0.0.1] - 2026-01-01\n\n- x\n", "no filled"),
        ("# Changelog\n\n## [0.1.0]\n\n- x\n", "no release date"),
    ]:
        with pytest.raises(release.ReleaseError, match=expect):
            release.check("v0.1.0", project(tmp_path, changelog=body))


def test_the_notes_are_that_versions_section_only(tmp_path: Path) -> None:
    notes = release.changelog_section("0.1.0", project(tmp_path))
    assert notes == "- First release.\n"
    assert "Earlier" not in notes and "Unreleased" not in notes


def test_a_zero_version_is_a_prerelease_and_still_the_latest_image() -> None:
    assert release.flags("v0.1.0") == {"version": "0.1.0", "prerelease": "true", "latest": "true"}
    assert release.flags("v1.0.0")["prerelease"] == "false"


def test_the_command_line_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["flags", "v0.1.0"]) == 0
    assert "prerelease=true" in capsys.readouterr().out
    assert release.main(["check", "nope"]) == 1
    assert "not a release tag" in capsys.readouterr().err
    assert release.main(["bogus"]) == 2


def test_the_package_version_is_the_one_in_pyproject() -> None:
    """``semula --version`` reads the installed metadata: it must follow pyproject.toml."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert semula.__version__ == declared, "run `uv sync` after changing the version"


def test_the_repository_is_releasable_as_it_stands_once_dated() -> None:
    """The CHANGELOG always has the sections the script reads; the lockfile follows pyproject."""
    declared = release.pyproject_version()
    assert release.locked_version() == declared
    assert (ROOT / "CHANGELOG.md").read_text().startswith("# Changelog")
