import pytest

import semula
from semula.cli import main

pytestmark = pytest.mark.unit


def test_version_prints_the_package_version_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"semula {semula.__version__}"
