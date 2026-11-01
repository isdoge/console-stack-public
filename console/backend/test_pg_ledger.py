# -*- coding: utf-8 -*-
"""账本域 PG 回归（提案 §4.2/§5.4；PG-01 交付）。

这些测试**真连 PG16**：金额 NUMERIC、账本锁串行化、具名列、双连接并发都只有真库能证明。
数据全部是合成流水，绝不触碰真实账本。
"""
import pathlib
import sys
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import ledger  # noqa: E402
import money  # noqa: E402
import pg_locks  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import pgdb  # noqa: E402


@pytest.fixture()
def ledger_db(pg_container):
    """每个测试一个独立数据库：schema12 + 基础字典，互不共享状态。"""
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_test_ledger_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = pg_container.dsn(dbname=name)
    conn = pg_test_support.connect(dsn, autocommit=True)
    try:
        pg_migrations.apply(conn)
        conn.execute(
            "INSERT INTO ledger_categories(level1,level2,sort) VALUES"
            " ('餐饮','三餐',1),('生活','日用',2),('收入','工资',3)"
        )
    finally:
        conn.close()
    yield name
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture()
def pg_env(pg_container, ledger_db, monkeypatch):
    """把 pgdb 指向隔离库；每个测试前后重置池。"""
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": ledger_db,
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
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    # 交出**本测试库**的 DSN：advisory lock 是 per-database 的，拿容器默认库的 DSN
    # 去持锁根本锁不住业务连接，会让并发测试假通过。
    yield pg_container.dsn(dbname=ledger_db)
    pgdb.close_pool()


def add(**values):
    payload = {
        "date": "2026-08-12",
        "direction": "支出",
        "amount": 30,
        "who": "示例商户",
        "goods": "示例商品",
        **values,
    }
    return ledger.manual(payload)["id"]


# --- 金额与基础 CRUD ---------------------------------------------------------

def test_manual_amount_is_stored_as_numeric_and_returned_as_json_number(pg_env):
    tid = add(amount=12.5)
    with pgdb.pg_transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT amount FROM ledger_tx WHERE id=%s", (tid,)
        ).fetchone()
    assert isinstance(row["amount"], money.Decimal)
    assert money.canonical(row["amount"]) == "12.5"

    page = ledger.transactions({})
    assert page["total"] == 1
    assert page["rows"][0]["amount"] == 12.5
    assert isinstance(page["rows"][0]["amount"], float)


def test_manual_refund_is_negative_expense(pg_env):
    tid = add(direction="退款", amount=12.5)
    row = ledger.transactions({})["rows"][0]
    assert row["id"] == tid
    assert row["direction"] == "支出" and row["amount"] == -12.5


@pytest.mark.parametrize(
    "values",
    [
        {"amount": "NaN"},
        {"amount": True},
        {"amount": 0},
        {"amount": -1},
        {"amount": 10**13},
        {"date": "2026-02-30"},
        {"time": "25:00"},
        {"type1": "已删除", "type2": "类别"},
    ],
)
def test_invalid_manual_input_never_writes(pg_env, values):
    with pytest.raises(ValueError):
        add(**values)
    assert ledger.transactions({})["total"] == 0


def test_category_teaches_and_follows_only_unclassified_rows_and_refund(pg_env):
    first = add()
    following = add()
    classified = add(type1="生活", type2="日用")
    refund = add(direction="退款", goods="退款项目")
    ledger.offset({"a": first, "b": refund})
    result = ledger.set_category(first, {"type1": "餐饮", "type2": "三餐"})
    assert result["followed"] == 2
    rows = {row["id"]: row for row in ledger.transactions({})["rows"]}
    assert rows[following]["type1"] == rows[refund]["type1"] == "餐饮"
    assert rows[classified]["type1"] == "生活"
    with pgdb.pg_transaction(readonly=True) as conn:
        assert (
            conn.execute("SELECT type1 FROM ledger_knowledge").fetchone()["type1"]
            == "餐饮"
        )


@pytest.mark.parametrize("income,mode,expense", [(30, "full", 0), (12, "partial", 18)])
def test_offsets_really_remove_income_and_reduce_net_expense(pg_env, income, mode, expense):
    a = add()
    b = add(direction="收入", amount=income)
    assert ledger.offset({"a": a, "b": b})["mode"] == mode
    stats = ledger.overview({"year": "2026", "month": "2026-08"})
    assert (stats["month_income"], stats["month_expense"]) == (0, expense)
    with pytest.raises((ValueError, FileExistsError)):
        ledger.offset({"a": a, "b": b})


