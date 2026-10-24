"""schema.sql derives each existing tenant's home_calling_code from its own numbers (#168).

A flat default of '1' would turn every +91 destination of an Indian tenant into an
"international" one and break it on upgrade. Each test builds a database the way it looks
before this release (no home_calling_code column, live data), then applies schema.sql as an
upgrade does.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
import warnings
from pathlib import Path

import asyncpg
import pytest

from libs.config_sdk.calling_codes import CALLING_CODES
from libs.config_sdk.dial_targets import TransferPolicy, transfer_policy_problem
from services.config.tenants import transfer_policy

REPO = Path(__file__).resolve().parents[3]
SCHEMA = REPO / "database" / "schema.sql"
_BASE = os.environ["POSTGRES_DSN"].rsplit("/", 1)[0]


def _psql(database: str, *args: str, timeout: float | None = None) -> None:
    subprocess.run(
        ["psql", f"{_BASE}/{database}", "-q", "-v", "ON_ERROR_STOP=1", *args],
        check=True, capture_output=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def scratch_db():
    name = f"home_code_scratch_{uuid.uuid4().hex[:8]}"
    _psql("postgres", "-c", f"CREATE DATABASE {name}")
    _psql(name, "-f", str(SCHEMA))
    yield name
    try:
        _psql("postgres", "-c", f"DROP DATABASE IF EXISTS {name}", timeout=5)
    except subprocess.TimeoutExpired:
        warnings.warn(f"scratch database {name} was not dropped: DROP DATABASE did not return in 5s")


@pytest.fixture
async def conn(scratch_db):
    """Connection to a database holding the pre-release shape: no home_calling_code."""
    c = await asyncpg.connect(f"{_BASE}/{scratch_db}")
    await c.execute("TRUNCATE tenants CASCADE")
    await c.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS home_calling_code")
    yield c
    await c.close()


def _upgrade(scratch_db: str) -> None:
    _psql(scratch_db, "-f", str(SCHEMA))


async def _tenant(conn, dids: tuple[str, ...] = (), destinations: tuple[str, ...] = ()) -> uuid.UUID:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    tenant = await conn.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING id", slug)
    for did in dids:
        await conn.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", did, tenant)
    for i, destination in enumerate(destinations):
        await conn.execute(
            "INSERT INTO agents (tenant_id, slug, name, transfer_type, transfer_destination) "
            "VALUES ($1, $2, 'a', 'cold', $3)", tenant, f"a{i}", destination,
        )
    return tenant


async def _home(conn, tenant) -> str:
    return await conn.fetchval("SELECT home_calling_code FROM tenants WHERE id = $1", tenant)


async def test_indian_tenant_keeps_its_plus91_destinations(scratch_db, conn):
    destinations = ("+918012345678", "00918098765432", "1001")
    tenant = await _tenant(conn, dids=("918065354620", "918065354621"), destinations=destinations)
    _upgrade(scratch_db)
    row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant)
    assert row["home_calling_code"] == "91"
    for destination in destinations:
        assert transfer_policy_problem(destination, transfer_policy(dict(row))) is None, destination
    # The same destinations under the old flat default are what broke.
    assert transfer_policy_problem("+918012345678", TransferPolicy()) is not None


async def test_home_code_comes_from_dids_first_then_destinations(scratch_db, conn):
    from_dids = await _tenant(conn, dids=("+91 80-6535 4620", "918065354621", "442071234567"), destinations=("+442071234567",))
    from_dests = await _tenant(conn, destinations=("+442071234567", "0044207123456", "+918012345678"))
    only_extensions = await _tenant(conn, destinations=("1001", "sip:desk@pbx.example.com"))
    no_data = await _tenant(conn)
    us = await _tenant(conn, dids=("14155550100",))
    _upgrade(scratch_db)
    assert await _home(conn, from_dids) == "91"
    assert await _home(conn, from_dests) == "44"
    assert await _home(conn, only_extensions) == "1"
    assert await _home(conn, no_data) == "1"
    assert await _home(conn, us) == "1"


async def test_reapplying_schema_keeps_a_code_set_since(scratch_db, conn):
    tenant = await _tenant(conn, dids=("918065354620",))
    _upgrade(scratch_db)
    await conn.execute("UPDATE tenants SET home_calling_code = '44' WHERE id = $1", tenant)
    _upgrade(scratch_db)
    assert await _home(conn, tenant) == "44"


def test_sql_calling_code_list_equals_the_python_table():
    block = re.search(r"unnest\(ARRAY\[(.*?)\]\)", SCHEMA.read_text(), re.S)
    assert block is not None, "calling-code list not found in schema.sql"
    assert set(re.findall(r"'(\d+)'", block.group(1))) == CALLING_CODES
