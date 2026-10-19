import pytest

import semula

pytestmark = pytest.mark.unit


def test_package_exposes_a_version() -> None:
    assert semula.__version__.count(".") == 2
