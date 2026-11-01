# -*- coding: utf-8 -*-
"""续费域 PG 回归（提案 §4.2；PG-01 交付）。

真连 PG16：`cost` 是 NUMERIC、写操作取续费域锁、`sort_order` 压实必须原子。
"""
import datetime
import pathlib
import sys
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import money  # noqa: E402
import pg_locks  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import pgdb  # noqa: E402
import renewals  # noqa: E402


@pytest.fixture()
def pg_env(pg_container):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_test_renewals_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = pg_container.dsn(dbname=name)
    conn = pg_test_support.connect(dsn, autocommit=True)
    try:
        pg_migrations.apply(conn)
    finally:
        conn.close()

    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": name,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        import os

        os.environ[key] = value
    pgdb.close_pool()
    try:
        yield dsn
    finally:
        pgdb.close_pool()
        for key in values:
            import os

            os.environ.pop(key, None)
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _payload(**overrides):
    data = {
        "name": "example.com",
        "kind": "domain",
        "expires_at": "2026-12-31",
        "cost": 12.0,
        "currency": "USD",
        "cycle": "yearly",
    }
    data.update(overrides)
    return data


def test_create_list_get_update_delete(pg_env):
    created = renewals.create_renewal(_payload())
    assert created["name"] == "example.com" and created["sort_order"] == 1
    rid = created["id"]
    rows = renewals.list_renewals()
    assert len(rows) == 1 and rows[0]["kind"] == "domain"

    updated = renewals.update_renewal(rid, {"name": "example.net", "cost": 20})
    assert updated["name"] == "example.net" and updated["cost"] == 20

    assert renewals.delete_renewal(rid) is True
    assert renewals.list_renewals() == []


def test_lifecycle_preserves_history_and_can_resume(pg_env):
    row = renewals.create_renewal(_payload(auto_renew=True, expires_at="2026-01-01", note="keep"))
    assert row["status"] == "active"
    for status in ("not_renewing", "ended"):
        updated = renewals.update_renewal(row["id"], {"status": status})
        assert updated["status"] == status
        assert updated["note"] == "keep" and updated["cost"] == row["cost"]
        assert updated["expires_at"] == row["expires_at"]
        assert renewals.project_expiry(updated, datetime.date(2026, 9, 25)) == "2026-01-01"
        assert len(renewals.list_renewals()) == 1
    restored = renewals.update_renewal(row["id"], {"status": "active"})
    assert renewals.project_expiry(restored, datetime.date(2026, 9, 25)) == "2027-01-01"
    for bad in (None, "", [], "cancelled"):
        with pytest.raises(ValueError, match="status"):
            renewals.update_renewal(row["id"], {"name": "lost", "status": bad})
        assert renewals.get_renewal(row["id"])["name"] == row["name"]
        with pytest.raises(ValueError, match="status"):
            renewals.create_renewal(_payload(status=bad))


def test_cost_is_numeric_decimal_in_database_and_number_in_response(pg_env):
    created = renewals.create_renewal(_payload(cost="12.5"))
    with pgdb.pg_transaction(readonly=True) as conn:
        stored = conn.execute(
            "SELECT cost FROM renewals WHERE id=%s", (created["id"],)
        ).fetchone()["cost"]
    assert isinstance(stored, money.Decimal)
    assert money.canonical(stored) == "12.5"
    assert created["cost"] == 12.5


def test_create_requires_name_and_expires_at(pg_env):
    with pytest.raises(ValueError):
        renewals.create_renewal({"name": "x"})


def test_invalid_kind_and_cycle_rejected(pg_env):
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(kind="invalid"))
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(kind=[]))
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(cycle="fortnightly"))
    created = renewals.create_renewal(_payload(id="cycle-row"))
    with pytest.raises(ValueError):
        renewals.update_renewal(created["id"], {"cycle": "weekly"})


def test_blank_cycle_still_allowed_for_legacy_rows(pg_env):
    created = renewals.create_renewal(_payload(id="no-cycle", cycle=""))
    assert created["cycle"] is None


def test_provider_round_trip_trim_clear_and_validate(pg_env):
    created = renewals.create_renewal(
        _payload(id="provider-row", provider="  AWS  ", auto_renew=True)
    )
    assert created["provider"] == "AWS" and created["auto_renew"] is True
    updated = renewals.update_renewal("provider-row", {"provider": "  阿里云  "})
    assert updated["provider"] == "阿里云"
    cleared = renewals.update_renewal("provider-row", {"provider": "   "})
    assert cleared["provider"] is None
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(id="bad-provider", provider=[]))
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(id="long-provider", provider="x" * 65))


def test_topup_allows_open_term_but_others_require_expiry(pg_env):
    topup = renewals.create_renewal(
        _payload(id="topup-row", kind="other", cycle="topup", expires_at=None)
    )
    assert topup["expires_at"] is None
    with pytest.raises(ValueError):
        renewals.create_renewal(_payload(id="yearly-no-expiry", cycle="yearly", expires_at=None))


