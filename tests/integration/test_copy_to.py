"""Copy a SQLite deployment into another database, Postgres above all (SEM-127).

The source is always a SQLite file, so under ``make test-postgres`` (SEMULA_TEST_DATABASE_URL set)
the target is a real Postgres schema and this is SQLite -> Postgres; in the fast layer it is
SQLite -> SQLite. Either way every table of ``schema.metadata`` must arrive row for row, tokens
and sessions must still work, and a failure must leave the target empty."""

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from semula import canonical, copydb, schema
from semula.cli import main
from semula.config import Settings
from semula.db import Database, normalise_url
from semula.errors import SemulaError
from semula.oidc import Identity
from semula.scope import Scope
from semula.service import Candidate, Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.integration


def req(text: str) -> Any:
    return canonical.from_openai({"messages": [{"role": "user", "content": text}]})


class Source:
    """A SQLite deployment with something in every table."""

    def __init__(self, tmp_path: Path) -> None:
        self.url = f"sqlite:///{tmp_path / 'source.db'}"
        self.settings = Settings(data_dir=tmp_path, hosted=True, signup="open")
        self.service = Service(Database(self.url), self.settings)
        s = self.service
        s.create_workspace("acme", name="Acme")
        s.create_workspace("rival")
        self.acme = s.for_scope(Scope("acme"))
        self.rival = s.for_scope(Scope("rival"))
        self.acme.create_project("shop")
        self.rival.create_project("other")
        entry, _ = self.acme.upsert_entry(
            "shop", {"messages": [{"role": "user", "content": "hello shop"}]}, {"content": "hi"}
        )
        self.entry = entry["id"]
        self.acme.update_entry(self.entry, entry["version"], {"response": {"content": "hi again"}})
        self.rival.upsert_entry(
            "other", {"messages": [{"role": "user", "content": "rival secret"}]}, {"content": "x"}
        )
        self.pending = self.acme.open_pending(req("nobody has answered this"), "shop")
        self.token = self.acme.create_token("agent", "admin")["token"]
        self.bound = self.acme.create_token("bound", "record", project="shop")["token"]
        self.rival_token = self.rival.create_token("agent", "admin")["token"]
        self.ada = self.person("ada")
        invite = self.acme.create_invitation("ada@x.test", "admin")
        s.accept_invitation(invite["token"], self.ada)
        self.acme.create_invitation("pending@x.test", "viewer")
        self.cookie, _ = s.create_session(self.ada)
        s.create_signup_code(uses=2, note="beta")
        self.code = s.create_signup_code()["code"]
        self.bob = self.person("bob")
        s.redeem_signup_code(self.code, self.bob)
        self.cat = self.person("cat")
        s.signup_open(self.cat, "203.0.113.7")
        started = s.device_start(scope="record")
        s.device_decide(started["user_code"], self.ada, "acme", True)
        s.device_start()  # one still waiting
        self.acme.log_call("hit", time.perf_counter(), project="shop", entry_id=self.entry)
        self.acme.log_call("miss", time.perf_counter(), project="shop")
        s.create_workspace("quiet")
        s.for_scope(Scope("quiet")).suspend_workspace("spam")
        with s.db.begin() as conn:  # the two tables whose writers need a model or a real upstream
            s.db.execute(
                sa.insert(schema.entry_vector).values(
                    workspace_id="acme",
                    entry_id=self.entry,
                    project_id="shop",
                    model="test-model",
                    vector=b"\x00\x01\x02\xff",
                ),
                conn,
            )
            s.db.execute(
                sa.insert(schema.upstream_usage).values(
                    workspace_id="acme", project_id="shop", calls=3,
                    prompt_tokens=100, completion_tokens=50,
                ),
                conn,
            )  # fmt: skip
        self.seed_candidate_and_alias()

    def seed_candidate_and_alias(self) -> None:
        self.acme.record_candidates(req("hello shops"), "shop", [Candidate(self.entry, 0.8)])
        self.acme.record_candidates(req("hello shoppe"), "shop", [Candidate(self.entry, 0.7)])
        first, second = self.acme.list_candidates("shop")
        self.acme.approve_candidate(second["id"])  # makes an alias
        self.acme.reject_candidate(first["id"])

    def person(self, name: str) -> str:
        who = self.service.sign_in_identity(
            Identity("https://idp.test", name, f"{name}@x.test", True, name.title())
        )
        return str(who["id"])


