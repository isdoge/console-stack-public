# -*- coding: utf-8 -*-
"""PG-03 测试共享件：合成 SQLite 源 + 隔离 PG 目标 + CLI 子进程调用。

数值全部手工可核算（提案 §8.5：防搬运与验证共享同一错误的兜底是独立样例）。
`EXPECTED_*` 常量是**用计算器预先算好的**，不是跑代码回填的。
"""
import os
import pathlib
import sqlite3
import subprocess
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "console" / "backend"
SCRIPTS = REPO_ROOT / "scripts"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db  # noqa: E402  只用 legacy run_migrations 搭源快照骨架（1–11）

# app.init_db 里补的表（不含 panels：主夹具刻意缺它，验证「源不存在，目标兼容空表」）
EXTRA_TABLE_DDL = (
    """CREATE TABLE oplog (id INTEGER PRIMARY KEY, host TEXT, svc TEXT, action TEXT,
        started_at TEXT, finished_at TEXT, result TEXT, operator TEXT DEFAULT 'self')""",
    """CREATE TABLE versions (svc_name TEXT PRIMARY KEY, local_version TEXT,
        upstream_version TEXT, behind_count INTEGER, update_status TEXT,
        recent_changes TEXT, last_checked TEXT)""",
    """CREATE TABLE panel_scratch (panel_id INTEGER PRIMARY KEY, content TEXT,
        updated_at TEXT)""",
    """CREATE TABLE bookmarks (id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
        icon TEXT DEFAULT '', category TEXT DEFAULT 'default', tags TEXT DEFAULT '[]',
        sort_order INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""",
    """CREATE TABLE feed_cache (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
        url TEXT DEFAULT '', summary TEXT DEFAULT '', icon TEXT DEFAULT '',
        published_at TEXT DEFAULT '', fetched_at TEXT)""",
)

# --- 手工核算的金额样例（用计算器预先算好，不来自被测代码） ---------------------
# ledger_tx 行：id → (amount, direction, pay, type1)
LEDGER_ROWS = (
    # id, amount, direction, pay, type1
    (-5, 0.1, "expense", "wx", "food"),
    (1, 0.2, "expense", "wx", "food"),
    (2, -19.99, "income", "alipay", "traffic"),
    (3, 0.0, "expense", "cash", "other"),
    (4, -0.0, "expense", "cash", "other"),
    (5, 1234.5678901234567, "income", "bank", "salary"),
    (6, 100000, "expense", "wx", "food"),
    (7, None, "expense", "wx", "food"),
)
# 逐行规范值（Decimal(str(float)) 去尾零/负零归零）：
EXPECTED_LEDGER_CANONICAL = {
    -5: "0.1",
    1: "0.2",
    2: "-19.99",
    3: "0",
    4: "0",
    5: "1234.5678901234567",
    6: "100000",
    7: None,
}
# 分组汇总（月/方向/渠道/分类）：0.1+0.2+100000=100000.3；0.0+(-0.0)=0
EXPECTED_LEDGER_GROUPS = {
    ("2026-09", "expense", "wx", "food"): "100000.3",
    ("2026-09", "income", "alipay", "traffic"): "-19.99",
    ("2026-09", "expense", "cash", "other"): "0",
    ("2026-09", "income", "bank", "salary"): "1234.5678901234567",
}
# 总计 0.3 - 19.99 + 0 + 1234.5678901234567 + 100000 = 101214.8778901234567
EXPECTED_LEDGER_TOTAL = "101214.8778901234567"
# 二进制累加：0.1+0.2=0.30000000000000004 起步必偏离十进制口径
EXPECTED_BINARY_DRIFT = True

RENEWAL_ROWS = (  # id, cost, currency
    ("r1", 99.99, "CNY"),
    ("r2", 0.1, "CNY"),
    ("r3", None, "CNY"),
    ("r4", 5.5, "USD"),
)
EXPECTED_RENEWAL_GROUPS = {"CNY": "100.09", "USD": "5.5"}


