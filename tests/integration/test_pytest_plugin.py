"""The pytest plugin (SEM-107): start semula, point OPENAI_BASE_URL at it, fail on a miss."""

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.integration

ASK = """
import httpx, os

def ask(text):
    return httpx.post(os.environ["OPENAI_BASE_URL"] + "/chat/completions", timeout=30, json={
        "model": "semula", "messages": [{"role": "user", "content": text}]})

def seed(semula, text):
    r = httpx.post(semula.url + "/api/v1/projects/default/entries", json={
        "request": {"messages": [{"role": "user", "content": text}]},
        "response": {"content": "hi"}})
    assert r.status_code == 201, r.text
"""


def run(pytester: pytest.Pytester, body: str, *args: str) -> pytest.RunResult:
    pytester.makepyfile(test_llm=ASK + body)
    return pytester.runpytest_subprocess(*args)  # the plugin loads through its entry point


def test_a_recorded_call_passes(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        """
def test_hit(semula):
    seed(semula, "hello")
    assert ask("hello").json()["choices"][0]["message"]["content"] == "hi"
""",
    )
    result.assert_outcomes(passed=1)


def test_a_miss_fails_the_test_and_says_which_call(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        """
def test_miss(semula):
    ask("a prompt nobody recorded")
""",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*1 call(s) had no recorded answer*", "*a prompt nobody recorded*"]
    )


def test_each_test_is_judged_on_its_own_calls(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        """
import pytest

@pytest.mark.semula_allow_misses
def test_expected_miss(semula):
    assert ask("unrecorded").status_code == 424

def test_clean_after_a_miss(semula):
    seed(semula, "hello")
    ask("hello")
""",
    )
    result.assert_outcomes(passed=2)


def test_assert_no_misses_fails_inside_the_test(pytester: pytest.Pytester) -> None:
    result = run(
        pytester,
        """
import pytest

@pytest.mark.semula_allow_misses
def test_inline(semula):
    ask("unrecorded")
    semula.assert_no_misses()
""",
    )
    result.assert_outcomes(failed=1)


def test_a_named_store_keeps_recordings(pytester: pytest.Pytester) -> None:
    store = str(pytester.path / "recordings")
    first = run(
        pytester, "def test_seed(semula):\n    seed(semula, 'hello')\n", "--semula-store", store
    )
    first.assert_outcomes(passed=1)
    second = run(
        pytester,
        "def test_hit(semula):\n    assert ask('hello').status_code == 200\n",
        "--semula-store",
        store,
    )
    second.assert_outcomes(passed=1)