def test_two_refunds_and_self_offset_rejected_without_mutation(pg_env):
    a = add(direction="退款")
    b = add(direction="退款")
    for pair in [(a, a), (a, b)]:
        with pytest.raises(ValueError):
            ledger.offset({"a": pair[0], "b": pair[1]})
    assert ledger.overview({"year": "2026"})["year_expense"] == -60


def test_multi_way_aa_offset_links_each_income_to_anchor(pg_env):
    expense = add(amount=100)
    income_a = add(direction="收入", amount=60)
    income_b = add(direction="收入", amount=40)
    result = ledger.offset({"ids": [income_a, expense, income_b]})
    assert result["mode"] == "full"
    assert result["offset"] == 2
    assert result["offset_ids"] == [income_a, income_b]
    assert result["kept"] == expense
    rows = {row["id"]: row for row in ledger.transactions({})["rows"]}
    assert rows[income_a]["offset_done"]["other_id"] == expense
    assert rows[income_b]["offset_done"]["other_id"] == expense
    assert rows[expense]["offset_done"]["other_id"] == min(income_a, income_b)
    assert [peer["other_id"] for peer in rows[expense]["offset_peers"]] == sorted(
        [income_a, income_b]
    )
    assert all(peer["other_amount"] < 0 for peer in rows[expense]["offset_peers"])
    # 跨页完整性：锚点单独落在第 3 页时，页外收入的关联仍全量返回。
    tail = ledger.transactions({"page": 3, "size": 1})["rows"][0]
    assert tail["id"] == expense
    assert sorted(peer["other_id"] for peer in tail["offset_peers"]) == sorted(
        [income_a, income_b]
    )
    with pgdb.pg_transaction(readonly=True) as con:
        links = con.execute(
            "SELECT a,b,rel FROM ledger_tx_link WHERE rel IN ('offset','aa_offset') ORDER BY a,b"
        ).fetchall()
    assert [(link["a"], link["b"], link["rel"]) for link in links] == sorted(
        [(min(expense, i), max(expense, i), "offset") for i in (income_a, income_b)]
    )


def test_dictionary_rename_is_atomic_and_deletion_protects_knowledge(pg_env):
    a = add()
    ledger.set_category(a, {"type1": "餐饮", "type2": "三餐"})
    ledger.rename_category(
        {"old1": "餐饮", "old2": "三餐", "new1": "餐饮", "new2": "正餐"}
    )
    assert ledger.transactions({})["rows"][0]["type2"] == "正餐"
    with pytest.raises(FileExistsError):
        ledger.delete_category({"level1": "餐饮", "level2": "正餐"})
    with pytest.raises(FileExistsError):
        ledger.rename_category(
            {"old1": "餐饮", "old2": "正餐", "new1": "生活", "new2": "日用"}
        )
    assert ledger.dictionary()["餐饮"] == ["正餐"]


def test_bulk_requires_specific_scope_and_rechecks_confirmation(pg_env):
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO ledger_tx(source,month,direction,amount,who,goods) "
            "SELECT '手动','2026-08','支出',1,'示例','商品' FROM generate_series(1,301)"
        )
    with pytest.raises(ValueError):
        ledger.bulk_category({"type1": "餐饮", "type2": "三餐", "classified": "0"})
    request = {"type1": "餐饮", "type2": "三餐", "month": "2026-08"}
    with pytest.raises(PermissionError):
        ledger.bulk_category(request)
    assert ledger.transactions({"classified": "1"})["total"] == 0
    assert ledger.bulk_category({**request, "confirm": True})["updated"] == 301


def test_literal_fuzzy_filters_empty_goods_and_paging_validation(pg_env):
    add(who="50%商户", goods="")
    add(who="50a商户", goods="item")
    assert ledger.transactions({"who_f": "50%"})["total"] == 1
    assert ledger.transactions({"goods_e": ""})["total"] == 1
    for filters in (
        {"size": "0"},
        {"page": "NaN"},
        {"year": "2026 OR 1=1"},
        {"month": "2026-13"},
    ):
        with pytest.raises(ValueError):
            ledger.transactions(filters)


def test_relationships_are_reported_for_both_visible_sides_and_empty_pages(pg_env):
    a = add()
    b = add(direction="退款")
    ledger.offset({"a": a, "b": b})
    first = ledger.transactions({"size": 1})
    second = ledger.transactions({"size": 1, "page": 2})
    assert first["rows"][0]["refund_pair"]["other_id"] == a
    assert second["rows"][0]["refund_pair"]["other_id"] == b
    assert ledger.transactions({"page": 3, "size": 1})["rows"] == []


