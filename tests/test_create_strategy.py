"""Database cloning strategy configuration and fixture integration tests."""

from collections.abc import Callable

import pytest
from packaging.version import Version
from psycopg import AsyncConnection, Connection

from pytest_postgresql.executors import PostgreSQLExecutor
from pytest_postgresql.factories import postgresql, postgresql_async, postgresql_noproc, postgresql_proc
from tests.loader import load_database


@pytest.mark.parametrize(
    ("ini_strategy", "cli_strategy", "expected"),
    [
        (None, None, None),
        ("clone", None, "clone"),
        (None, "file_copy", "file_copy"),
        ("clone", "wal_log", "wal_log"),
    ],
)
def test_create_strategy_configuration(
    pytester: pytest.Pytester, ini_strategy: str | None, cli_strategy: str | None, expected: str | None
) -> None:
    """The CLI overrides ini configuration, and the unset default is None."""
    if ini_strategy:
        pytester.makeini(f"[pytest]\npostgresql_create_strategy = {ini_strategy}\n")
    pytester.makepyfile(
        "from pytest_postgresql.config import get_config\n"
        "def test_strategy(request):\n"
        f"    assert get_config(request).create_strategy == {expected!r}\n"
    )
    args = [f"--postgresql-create-strategy={cli_strategy}"] if cli_strategy else []
    pytester.runpytest(*args).assert_outcomes(passed=1)


@pytest.mark.parametrize("source", ["cli", "ini"])
def test_invalid_create_strategy_configuration(pytester: pytest.Pytester, source: str) -> None:
    """Invalid CLI and ini values produce a clear configuration error."""
    pytester.makepyfile(
        "from pytest_postgresql.config import get_config\ndef test_strategy(request):\n    get_config(request)\n"
    )
    if source == "cli":
        result = pytester.runpytest("--postgresql-create-strategy=invalid")
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines(["*invalid choice: 'invalid'*"])
    else:
        pytester.makeini("[pytest]\npostgresql_create_strategy = invalid\n")
        result = pytester.runpytest()
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*UsageError: Invalid PostgreSQL create_strategy 'invalid'*"])


@pytest.mark.parametrize("factory", [postgresql_proc, postgresql_noproc])
def test_invalid_factory_create_strategy(factory: Callable[..., object]) -> None:
    """Factory arguments reject misspelled strategies immediately."""
    with pytest.raises(ValueError, match="Invalid PostgreSQL create_strategy 'invalid'"):
        factory(create_strategy="invalid")


@pytest.mark.parametrize("factory_name", ["postgresql_proc", "postgresql_noproc"])
@pytest.mark.parametrize("factory_strategy", [None, "clone"])
def test_factory_create_strategy_precedence(
    postgresql_proc: PostgreSQLExecutor,
    pytester: pytest.Pytester,
    factory_name: str,
    factory_strategy: str | None,
) -> None:
    """Both factories propagate the chosen setting to clients, with arguments taking precedence."""
    pytester.makeini("[pytest]\npostgresql_create_strategy = file_copy\n")
    kwargs: dict[str, object] = {"create_strategy": factory_strategy}
    if factory_name == "postgresql_noproc":
        kwargs.update(
            host=postgresql_proc.host,
            port=postgresql_proc.port,
            user=postgresql_proc.user,
            dbname="strategy_noproc",
        )
    expected = factory_strategy or "wal_log"
    pytester.makeconftest(
        f"from pytest_postgresql import factories\nconfigured_proc = factories.{factory_name}(**{kwargs!r})\n"
    )
    pytester.makepyfile(
        f"def test_strategy(configured_proc):\n    assert configured_proc.create_strategy == {expected!r}\n"
    )
    pytester.runpytest("--postgresql-create-strategy=wal_log").assert_outcomes(passed=1)


strategy_proc = postgresql_proc(load=[load_database], create_strategy="wal_log")


@pytest.fixture(scope="session")
def supported_strategy_proc(strategy_proc: PostgreSQLExecutor) -> PostgreSQLExecutor:
    """Skip explicit-strategy integration on PostgreSQL versions predating the option."""
    if strategy_proc.version < Version("15"):
        pytest.skip("Explicit CREATE DATABASE strategies require PostgreSQL 15+")
    return strategy_proc


strategy_noproc = postgresql_noproc(depends_on="supported_strategy_proc", create_strategy="wal_log")
strategy_client = postgresql("supported_strategy_proc")
strategy_async_client = postgresql_async("strategy_noproc")


def test_client_clones_with_create_strategy(strategy_client: Connection) -> None:
    """A sync client clones schema and data using the configured process strategy."""
    with strategy_client.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM stories")
        assert cursor.fetchone() == (4,)


@pytest.mark.asyncio
async def test_async_client_clones_with_create_strategy(strategy_async_client: AsyncConnection) -> None:
    """An async client honors the strategy from a layered noproc fixture."""
    async with strategy_async_client.cursor() as cursor:
        await cursor.execute("SELECT count(*) FROM stories")
        assert await cursor.fetchone() == (4,)
