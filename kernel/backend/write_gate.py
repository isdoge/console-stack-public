# -*- coding: utf-8 -*-
"""CLI 写入口的维护态**权威解析**（MT-01；提案 §11.12）。

**问题**（PG-06 S20 现场复现）：`runtime_mode.maintenance_mode()` 只读进程 env、
缺省 `normal`；割接链把 `CONSOLE_MAINTENANCE_MODE=read_only` 写进部署 `.env` 供给
app，但**直连 CLI**（`ledger_cli.py import/reclassify` 等）在没带该变量的进程里跑时
`require_writable()` 会静默放行 → 文件先落盘 → PG 事务因冻结被拒而回滚，**文件不
回滚** → 留下孤儿归档（`GET /api/ledger/files/batches/<id>` 还回 200，对外像
「已归档且完整」）。

**本模块只做一件事**：CLI 写入口在发起**任何落盘/写库之前**，把「现在能不能写」
解析成**进程无关**的判定（纯只读查询，不落盘、不写库、不加锁）。app/HTTP 路径与
`publish_batch`/`ledger_import` 里既有的 env 守卫**原样不动**（PG-06 已验收）。

判定顺序（**最严格者胜**）：

1. 进程 env 显式 `read_only` → 只读（保留既有 env 路径；非法值仍 `ConfigError`）。
2. 引擎不是 `postgres`（sqlite 缺省）→ **放行**：不翻转全局默认，开发/CI 进程
   不带该变量必须照常工作。
3. 引擎是 `postgres` → 连库问权威来源（与进程 env 无关）：
   a. `pg_locks` 里有本 namespace 的**独占维护锁**（key=1；控制器/迁移器持锁 =
      冻结窗口在案，含「等待中」的请求——那是冻结正在发生）→ 只读；
   b. 本连接账号在 `public` 业务表上**缺 INSERT**（`cutover_writers freeze --role`
      的撤权写在库里，控制器死掉、锁释放之后也仍然拒写）→ 只读；
   c. 两者都查得到且都未命中 → 放行（`source=pg:writable`）。
4. 判不出（缺配置 / 连不上 / 查询失败）→ **fail-closed：只读**。已部署环境
   （显式声明 postgres）宁可拒绝，也不冒「文件已落盘、库里没记录」的险。

**边界（有意不做）**：

- 不读部署 `.env` 文件：它的路径由割接参数（`--deploy-env`）决定，对 CLI 不可知；
  而角色权限与独占锁正是 `.env` 想表达的**事实本身**，`.env` 只是它的副本。
- `schema_migrations` 不在权限探测范围：§6.3 对运行账号**单独撤销**它的写权限是
  常态（`pg_migrations.revoke_migrations_writes`），不是冻结信号。
- 残余竞态：本判定发生在 CLI 入口，若**写入进行中**割接窗口才开始（或长导入期间
  PG 失效），文件仍可能先行落盘——那是「任何中途失败」的共性面，不属 MT-01
  （入口静默放行）；窗口内停写仍由割接 `stop-writers` 负责。
"""
import runtime_mode

SOURCE_ENV_READ_ONLY = "env:CONSOLE_MAINTENANCE_MODE=read_only"
SOURCE_SQLITE_DEFAULT = "engine:非 postgres（sqlite 缺省，开发/CI 放行）"
SOURCE_PG_LOCK = "pg:maintenance-lock（独占维护锁在案）"
SOURCE_PG_ROLE_FROZEN = "pg:role-frozen（账号写权限已被撤）"
SOURCE_PG_WRITABLE = "pg:writable"
SOURCE_UNDETERMINED = "undetermined:fail-closed"