def test_paged_relationships_keep_values_with_bounded_queries(pg_env):
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO ledger_tx(time,month,direction,amount) "
            "SELECT '2026-08-12 12:00:00','2026-08','支出',30 FROM generate_series(1,201)"
        )
        # 三条都是 status='linked'（与既有 SQLite 用例一致）：它们本身被默认筛选排除，
        # 但作为关系对手方仍必须出现在第一页行的 refund_pair / offset_done 里。
        conn.execute(
            "INSERT INTO ledger_tx(id,time,month,direction,amount,card,goods,status) "
            "VALUES(1001,'2026-07-01 09:30:00','2026-07','支出',30,'演示卡','银行摘要','linked'),"
            "(1002,'2026-07-01 09:30:00','2026-07','支出',-30,'','','linked'),"
            "(1003,'2026-07-01 09:30:00','2026-07','支出',-12,'','','linked')"
        )
        conn.execute(
            "INSERT INTO ledger_tx_link(a,b,rel) VALUES"
            "(1001,201,'bank_bill'),(200,1002,'refund_pair'),(1003,199,'aa_offset')"
        )
    result = ledger.transactions({"year": "2026", "size": 200})
    assert result["total"] == 201 and len(result["rows"]) == 200
    rows = {row["id"]: row for row in result["rows"]}
    assert rows[201]["bank_linked"] == {
        "date": "2026-07-01", "amount": 30, "card": "演示卡", "summary": "银行摘要",
    }
    assert rows[200]["refund_pair"] == {
        "other_id": 1002, "other_dir": "支出", "other_amount": -30, "other_time": "2026-07-01",
    }
    assert rows[199]["offset_done"]["other_amount"] == -12
    assert "bank_linked" not in rows[198] and "offset_done" not in rows[198]


def test_years_filter_rejects_malformed_months(pg_env):
    add()
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO ledger_tx(source,month,direction,amount,status) "
            "VALUES('手动','2026-13','支出',1,'normal'),('手动','not-a-month','支出',1,'normal')"
        )
    assert ledger.years() == ["2026"]


# --- 汇总（Decimal 精度） -----------------------------------------------------

def test_overview_and_by_category_use_decimal_totals(pg_env):
    add(amount="0.1")
    add(amount="0.2")
    stats = ledger.overview({"year": "2026", "month": "2026-08"})
    # 二进制浮点累加会给出 0.30000000000000004；Decimal 汇总必须是精确的 0.3。
    assert stats["month_expense"] == 0.3
    rows = ledger.by_category({"year": "2026"})["rows"]
    assert rows[0]["amt"] == 0.3


def test_compare_months_are_decimal_quantized(pg_env):
    add(amount="10.005", type1="餐饮", type2="三餐")
    result = ledger.compare({"year": "2026"})
    group = result["expense"]["groups"][0]
    assert group["t1"] == "餐饮"
    assert group["months"][7] == 10.0 or group["months"][7] == 10.01


# --- 并发与锁 ---------------------------------------------------------------

def test_ledger_lock_serialises_concurrent_manual_writes(pg_env):
    """另一连接持账本锁时，本连接的写事务必须在有界超时内失败（提案 §4.2）。"""
    holder = pg_test_support.connect(pg_env)
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, pg_locks.LEDGER.key),
        )
        with pytest.raises(pgdb.StorageUnavailable):
            add()
        holder.rollback()
    finally:
        holder.close()
    assert add() > 0


def test_reads_do_not_take_the_ledger_lock(pg_env):
    """纯读事务不取业务锁，冻结期仍能读（提案 §7.3）。"""
    holder = pg_test_support.connect(pg_env)
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, pg_locks.LEDGER.key),
        )
        assert ledger.transactions({})["total"] == 0
        holder.rollback()
    finally:
        holder.close()


def test_offset_is_atomic_under_two_connections(pg_env):
    """两笔并发配对同一对流水：只能有一个成功，不能产生双配对。"""
    a, b = add(), add(direction="收入", amount=30)
    holder = pg_test_support.connect(pg_env)
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, pg_locks.LEDGER.key),
        )
        with pytest.raises(pgdb.StorageUnavailable):
            ledger.offset({"a": a, "b": b})
        holder.rollback()
    finally:
        holder.close()
    assert ledger.offset({"a": a, "b": b})["ok"] == 1
    with pgdb.pg_transaction(readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_tx_link WHERE rel IN ('offset','aa_offset','refund_pair')"
        ).fetchone()["c"] == 1