def test_switching_to_topup_can_clear_expiry_and_back_again(pg_env):
    renewals.create_renewal(_payload(id="switch-row", cycle="yearly"))
    cleared = renewals.update_renewal("switch-row", {"cycle": "topup", "expires_at": None})
    assert cleared["expires_at"] is None
    with pytest.raises(ValueError):
        renewals.update_renewal("switch-row", {"cycle": "yearly"})
    restored = renewals.update_renewal(
        "switch-row", {"cycle": "yearly", "expires_at": "2028-01-01"}
    )
    assert restored["expires_at"] == "2028-01-01"


def test_open_term_rows_sort_last(pg_env):
    renewals.create_renewal(
        _payload(id="open-term", kind="other", cycle="topup", expires_at=None)
    )
    renewals.create_renewal(_payload(id="far", expires_at="2030-01-01"))
    renewals.create_renewal(_payload(id="near", expires_at="2026-01-01"))
    rows = renewals.list_renewals()
    assert [row["id"] for row in rows] == ["near", "far", "open-term"]


def test_due_within_filters_by_expiry_window_and_excludes_open_term(pg_env):
    today = datetime.date.today()
    renewals.create_renewal(
        _payload(name="soon", expires_at=(today + datetime.timedelta(days=5)).isoformat())
    )
    renewals.create_renewal(
        _payload(name="later", expires_at=(today + datetime.timedelta(days=60)).isoformat())
    )
    renewals.create_renewal(
        _payload(name="past", expires_at=(today - datetime.timedelta(days=1)).isoformat())
    )
    renewals.create_renewal(
        _payload(id="open-term", kind="other", cycle="topup", expires_at=None)
    )
    rows = renewals.list_renewals(due_within=30)
    assert {row["name"] for row in rows} == {"soon"}


def test_create_move_delete_and_reorder_compacts_kind_order(pg_env):
    domain_a = renewals.create_renewal(_payload(id="domain-a", name="domain a"))
    domain_b = renewals.create_renewal(_payload(id="domain-b", name="domain b"))
    cert_a = renewals.create_renewal(_payload(id="cert-a", name="cert a", kind="cert"))
    assert (domain_a["sort_order"], domain_b["sort_order"], cert_a["sort_order"]) == (1, 2, 1)

    moved = renewals.update_renewal("domain-b", {"kind": "cert"})
    assert moved["sort_order"] == 2

    domain_c = renewals.create_renewal(_payload(id="domain-c", name="domain c"))
    assert domain_c["sort_order"] == 2

    assert renewals.delete_renewal("domain-a") is True
    assert renewals.get_renewal("domain-c")["sort_order"] == 2

    rows = renewals.reorder_renewals("domain", ["domain-c"])
    assert [(row["id"], row["sort_order"]) for row in rows] == [("domain-c", 1)]


def test_reorder_rejects_invalid_full_set_without_partial_write(pg_env):
    renewals.create_renewal(_payload(id="domain-a", name="domain a"))
    renewals.create_renewal(_payload(id="domain-b", name="domain b"))
    renewals.create_renewal(_payload(id="cert-a", name="cert a", kind="cert"))
    for kind, ids in (
        ("invalid", ["domain-a", "domain-b"]),
        ([], ["domain-a", "domain-b"]),
        ("domain", "domain-a"),
        ("domain", ["domain-a", "domain-a"]),
        ("domain", ["domain-a"]),
        ("domain", ["domain-a", "cert-a"]),
        ("domain", ["domain-a", "missing"]),
    ):
        with pytest.raises(ValueError):
            renewals.reorder_renewals(kind, ids)
    rows = {row["id"]: row["sort_order"] for row in renewals.list_renewals()}
    assert rows == {"domain-a": 1, "domain-b": 2, "cert-a": 1}


def test_reorder_lock_serialises_against_concurrent_writer(pg_env):
    """另一连接持续费锁时，重排必须在有界超时内失败（提案 §4.2）。"""
    renewals.create_renewal(_payload(id="domain-a"))
    holder = pg_test_support.connect(pg_env)
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, pg_locks.RENEWALS.key),
        )
        with pytest.raises(pgdb.StorageUnavailable):
            renewals.reorder_renewals("domain", ["domain-a"])
        holder.rollback()
    finally:
        holder.close()
    assert renewals.reorder_renewals("domain", ["domain-a"])[0]["sort_order"] == 1


def test_sync_probe_cert_updates_probe_only(pg_env):
    manual = renewals.create_renewal(
        {
            "name": "manual cert",
            "kind": "cert",
            "expires_at": "2026-01-01",
            "url": "https://svc.example.com",
            "source": "manual",
        }
    )
    probe = renewals.create_renewal(
        {
            "name": "probe cert",
            "kind": "cert",
            "expires_at": "2026-01-01",
            "url": "https://svc.example.com:443/path",
            "source": "probe",
        }
    )
    assert renewals.sync_probe_cert("https://svc.example.com", 45) == 1
    assert renewals.get_renewal(manual["id"])["expires_at"] == "2026-01-01"
    assert renewals.get_renewal(probe["id"])["expires_at"] != "2026-01-01"


def test_update_not_found_and_delete_not_found(pg_env):
    assert renewals.update_renewal("missing", {"name": "x"}) is None
    assert renewals.delete_renewal("missing") is False