def make_legacy_db(path):
    """合成完整 schema 的离线源：SQLite v1–11 + init_db 补表 + 手工数据。"""
    path = str(path)
    db.run_migrations(path)
    conn = sqlite3.connect(path)
    try:
        for ddl in EXTRA_TABLE_DDL:
            conn.execute(ddl)
        _seed(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _seed(conn):
    conn.execute(
        "INSERT INTO renewals (id,name,kind,expires_at,starts_at,cost,currency,cycle,"
        "auto_renew,url,note,source,created_at,updated_at,sort_order,provider) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "域名A", "domain", "2027-01-01", "2026-01-01", 99.99, "CNY", "yearly",
         1, "https://a", None, "manual", "2026-01-01 00:00:00", "2026-01-01 00:00:00", 0, "p1"),
    )
    conn.execute(
        "INSERT INTO renewals (id,name,kind,expires_at,cost,currency,sort_order) "
        "VALUES (?,?,?,?,?,?,?)",
        ("r2", "证书B", "cert", None, 0.1, "CNY", 1),
    )
    conn.execute(
        "INSERT INTO renewals (id,name,kind,expires_at,cost,currency,sort_order) "
        "VALUES (?,?,?,?,?,?,?)",
        ("r3", "不限时", "other", "2026-12-31", None, "CNY", 2),
    )
    conn.execute(
        "INSERT INTO renewals (id,name,kind,expires_at,cost,currency,sort_order) "
        "VALUES (?,?,?,?,?,?,?)",
        ("r4", "海外VPS", "vps", "2027-06-01", 5.5, "USD", 3),
    )
    for tx_id, amount, direction, pay, type1 in LEDGER_ROWS:
        conn.execute(
            "INSERT INTO ledger_tx (id,dedup,source,src_file,order_no,time,date,month,"
            "direction,amount,who,goods,pay,zfb_cat,type1,type2,basis,pay_confirmed,"
            "created_at,card,status,note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tx_id, f"dedup-{tx_id}", "wechat", "f.xlsx", f"o{tx_id}",
             "2026-09-01 10:00:00", "2026-09-01", "2026-09", direction, amount,
             "山田商店😀", "商品\n第二行", pay, None, type1, None, "manual", 1,
             "2026-09-01 10:00:01", None, "normal", "备注换行\nsecond" if tx_id == 1 else ""),
        )
    conn.execute("INSERT INTO ledger_tx_link (a,b,rel) VALUES (-5,1,'offset')")
    conn.execute("INSERT INTO ledger_tx_link (a,b,rel) VALUES (5,6,'link_repay')")
    conn.executemany(
        "INSERT INTO ledger_categories (id,level1,level2,sort) VALUES (?,?,?,?)",
        [(1, "food", "snack", 1), (2, "traffic", "", 2), (3, "salary", None, 3)],
    )
    conn.executemany(
        "INSERT INTO ledger_knowledge (who,good,type1,type2,cnt) VALUES (?,?,?,?,?)",
        [("山田商店😀", "商品", "food", "snack", 3), ("", "空商户", "other", None, 1)],
    )
    conn.execute(
        "INSERT INTO ledger_imports (source_sha256,imported_at,counts_json) VALUES (?,?,?)",
        ("a" * 64, "2026-09-01 00:00:00", '{"n": 8, "跳过": 0}'),
    )
    for pid in (1, 2, 3):
        conn.execute(
            "INSERT INTO probe_history (id,machine,ts,status,latency_ms) VALUES (?,?,?,?,?)",
            (pid, "nas", f"2026-09-0{pid} 00:00:0{pid}", "up", pid * 1.5),
        )
    conn.execute("DELETE FROM probe_history WHERE id = 3")  # 高水位 3、max 2 → next 必须 4
    conn.executemany(
        "INSERT INTO service_health_history (id,name,ts,status) VALUES (?,?,?,?)",
        [(10, "svc-a", "2026-09-01 00:00:00", "Healthy"),
         (11, "svc-b", "2026-09-01 00:01:00", "Unhealthy")],
    )
    conn.execute(
        "INSERT INTO monitor_events (id,fingerprint,kind,source,target,machine,severity,"
        "title,detail,opened_at,updated_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "fp-1", "probe", "nas", "svc-a", "nas", "error", "开事件", "",
         "2026-09-01 00:00:00", "2026-09-01 00:00:00", None),
    )
    conn.execute(
        "INSERT INTO monitor_events (id,fingerprint,kind,source,target,machine,severity,"
        "title,detail,opened_at,updated_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, "fp-2", "probe", "nas", "svc-b", "nas", "error", "已恢复", "",
         "2026-09-01 00:00:00", "2026-09-01 00:05:00", "2026-09-01 00:05:00"),
    )
    conn.execute(
        "INSERT INTO machine_traffic (machine,month,sent_bytes,recv_bytes,last_sent_total,"
        "last_recv_total,nic,resets,started_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("nas", "2026-09", 9007199254740000, 123, 10 ** 15, 10 ** 15 + 7, "en0", 0,
         "2026-09-01 00:00:00", "2026-09-09 00:00:00"),
    )
    conn.execute(
        "INSERT INTO dashboard_layout (widget_key,x,y,w,hidden,updated_at) VALUES (?,?,?,?,?,?)",
        ("machines", 0, 0, 6, 1, "2026-09-01 00:00:00"),
    )
    conn.executemany(
        "INSERT INTO oplog (id,host,svc,action,started_at,finished_at,result,operator) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(5, "nas", "console", "deploy", "2026-09-01", "2026-09-01", "ok", "self"),
         (7, "nas", "console", "backup", "2026-09-02", None, "running", "self")],
    )
    conn.execute(
        "INSERT INTO versions (svc_name,local_version,upstream_version,behind_count,"
        "update_status,recent_changes,last_checked) VALUES (?,?,?,?,?,?,?)",
        ("console-stack", "1.0", "1.0", 0, "latest", "[]", "2026-09-09 00:00:00"),
    )
    conn.execute("INSERT INTO panel_scratch (panel_id,content,updated_at) VALUES (?,?,?)",
                 (7, "草稿😀", "2026-09-09 00:00:00"))
    conn.execute(
        "INSERT INTO bookmarks (id,title,url,tags,sort_order) VALUES (?,?,?,?,?)",
        ("b1", "内网", "http://x", '["工作","旅行"]', 1),
    )
    conn.execute(
        "INSERT INTO feed_cache (id,source,title,fetched_at) VALUES (?,?,?,?)",
        ("f1", "rss", "标题", "2026-09-09 00:00:00"),
    )


def fresh_database(admin_conn, prefix="pg03t"):
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    return name


def drop_database(admin_conn, name):
    admin_conn.execute(
        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s',
        (name,),
    )
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def pg_env(container, dbname, extra=None):
    env = dict(os.environ)
    env.update({
        "CONSOLE_PG_HOST": container.host,
        "CONSOLE_PG_PORT": str(container.port),
        "CONSOLE_PG_DB": dbname,
        "CONSOLE_PG_USER": container.user,
        "CONSOLE_PG_PASSWORD": container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
    })
    env.pop("CONSOLE_PG_TEST_DSN", None)
    if extra:
        env.update(extra)
    return env


def run_cli(script_name, argv, env, timeout=180):
    """子进程跑真实 CLI（退出码就是契约的一部分）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script_name)] + list(argv),
        capture_output=True, text=True, env=env, timeout=timeout, check=False,
    )


def copied_source(tmp_path, name="console.db"):
    """主合成源的独立副本（坏数据注入不影响其他用例）。"""

    target = tmp_path / name
    if not target.exists():
        make_legacy_db(target)
    return str(target)
