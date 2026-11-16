import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]


def load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("fork", ROOT / "scripts" / "fork.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


fork = load()


def tree(root: Path) -> Path:
    files = {
        "README.md": "docker run ghcr.io/leejianrong/semula\nDocs: https://leejianrong.github.io/semula/\n"
        "Code: https://github.com/leejianrong/semula\nBy leejianrong/indah too\n",
        "zensical.toml": 'repo_name = "leejianrong/semula"\n',
        "deploy/a/compose.yaml": "image: ${SEMULA_IMAGE:-ghcr.io/leejianrong/semula:latest}\n",
        "CHANGELOG.md": "The image ghcr.io/leejianrong/semula began here.\n",
        "docs/adr/0001-x.md": "ghcr.io/leejianrong/semula in history\n",
        "uv.lock": 'source = "https://github.com/leejianrong/semula"\n',
        "logo.png": "not text",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "logo.png").write_bytes(b"\x89PNG\xff\xfe")
    return root


def test_a_dry_run_lists_what_would_change_and_touches_nothing(tmp_path: Path) -> None:
    root = tree(tmp_path)
    changed = fork.run(root, "acme/semula-fork", apply=False)
    assert set(changed) == {"README.md", "zensical.toml", "deploy/a/compose.yaml"}
    assert "leejianrong/semula" in (root / "README.md").read_text()


def test_apply_rewrites_the_names_a_fork_must_own(tmp_path: Path) -> None:
    root = tree(tmp_path)
    fork.run(root, "Acme/Semula-Fork", apply=True)
    readme = (root / "README.md").read_text()
    assert "ghcr.io/acme/semula-fork" in readme  # registry names are lowercase
    assert "https://acme.github.io/Semula-Fork/" in readme  # Pages keeps the repo's case
    assert "https://github.com/Acme/Semula-Fork" in readme
    assert "leejianrong/indah" in readme  # a different project is left alone
    assert 'repo_name = "Acme/Semula-Fork"' in (root / "zensical.toml").read_text()
    assert "ghcr.io/acme/semula-fork:latest" in (root / "deploy/a/compose.yaml").read_text()


def test_history_the_lockfile_and_binaries_are_left_alone(tmp_path: Path) -> None:
    root = tree(tmp_path)
    fork.run(root, "acme/semula", apply=True)
    for name in ("CHANGELOG.md", "docs/adr/0001-x.md", "uv.lock"):
        assert "leejianrong" in (root / name).read_text()
    assert (root / "logo.png").read_bytes() == b"\x89PNG\xff\xfe"


def test_running_it_twice_changes_nothing_more(tmp_path: Path) -> None:
    root = tree(tmp_path)
    fork.run(root, "acme/semula", apply=True)
    assert fork.run(root, "acme/semula", apply=True) == []


@pytest.mark.parametrize("bad", ["", "acme", "a/b/c", "acme/", "/semula", "ac me/semula"])
def test_a_name_that_is_not_owner_slash_repo_is_refused(bad: str, tmp_path: Path) -> None:
    with pytest.raises(fork.ForkError):
        fork.run(tmp_path, bad, apply=False)