def resolve():
    """解析 CLI 写入口的维护态；返回 `dict`，**只读无副作用**。

    `{"read_only": bool, "source": str, "detail": {...}}`——`source` 说明判定依据
    （进日志/证据用，不含 DSN/凭据）；`detail` 对 PG 分支给出两个原始信号：
    `maintenance_lock_held` 与 `tables_missing_insert`（表名清单，不含任何数据）。
    """
    # 1) 进程 env（既有路径；非法值 ConfigError 原样抛，不静默降级）
    if runtime_mode.maintenance_mode() == runtime_mode.MAINTENANCE_READ_ONLY:
        return {"read_only": True, "source": SOURCE_ENV_READ_ONLY, "detail": {}}
    # 2) 非 postgres（sqlite 缺省）：开发/CI 放行，不翻转全局默认
    if runtime_mode.storage_engine() != runtime_mode.STORAGE_ENGINE_POSTGRES:
        return {"read_only": False, "source": SOURCE_SQLITE_DEFAULT, "detail": {}}
    # 3) postgres：权威来源 + fail-closed
    return _probe_postgres()


def _probe_postgres():
    conn = None
    try:
        conn = _connect_once()
        lock_held = _maintenance_lock_held(conn)
        missing = _tables_missing_insert(conn)
    except Exception as exc:  # noqa: BLE001 - 判不出即拒绝（fail-closed）
        return {"read_only": True, "source": SOURCE_UNDETERMINED,
                "detail": {"error": _safe_error(exc)}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - 关闭失败不影响判定
                pass
    detail = {"maintenance_lock_held": lock_held,
              "tables_missing_insert": missing}
    if lock_held:
        return {"read_only": True, "source": SOURCE_PG_LOCK, "detail": detail}
    if missing:
        return {"read_only": True, "source": SOURCE_PG_ROLE_FROZEN, "detail": detail}
    return {"read_only": False, "source": SOURCE_PG_WRITABLE, "detail": detail}


def _connect_once():
    """一次性的短连接（CLI 单进程用；不进 app 池，不读生产 DSN 以外的东西）。

    只复用 `pgdb.PgConfig` 的标准配置路径（`CONSOLE_PG_*`）；psycopg 延迟导入，
    保持本模块 import 期零副作用。
    """
    import pgdb

    cfg = pgdb.PgConfig.from_env()
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise pgdb.PgConfigError(
            "缺少 psycopg 依赖：请安装 psycopg[binary]") from exc
    conn = psycopg.connect(
        cfg.conninfo(),
        connect_timeout=cfg.connect_timeout,
        autocommit=True,
        row_factory=dict_row,
        application_name="console-stack/cli-write-gate",
    )
    # 判据查询都是只读小查询；给一个硬上限，卡住按「判不出」处理（fail-closed）。
    conn.execute("SET statement_timeout = '10s'")
    return conn


def _maintenance_lock_held(conn):
    """`pg_locks` 里本 namespace 的独占维护锁（granted 或等待中的请求都算）。

    业务写事务只用**共享**维护锁（`pgdb.pg_transaction` 自动追加），正常运行时
    不会出现独占行；独占行只来自控制器/迁移器——即冻结窗口。
    """
    import pg_locks

    row = conn.execute(
        "SELECT count(*) AS held FROM pg_locks "
        "WHERE locktype = 'advisory' AND classid = %s AND objid = %s "
        "AND mode = 'ExclusiveLock'",
        (pg_locks.LOCK_NAMESPACE, pg_locks.KEY_MAINTENANCE),
    ).fetchone()
    return row["held"] > 0


def _tables_missing_insert(conn):
    """本连接账号在 `public` 业务表上缺 INSERT 的表名（排除 `schema_migrations`）。

    枚举用 `pg_class`（对任何角色可见）而不是 `information_schema.tables`——后者
    会隐藏「连 SELECT 都没有」的表，那正是最需要被看见的冻结形态。
    """
    rows = conn.execute(
        "SELECT c.relname AS name FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') "
        "AND c.relname <> 'schema_migrations' "
        "AND NOT has_table_privilege("
        "  current_user, quote_ident(n.nspname) || '.' || quote_ident(c.relname), 'INSERT') "
        "ORDER BY c.relname",
        ("public",),
    ).fetchall()
    return [row["name"] for row in rows]


def _safe_error(exc):
    """异常文本脱敏：只留类型名；`PgConfigError` 的正文只有变量名，可留。"""
    import pgdb

    if isinstance(exc, pgdb.PgConfigError):
        return f"{type(exc).__name__}: {pgdb._safe_exc(exc)}"
    return type(exc).__name__