@pytest.fixture
def source(tmp_path: Path) -> Source:
    return Source(tmp_path)


def rows(url: str, table: sa.Table) -> list[tuple[Any, ...]]:
    engine = Database(url, migrate=False).engine
    with engine.connect() as conn:
        found = conn.execute(sa.select(table)).all()
    return sorted((tuple(r) for r in found), key=repr)


def counts(url: str) -> dict[str, int]:
    engine = Database(url, migrate=False).engine
    with engine.connect() as conn:
        return {
            t.name: int(conn.execute(sa.select(sa.func.count()).select_from(t)).scalar() or 0)
            for t in schema.metadata.sorted_tables
        }


# --- coverage: a table cannot be forgotten ---------------------------------------------------


def test_the_seeded_source_has_something_in_every_table(source: Source) -> None:
    """If a table is added to the schema and this fails, seed it above, so the copy test below
    covers it too."""
    empty = [name for name, n in counts(source.url).items() if n == 0]
    assert empty == [], f"nothing seeded in {empty}"


def test_the_copy_covers_every_table_of_the_schema() -> None:
    assert [t.name for t in copydb.tables()] == [t.name for t in schema.metadata.sorted_tables]
    assert {t.name for t in copydb.tables()} == set(schema.metadata.tables)


# --- the copy --------------------------------------------------------------------------------


def test_every_row_of_every_table_arrives_unchanged(tmp_path: Path, source: Source) -> None:
    target = fresh_database_url(tmp_path)
    done = copydb.copy_database(source.url, target)
    assert done.counts == {k: v for k, v in counts(source.url).items()}
    for table in schema.metadata.sorted_tables:
        assert rows(target, table) == rows(source.url, table), table.name


def test_tokens_sessions_and_the_api_still_answer_the_same(tmp_path: Path, source: Source) -> None:
    target = fresh_database_url(tmp_path)
    copydb.copy_database(source.url, target)
    moved = Service(Database(target), Settings(data_dir=tmp_path, hosted=True))
    # the same secrets, the same people
    who = moved.authenticate_token(source.token)
    assert who is not None and who.workspace_id == "acme" and who.scope == "admin"
    bound = moved.authenticate_token(source.bound)
    assert bound is not None and bound.project == "shop"
    rival = moved.authenticate_token(source.rival_token)
    assert rival is not None and rival.workspace_id == "rival"
    session = moved.authenticate_session(source.cookie)
    assert session is not None and session["person_id"] == source.ada
    assert moved.membership_of(source.ada, "acme") == "admin"
    # the same answers
    mine = moved.for_scope(Scope("acme", who))
    old = source.acme
    assert mine.list_projects() == old.list_projects()
    assert mine.get_entry(source.entry) == old.get_entry(source.entry)
    assert [p["id"] for p in mine.list_pending()] == [p["id"] for p in old.list_pending()]
    assert moved.for_scope(Scope("acme")).list_audit() == old.list_audit()  # people-only
    # a token of a suspended workspace stays refused
    quiet = moved.for_scope(Scope("quiet"))
    assert quiet.workspace_status()["suspended_reason"] == "spam"
    # the invitation and the codes are still usable or spent exactly as they were
    assert len(moved.list_signup_codes()) == 2
    with pytest.raises(SemulaError):
        moved.signup_open(source.cat, "203.0.113.7")  # already signed up: the counter came along


def test_new_rows_after_the_copy_continue_the_numbering(tmp_path: Path, source: Source) -> None:
    """Ids were copied, so a Postgres sequence must be moved past them or the next audit row or
    call would collide."""
    target = fresh_database_url(tmp_path)
    copydb.copy_database(source.url, target)
    moved = Service(Database(target), Settings(data_dir=tmp_path, hosted=True))
    acme = moved.for_scope(Scope("acme"))
    before = len(acme.list_audit())
    acme.create_project("later")
    acme.log_call("hit", time.perf_counter(), project="shop")
    assert len(acme.list_audit()) == before + 1


def test_the_quota_counters_and_the_search_index_are_rebuilt(
    tmp_path: Path, source: Source
) -> None:
    target = fresh_database_url(tmp_path)
    # counters in the source drifted: a rebuild after the copy fixes them
    with source.service.db.begin() as conn:
        source.service.db.execute(
            sa.update(schema.workspace_usage)
            .where(schema.workspace_usage.c.workspace_id == "acme")
            .values(entries=99, bytes=12345),
            conn,
        )
    copydb.copy_database(source.url, target)
    moved = Service(Database(target), Settings(data_dir=tmp_path, hosted=True))
    used = moved.for_scope(Scope("acme")).usage()["used"]
    assert used["entries"] == 1 and used["bytes"] != 12345
    # lexical search finds the copied entry (FTS5 rebuilt on SQLite; a scan on Postgres)
    acme = moved.for_scope(Scope("acme"))
    config = acme.project_settings("shop").fingerprint_config()
    hits = acme.lexical.search("acme", "shop", ["hello", "shop"], config, 5)
    assert [h.entry_id for h in hits] == [source.entry]


# --- the refusals ----------------------------------------------------------------------------


def test_a_target_with_data_is_refused_and_left_alone(tmp_path: Path, source: Source) -> None:
    target = fresh_database_url(tmp_path)
    other = Service(Database(target), Settings(data_dir=tmp_path))
    other.create_workspace("mine")
    before = counts(target)
    with pytest.raises(copydb.TargetNotEmpty):
        copydb.copy_database(source.url, target)
    assert counts(target) == before


def test_a_freshly_migrated_target_counts_as_empty(tmp_path: Path, source: Source) -> None:
    target = fresh_database_url(tmp_path)
    Database(target)  # the compose project's migrate step already ran
    done = copydb.copy_database(source.url, target)
    assert done.counts["workspace"] == counts(source.url)["workspace"]


def test_the_copy_cannot_be_run_twice(tmp_path: Path, source: Source) -> None:
    target = fresh_database_url(tmp_path)
    copydb.copy_database(source.url, target)
    with pytest.raises(copydb.TargetNotEmpty):
        copydb.copy_database(source.url, target)


def test_copying_a_database_onto_itself_is_refused(source: Source) -> None:
    with pytest.raises(copydb.CopyError, match="same"):
        copydb.copy_database(source.url, source.url)


def test_a_source_that_is_not_up_to_date_is_refused(tmp_path: Path, source: Source) -> None:
    with Database(source.url).engine.begin() as conn:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = '0001'"))
    with pytest.raises(copydb.CopyError, match="migrate"):
        copydb.copy_database(source.url, fresh_database_url(tmp_path))


def test_a_failure_part_way_leaves_the_target_empty(
    tmp_path: Path, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fresh_database_url(tmp_path)
    Database(target)
    before = counts(target)
    real = copydb._copy_table
    seen: list[str] = []

    def explode(conn: Any, src: Any, table: sa.Table) -> int:
        seen.append(table.name)
        if table.name == "entry":  # after the parents, before the end
            raise RuntimeError("disk on fire")
        return int(real(conn, src, table))

    monkeypatch.setattr(copydb, "_copy_table", explode)
    with pytest.raises(RuntimeError, match="disk on fire"):
        copydb.copy_database(source.url, target)
    assert "workspace" in seen and "entry" in seen
    assert counts(target) == before  # not even the seeded default workspace changed
    monkeypatch.undo()
    copydb.copy_database(source.url, target)  # and it can be tried again


def test_a_row_the_target_refuses_is_reported_by_table_without_quoting_it(
    tmp_path: Path, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fresh_database_url(tmp_path)
    Database(target)
    before = counts(target)

    def refuse(conn: Any, src: Any, table: sa.Table) -> int:
        if table.name == "person":
            raise sa.exc.DataError("INSERT", {}, ValueError("Key (email)=(ada@x.test) is bad"))
        return 0

    monkeypatch.setattr(copydb, "_copy_table", refuse)
    with pytest.raises(copydb.CopyError) as caught:
        copydb.copy_database(source.url, target)
    assert "person" in str(caught.value) and "ada@x.test" not in str(caught.value)
    assert counts(target) == before


def test_a_row_count_mismatch_fails_loudly_and_rolls_back(
    tmp_path: Path, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fresh_database_url(tmp_path)
    Database(target)
    before = counts(target)
    real = copydb._copy_table

    def lose_a_row(conn: Any, src: Any, table: sa.Table) -> int:
        n = int(real(conn, src, table))
        if table.name == "api_token":
            conn.execute(table.delete().where(table.c.name == "bound"))
        return n

    monkeypatch.setattr(copydb, "_copy_table", lose_a_row)
    with pytest.raises(copydb.CopyMismatch, match="api_token"):
        copydb.copy_database(source.url, target)
    assert counts(target) == before


def test_the_password_is_never_in_an_error(tmp_path: Path, source: Source) -> None:
    with pytest.raises(copydb.CopyError) as caught:
        copydb.copy_database(source.url, "postgresql://u:hunter2secret@127.0.0.1:1/none")
    assert "hunter2secret" not in str(caught.value)


# --- the command -----------------------------------------------------------------------------


def test_the_command_copies_and_prints_the_counts(
    tmp_path: Path,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = fresh_database_url(tmp_path)
    monkeypatch.setenv("SEMULA_DATABASE_URL", source.url)
    monkeypatch.setenv("SEMULA_DATA_DIR", str(tmp_path))
    assert main(["--json", "admin", "copy-to", "--database-url", target]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["tables"]["entry"] == 2 and out["tables"]["api_token"] == 3
    assert main(["admin", "copy-to", "--database-url", target]) == 4  # not empty any more
    assert "not empty" in capsys.readouterr().err.lower()


def test_the_command_can_take_the_target_from_the_environment(
    tmp_path: Path,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So the password need not be in a process list."""
    target = fresh_database_url(tmp_path)
    monkeypatch.setenv("SEMULA_DATABASE_URL", source.url)
    monkeypatch.setenv("SEMULA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SEMULA_COPY_TO_URL", target)
    assert main(["admin", "copy-to"]) == 0
    assert "copied" in capsys.readouterr().out.lower()
    monkeypatch.delenv("SEMULA_COPY_TO_URL")
    assert main(["admin", "copy-to"]) == 2  # nowhere to copy to


# --- Postgres: the role the app really connects as ------------------------------------------


def test_the_app_role_keeps_working_after_the_copy(tmp_path: Path, source: Source) -> None:
    """Row-level security must still apply to the app's role on the copied data, and the role must
    be able to write (so sequences and grants survived)."""
    target = fresh_database_url(tmp_path)
    if not target.startswith("postgresql"):
        pytest.skip("needs SEMULA_TEST_DATABASE_URL (make test-postgres)")
    copydb.copy_database(source.url, target)
    schema_name = sa.engine.make_url(target).query["options"].split("=")[-1]
    role = f"semula_app_{uuid.uuid4().hex[:8]}"
    owner = sa.create_engine(normalise_url(target), future=True)
    app_url = sa.engine.make_url(normalise_url(target)).set(username=role, password="pw")  # noqa: S106
    try:
        with owner.begin() as conn:
            conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOSUPERUSER NOBYPASSRLS"))
            conn.execute(sa.text(f'GRANT USAGE ON SCHEMA "{schema_name}" TO {role}'))
            conn.execute(
                sa.text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema_name}"'
                    f" TO {role}"
                )
            )
            conn.execute(
                sa.text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema_name}" TO {role}')
            )
        app = Service(
            Database(app_url.render_as_string(hide_password=False), migrate=False),
            Settings(data_dir=tmp_path, hosted=True),
        )
        who = app.authenticate_token(source.token)
        assert who is not None
        mine = app.for_scope(Scope("acme", who))
        assert {p["id"] for p in mine.list_projects()} == {"default", "shop"}
        mine.create_project("written-by-the-app-role")
        mine.log_call("hit", time.perf_counter(), project="shop")  # a sequence-backed insert
        # and another workspace's rows are still invisible to a query that forgot its filter
        leaked = app.db.for_workspace("acme").rows(sa.select(schema.entry.c.workspace_id))
        assert {r["workspace_id"] for r in leaked} == {"acme"}
    finally:
        with owner.begin() as conn:
            conn.execute(sa.text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema_name}" FROM {role}'))
            conn.execute(
                sa.text(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema_name}" FROM {role}')
            )
            conn.execute(sa.text(f'REVOKE ALL ON SCHEMA "{schema_name}" FROM {role}'))
            conn.execute(sa.text(f"DROP ROLE IF EXISTS {role}"))
        owner.dispose()


def test_copying_as_the_app_role_is_refused_with_a_clear_message(
    tmp_path: Path, source: Source
) -> None:
    """The app role is subject to row-level security, so it could not insert the copy."""
    target = fresh_database_url(tmp_path)
    if not target.startswith("postgresql"):
        pytest.skip("needs SEMULA_TEST_DATABASE_URL (make test-postgres)")
    Database(target)
    schema_name = sa.engine.make_url(normalise_url(target)).query["options"].split("=")[-1]
    role = f"semula_app_{uuid.uuid4().hex[:8]}"
    owner = sa.create_engine(normalise_url(target), future=True)
    app_url = sa.engine.make_url(normalise_url(target)).set(username=role, password="pw")  # noqa: S106
    try:
        with owner.begin() as conn:
            conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' NOBYPASSRLS"))
            conn.execute(sa.text(f'GRANT USAGE ON SCHEMA "{schema_name}" TO {role}'))
            conn.execute(
                sa.text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema_name}"'
                    f" TO {role}"
                )
            )
        with pytest.raises(copydb.CopyError, match="owns"):
            copydb.copy_database(source.url, app_url.render_as_string(hide_password=False))
        assert sum(counts(target).values()) == 3  # untouched: just the seeded default workspace
    finally:
        with owner.begin() as conn:
            conn.execute(sa.text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema_name}" FROM {role}'))
            conn.execute(sa.text(f'REVOKE ALL ON SCHEMA "{schema_name}" FROM {role}'))
            conn.execute(sa.text(f"DROP ROLE IF EXISTS {role}"))
        owner.dispose()


def test_a_server_runs_on_the_copy(tmp_path: Path, source: Source) -> None:
    """End to end: a real server on the copied database accepts the old token over HTTP."""
    import httpx

    target = fresh_database_url(tmp_path)
    copydb.copy_database(source.url, target)
    with running_server(tmp_path, hosted=True, database_url=target, mode="replay") as url:
        ok = httpx.get(
            url + "/api/v1/projects", headers={"Authorization": f"Bearer {source.token}"}
        )
        assert ok.status_code == 200
        assert {p["id"] for p in ok.json()["projects"]} == {"default", "shop"}
        bound = httpx.get(
            url + "/api/v1/projects", headers={"Authorization": f"Bearer {source.bound}"}
        )
        assert [p["id"] for p in bound.json()["projects"]] == ["shop"]
        wrong = httpx.get(url + "/api/v1/projects", headers={"Authorization": "Bearer smla_live_x"})
        assert wrong.status_code == 401
