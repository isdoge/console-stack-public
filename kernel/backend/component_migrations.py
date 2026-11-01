# -*- coding: utf-8 -*-
"""B7-2 组件迁移登记与遗产链接管回执（隔离验证批次，未进入生产）。

B7 要把通用 Console 从私人组合里抽出来：公共新库不该为了「看起来一样」去建
ledger/renewals 等私人表，私人既有库也不该被重写成另一条历史。因此保留现有
`schema_migrations` 1–17 遗产链**逐字节冻结**，另建一条按组件登记的谱系：

    component_schema_migrations(component, version) —— 主键

`kernel` 与有表的 `plugin:<id>` 各写各的迁移；部署组合负责给出组件与顺序；
DDL 只由受控 owner CLI（`scripts/pg_component_schema.py`）在**既有维护锁**下执行。
插件的 import/initializer 永远不执行 DDL（§4.4）。

本批的硬边界，写在代码里也写在测试里：

- **未改生产**：本模块不连任何生产库、不改部署链、不改 `pg_migrations.py` 的
  任何 SQL/checksum，也不预占遗产链的「18」。生产登记要等 B7-4 完整消费方就绪。
- **遗产 checksum 冻结**：接管只读 `pg_migrations.migration_checksums()` 与
  `pg_migrations.applied()` 做核对，**从不**写 `schema_migrations`、不重放
  CREATE/seed、不 DROP。
- **登记 owner 唯一**：组件登记表由本模块定义，`scripts/pg_component_schema.py`
  是唯一的 DDL 入口；应用启动路径不建表。
- **结构等价双向证明**：接管（owner 路径）先把冻结遗产链重放进一次性 schema，
  得到**独立标准 catalog**，逐组件比对目标结构后才写半条回执；`registry_report()`
  是只读快照路径（备份可能在 REPEATABLE READ/READ ONLY 里执行），只重算实际
  catalog 并比对登记里记录的最新指纹，**不做任何 DDL、不重放标准**。

组件与表的归属事实是**冻结**的：`FROZEN_PLUGIN_TABLES` 记录遗产链里哪张表属于
哪个插件。已安装插件只决定哪些组件存在；未安装插件的表进入 `retained`，绝不
回落给 kernel——因此 `kernel` 的 11 张表不随当前插件全集变化。

`kernel` 表 = 冻结表全集减去冻结插件表、减去遗产保留表与登记表本身。真实组合
下这正好是 §4.4 列出的 11 张 kernel 表。

`panels` 是 NAS 历史兼容表，不归 kernel 也不归任何插件，列在
`LEGACY_RETAINED_TABLES` 里显式保留、不自动 DROP。

指纹范围：列（顺序/类型/非空/默认）、约束、索引。**不含**数据、序列当前值、
角色权限、行数；跨库比对要求相同 `search_path`（`pg_get_expr`/`format_type`
的输出受它影响）。
"""
import hashlib
import json
import re
import uuid
from pathlib import Path

import composition
import kernel_baseline
import pg_migrations
import plugin_imports

KERNEL_COMPONENT = "kernel"
PLUGIN_PREFIX = "plugin:"
# 版本 0 保留给接管回执；组件自己的迁移从 1 开始，两者不会撞号。
ADOPTION_VERSION = 0
ORIGIN_APPLIED = "applied"
ORIGIN_ADOPTED = "adopted"
ADOPTED_FROM_LEGACY_CHAIN = "legacy-pg-chain"
# 私人遗产兼容层：既不属于 kernel 也不属于任何插件，登记为显式保留、不 DROP。
LEGACY_RETAINED_TABLES = ("panels",)
REGISTRY_TABLE = "component_schema_migrations"
# 登记表本身与冻结遗产链的版本表都不归组件迁移所有。
REGISTRY_TABLES = ("schema_migrations", REGISTRY_TABLE)
RECEIPT_KEYS = ("adopted_from", "legacy_versions", "legacy_checksums", "tables")

# 遗产链里各插件表的**冻结归属**。这张表是历史事实，不随当前插件全集变化：
# 某个插件被移出组合后，它的表进入 `retained`，而不会回落成 kernel 表。
# 新插件自己的表不进这里——它们由插件组件的迁移链创建。
FROZEN_PLUGIN_TABLES = {
    "backup_evidence": ("backup_evidence_incidents", "backup_evidence_receipts"),
    "feed": ("bookmarks", "feed_cache"),
    "ledger": (
        "ledger_categories", "ledger_file_batches", "ledger_imports",
        "ledger_knowledge", "ledger_tx", "ledger_tx_link",
    ),
    "panels": ("panel_scratch",),
    "renewals": ("renewals",),
}

_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_UTC_NOW = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"

# 组件登记表。`origin` 只允许两个值，且与 version 一一对应：
#   applied —— 该组件自己的迁移，由 owner CLI 执行后登记；
#   adopted —— 接管回执：结构**已经存在**，只登记身份，不重放 CREATE/seed。
# `catalog_fingerprint` 对 adopted 必填（回执要能证明接管时的结构）；
# `metadata` 对 adopted 必须带 RECEIPT_KEYS 四键，契约见 `validate_receipt()`。
REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS component_schema_migrations (
    component TEXT NOT NULL CHECK (component <> ''),
    version INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT %s,
    origin TEXT NOT NULL DEFAULT 'applied'
        CHECK (origin IN ('applied', 'adopted')),
    catalog_fingerprint TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (component, version),
    CHECK ((version = 0) = (origin = 'adopted')),
    CHECK (origin <> 'adopted' OR catalog_fingerprint IS NOT NULL)
)
""" % _UTC_NOW

KERNEL_BASELINE_VERSION = kernel_baseline.KERNEL_BASELINE_VERSION

# 正式组件版本链。kernel 基线来自 `kernel_baseline`（唯一来源，不复制 SQL）；
# 接管库的回执 v0 覆盖基线，因此旧库不需要重放这条 CREATE。
# 不预占遗产链版本号，也不改 `pg_migrations.PG_MIGRATIONS` 的任何字节。
COMPONENT_MIGRATIONS = {
    KERNEL_COMPONENT: (
        (
            KERNEL_BASELINE_VERSION,
            "kernel_baseline",
            kernel_baseline.KERNEL_BASELINE_SQL,
        ),
    ),
}


class ComponentRegistryError(RuntimeError):
    """组件登记状态与组合/代码期望不符：有界失败，不猜测修补。"""


class UnknownComponent(ComponentRegistryError):
    """组合里没有声明的组件出现在登记表或迁移计划里。"""


class ComponentChecksumDrift(ComponentRegistryError):
    """已登记版本的 SQL 指纹与代码不符：拒绝继续，先查清是谁改了历史。"""


class ReceiptError(ComponentRegistryError):
    """接管回执格式不合法。"""


# --- 组件命名与组合事实 -------------------------------------------------------


def plugin_component(plugin_id):
    """插件 id → 组件名 `plugin:<id>`（与插件清单同一 id 形状）。"""
    if not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id):
        raise UnknownComponent(f"插件 id 无效，不能登记为组件: {plugin_id!r}")
    return PLUGIN_PREFIX + plugin_id


def is_component_name(name):
    if name == KERNEL_COMPONENT:
        return True
    if not isinstance(name, str) or not name.startswith(PLUGIN_PREFIX):
        return False
    return bool(_ID.fullmatch(name[len(PLUGIN_PREFIX):]))


def manifest_table_owners(root):
    """已安装插件 → 其 `owns.tables`；只取登记需要的字段。

    完整清单校验归 `plugin_manifest`（ID/协议/路由/冲突）。这里刻意只读
    `id` 与 `owns.tables`，并复用 `composition.plugin_sources()` 做清单/磁盘
    两层校验（缺失、重复、越界、软链接），避免本模块变成第二个清单实现。
    """
    root = Path(root)
    declared = composition.plugin_sources(root)
    owners = {}
    seen = {}
    for pid in declared:
        path = root / pid / "manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ComponentRegistryError(f"{pid}: 清单不可读取") from exc
        if not isinstance(data, dict) or data.get("id") != pid:
            raise ComponentRegistryError(f"{pid}: 清单 id 与目录名不一致")
        owns = data.get("owns")
        tables = owns.get("tables") if isinstance(owns, dict) else None
        if not isinstance(tables, list) or any(
            not isinstance(t, str) or not t.strip() for t in tables
        ):
            raise ComponentRegistryError(f"{pid}: owns.tables 必须是不重复字符串数组")
        if len(set(tables)) != len(tables):
            raise ComponentRegistryError(f"{pid}: owns.tables 含重复表名")
        for table in tables:
            if table in seen:
                raise ComponentRegistryError(
                    f"表 {table} 同时归 {seen[table]} 与 {pid} 所有"
                )
            seen[table] = pid
        owners[pid] = tuple(tables)
    return owners


def component_table_map(plugin_tables, *, schema_tables=pg_migrations.SCHEMA_TABLES):
    """`{plugin_id: tables}` → 组件→表映射（kernel 在前，插件按 id 排序）。

    已安装插件只决定**哪些组件存在**；表归属来自冻结的 `FROZEN_PLUGIN_TABLES`，
    所以 kernel 的 11 张表不随插件全集变化。插件声明的表必须与冻结归属一致
    （多退少补都算清单漂移，fail closed）；没安装的插件表进入 `retained`。

    表名统一**升序规范化**：回执、白名单与 catalog 指纹都按这一份顺序比较，
    免得清单书写顺序影响指纹。
    """
    frozen = tuple(schema_tables)
    known = set(frozen) - set(REGISTRY_TABLES) - set(LEGACY_RETAINED_TABLES)
    claimed = {}
    for pid in sorted(plugin_tables):
        component = plugin_component(pid)
        tables = tuple(sorted(plugin_tables[pid]))
        historical = FROZEN_PLUGIN_TABLES.get(pid, ())
        if tables != tuple(sorted(historical)):
            raise ComponentRegistryError(
                f"{component} 的表归属与冻结遗产归属不符: "
                f"清单={list(tables)} 冻结={list(sorted(historical))}"
            )
        for table in tables:
            if table not in known:
                raise ComponentRegistryError(
                    f"{component} 声明了冻结表全集之外的表: {table}"
                )
            if table in claimed:
                raise ComponentRegistryError(
                    f"表 {table} 同时归 {claimed[table]} 与 {component} 所有"
                )
            claimed[table] = component
    result = {KERNEL_COMPONENT: tuple(sorted(
        t for t in frozen
        if t not in claimed and t not in REGISTRY_TABLES and t not in LEGACY_RETAINED_TABLES
        and not any(t in tables for tables in map(tuple, FROZEN_PLUGIN_TABLES.values()))
    ))}
    for pid in sorted(plugin_tables):
        result[plugin_component(pid)] = tuple(sorted(plugin_tables[pid]))
    return result


def retained_tables(components):
    """不属于任何组件的表：未安装插件的历史表 + 遗产保留表 + 两张登记表。

    它们必须进白名单（备份/目录核对要覆盖全员），但**不**进 `table_owners`——
    伪造组件归属会让 manifest 撒谎。备份方按「present → table_owners →
    retained」归类时，登记表也必须有归属，否则每次备份都会因「表既不归组件
    也不在 retained」而失败。
    """
    _validate_components(components)
    installed = {name[len(PLUGIN_PREFIX):] for name in components
                 if name.startswith(PLUGIN_PREFIX)}
    tables = set(LEGACY_RETAINED_TABLES) | set(REGISTRY_TABLES)
    for pid, owned in FROZEN_PLUGIN_TABLES.items():
        if pid not in installed:
            tables.update(owned)
    return tuple(sorted(tables))


def retained_component_tables(rows, components):
    """登记里有合法接管回执、但当前组合不再声明的组件 → `{组件: 表}`。

    插件移出源码后，它的表仍在库里、回执仍有效；这些表是显式保留，不是
    kernel、也不是「未知组件」。判定要求两条证据同时成立：回执自身校验通过，
    且回执表集合等于该插件的**冻结遗产归属**。没有任何合法回执的额外组件
    （如伪造的 `plugin:ghost`、只有 applied 行）一律留在 unknown 路径上。
    """
    _validate_components(components)
    retained = {}
    for row in rows or []:
        component = row.get("component")
        if component in components or not is_component_name(component):
            continue
        if component == KERNEL_COMPONENT or row.get("origin") != ORIGIN_ADOPTED:
            continue
        try:
            normalized = validate_receipt(row)
        except ReceiptError:
            continue
        frozen = FROZEN_PLUGIN_TABLES.get(component[len(PLUGIN_PREFIX):])
        if not frozen or normalized["metadata"]["tables"] != sorted(frozen):
            continue
        retained[component] = list(sorted(frozen))
    return {name: retained[name] for name in sorted(retained)}


def declared_components(plugin_root=None):
    """当前组合声明的组件与表归属（读插件根，不建连接）。"""
    root = Path(plugin_root) if plugin_root is not None else plugin_imports.plugin_root()
    return component_table_map(manifest_table_owners(root))


def zero_plugin_components():
    """零插件组合：只有 kernel，其余历史表全部 retained（公共新装起点）。"""
    return {KERNEL_COMPONENT: component_table_map({})[KERNEL_COMPONENT]}


def _validate_components(components):
    if not isinstance(components, dict) or not components:
        raise ComponentRegistryError("组件表必须是非空映射")
    for component, tables in components.items():
        if not is_component_name(component):
            raise UnknownComponent(f"无效组件名: {component!r}")
        if not isinstance(tables, tuple) or any(
            not isinstance(t, str) or not t.strip() for t in tables
        ):
            raise ComponentRegistryError(f"{component}: 表列表必须是字符串元组")
        # 顺序必须由 `component_table_map()` 规范化过：指纹与回执都比较这一份。
        if list(tables) != sorted(set(tables)):
            raise ComponentRegistryError(f"{component}: 表列表必须是不重复的升序")
    return components


def table_whitelist(components):
    """全表**允许**集合：组件表 + 未安装插件表（retained）+ 遗产保留表 + 登记表。

    备份/目录核对用它判断「目录里多出来的表」；多出来的表必须失败，不能被
    悄悄漏备（§4.4/§4.5）。注意这是允许集合而不是现存集合：零插件新装时
    whitelist 仍含 12 张未建的历史表，实际存在哪些表看 `present_tables`。
    """
    _validate_components(components)
    tables = set(retained_tables(components))
    for declared in components.values():
        tables.update(declared)
    return tuple(sorted(tables))


def table_owners(components):
    """表 → 组件（遗产保留表与登记表不出现在这里）。"""
    _validate_components(components)
    owners = {}
    for component, tables in components.items():
        for table in tables:
            owners[table] = component
    return owners


# --- 接管回执 ---------------------------------------------------------------


def receipt_checksum(component, metadata):
    """回执自身指纹：只跟组件名与回执内容走。"""
    payload = json.dumps(
        {"component": component, "metadata": metadata},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def adoption_receipt(component, tables, legacy_versions, legacy_checksums,
                     catalog_fingerprint):
    """构造一条接管回执（纯函数；不碰数据库）。

    契约（`validate_receipt()` 逐条校验）：

    - `version = 0`（保留号），`origin = adopted`；
    - `checksum` = 组件名 + 回执内容的 SHA256；
    - `catalog_fingerprint` = 接管时刻的结构指纹，必填；
    - `metadata` 必须且只能有 `adopted_from` / `legacy_versions` /
      `legacy_checksums` / `tables` 四键；`adopted_from` 固定 `legacy-pg-chain`，
      版本与指纹逐条对应，`tables` 是该组件登记的表全集。
      未安装插件的表按冻结归属进入 `retained`，不扩展回执格式。
    """
    if not is_component_name(component):
        raise ReceiptError(f"无效组件名: {component!r}")
    if not isinstance(catalog_fingerprint, str) or not catalog_fingerprint:
        raise ReceiptError("接管回执必须带 catalog_fingerprint")
    metadata = {
        "adopted_from": ADOPTED_FROM_LEGACY_CHAIN,
        "legacy_versions": list(legacy_versions),
        "legacy_checksums": dict(legacy_checksums),
        "tables": list(tables),
    }
    normalized = validate_metadata(component, metadata)
    return {
        "component": component,
        "version": ADOPTION_VERSION,
        "description": "adopted_from:" + ADOPTED_FROM_LEGACY_CHAIN,
        "origin": ORIGIN_ADOPTED,
        "checksum": receipt_checksum(component, normalized),
        "catalog_fingerprint": catalog_fingerprint,
        "metadata": normalized,
    }


def validate_metadata(component, metadata):
    """校验并规范化回执 metadata；返回排序后的新对象。"""
    if not isinstance(metadata, dict) or set(metadata) != set(RECEIPT_KEYS):
        raise ReceiptError(
            "接管回执 metadata 必须且只能包含 " + ", ".join(RECEIPT_KEYS)
        )
    if metadata["adopted_from"] != ADOPTED_FROM_LEGACY_CHAIN:
        raise ReceiptError(f"未知接管来源: {metadata['adopted_from']!r}")
    versions = metadata["legacy_versions"]
    if (not isinstance(versions, list) or not versions
            or any(type(v) is not int or v < pg_migrations.PG_SCHEMA_VERSION
                   for v in versions)
            or versions != sorted(set(versions))):
        raise ReceiptError("legacy_versions 必须是不重复递增的遗产链版本号")
    checksums = metadata["legacy_checksums"]
    if not isinstance(checksums, dict) or set(checksums) != {str(v) for v in versions}:
        raise ReceiptError("legacy_checksums 必须与 legacy_versions 一一对应")
    for value in checksums.values():
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ReceiptError("legacy_checksums 必须是小写十六进制 SHA256")
    tables = metadata["tables"]
    if (not isinstance(tables, list) or not tables
            or any(not isinstance(t, str) or not t.strip() for t in tables)
            or tables != sorted(set(tables))):
        raise ReceiptError("回执 tables 必须是不重复的升序表名数组")
    return {
        "adopted_from": ADOPTED_FROM_LEGACY_CHAIN,
        "legacy_versions": list(versions),
        "legacy_checksums": {str(v): checksums[str(v)] for v in versions},
        "tables": list(tables),
    }


def validate_receipt(row):
    """校验一条登记行是不是合法接管回执；返回规范化后的行。"""
    if not isinstance(row, dict):
        raise ReceiptError("登记行必须是映射")
    component = row.get("component")
    if not is_component_name(component):
        raise ReceiptError(f"无效组件名: {component!r}")
    if row.get("version") != ADOPTION_VERSION:
        raise ReceiptError("接管回执的 version 必须是 0")
    if row.get("origin") != ORIGIN_ADOPTED:
        raise ReceiptError("接管回执的 origin 必须是 adopted")
    checksum = row.get("checksum")
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        raise ReceiptError("回执 checksum 必须是 SHA256")
    fingerprint = row.get("catalog_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ReceiptError("回执缺少 catalog_fingerprint")
    metadata = _metadata_dict(row.get("metadata"))
    normalized = validate_metadata(component, metadata)
    if checksum != receipt_checksum(component, normalized):
        raise ReceiptError("回执 checksum 与内容不符（回执被改过）")
    return {"component": component, "version": ADOPTION_VERSION,
            "origin": ORIGIN_ADOPTED, "checksum": checksum,
            "catalog_fingerprint": fingerprint, "metadata": normalized}


def _metadata_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError as exc:
            raise ReceiptError("metadata 不是合法 JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise ReceiptError("metadata 必须是对象")


# --- 目录指纹 ---------------------------------------------------------------


_COLUMNS_SQL = """
SELECT c.relname::text AS table_name,
       a.attname::text AS column_name,
       format_type(a.atttypid, a.atttypmod) AS type_name,
       a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = %s AND c.relname::text = ANY(%s::text[])
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT c.relname::text AS table_name,
       con.conname::text AS constraint_name,
       con.contype::text AS constraint_type,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname::text = ANY(%s::text[])
ORDER BY c.relname, con.conname
"""

_INDEXES_SQL = """
SELECT t.relname::text AS table_name,
       i.relname::text AS index_name,
       pg_get_indexdef(ix.indexrelid) AS definition
FROM pg_index ix
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = %s AND t.relname::text = ANY(%s::text[])
ORDER BY t.relname, i.relname
"""

_SEQUENCES_SQL = """
SELECT t.relname::text AS table_name,
       s.relname::text AS sequence_name,
       format_type(q.seqtypid, NULL) AS data_type,
       q.seqstart AS start_value,
       q.seqincrement AS increment_by,
       q.seqmin AS min_value,
       q.seqmax AS max_value,
       q.seqcache AS cache_size,
       q.seqcycle AS cycle,
       a.attname::text AS owned_column
FROM pg_class t
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_depend d ON d.refobjid = t.oid AND d.deptype IN ('a', 'i')
JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
JOIN pg_sequence q ON q.seqrelid = s.oid
LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
WHERE n.nspname = %s AND t.relname::text = ANY(%s::text[])
ORDER BY t.relname, s.relname
"""


def _normalize_definition(definition, schema):
    """把非 public schema 的限定名还原成 public（只有索引定义会带限定名）。"""
    if not isinstance(definition, str) or schema == "public":
        return definition
    return definition.replace(' ON %s.' % schema, ' ON public.')


def catalog_entries(conn, tables, *, schema="public"):
    """读取给定表的结构条目：列（按位置）/约束/索引/序列，全部排序确定。

    `schema` 仅供接管时比对一次性标准 schema；报告路径固定读 `public`。
    """
    names = list(tables)
    entries = {
        table: {"columns": [], "constraints": [], "indexes": [], "sequences": []}
        for table in names
    }
    for row in conn.execute(_COLUMNS_SQL, (schema, names)).fetchall():
        entries[row["table_name"]]["columns"].append([
            row["column_name"], row["type_name"],
            bool(row["not_null"]), row["default_expr"],
        ])
    for row in conn.execute(_CONSTRAINTS_SQL, (schema, names)).fetchall():
        entries[row["table_name"]]["constraints"].append([
            row["constraint_name"], row["constraint_type"], row["definition"],
        ])
    for row in conn.execute(_INDEXES_SQL, (schema, names)).fetchall():
        entries[row["table_name"]]["indexes"].append([
            row["index_name"], _normalize_definition(row["definition"], schema),
        ])
    for row in conn.execute(_SEQUENCES_SQL, (schema, names)).fetchall():
        entries[row["table_name"]]["sequences"].append(_sequence_entry(row))
    return entries


def _sequence_entry(row):
    return [
        row["sequence_name"], row["data_type"], row["start_value"],
        row["increment_by"], row["min_value"], row["max_value"],
        row["cache_size"], bool(row["cycle"]), row["owned_column"],
    ]


def catalog_fingerprint(entries):
    """结构条目的稳定指纹（纯函数）：列顺序、类型、非空、默认、约束、索引、序列。"""
    if not isinstance(entries, dict):
        raise ComponentRegistryError("catalog entries 必须是映射")
    canonical = {}
    for table in sorted(entries):
        spec = entries[table]
        canonical[table] = {
            "columns": [list(column) for column in spec.get("columns", [])],
            "constraints": [list(item) for item in spec.get("constraints", [])],
            "indexes": [list(item) for item in spec.get("indexes", [])],
            "sequences": [list(item) for item in spec.get("sequences", [])],
        }
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- 纯校验 -----------------------------------------------------------------


def expected_from_migrations(migrations):
    """迁移计划 → {component: {version: checksum}}。"""
    expected = {}
    for component, items in migrations.items():
        for version, _description, statements in items:
            expected.setdefault(component, {})[version] = pg_migrations.checksum(statements)
    return expected


def official_expected():
    """正式期望版本集：kernel 基线 + `COMPONENT_MIGRATIONS` 的后续增量。"""
    return expected_from_migrations(COMPONENT_MIGRATIONS)


def check_registry(rows, components, present_tables, *, legacy_chain_present,
                   expected=None, catalogs=None, registry_present=True,
                   legacy_identity=None):
    """纯校验：登记行 + 组合声明 + 实际 catalog → `{ok, issues}`。

    `expected` 为 None 时用 `official_expected()`；显式传空映射只用于取证。

    接管与增量的共存规则：接管回执（version 0）**覆盖**组件声明的基线版本
    （当前只有 kernel 的 1），所以接管库不需要重放基线 CREATE，也不会因缺
    v1 报告 `missing-version`；反过来，接管库再登记 v1 就是双基线，判
    `double-baseline`。合法增量从 v2 起，只需 checksum 与代码一致。

    fail closed 的条目（`issues[].kind`）：

    - `registry-missing`：登记表不存在（空库/未 migrate）；
    - `unknown-component`：登记表里有组合没声明的组件；
    - `invalid-receipt`：回执格式不合法，或 checksum 与内容不符（被改过）；
    - `double-baseline`：接管回执与基线 applied 行同时存在（重复建基线）；
    - `receipt-tables-drift`：回执登记的表与当前组合声明不一致；
    - `catalog-drift` / `catalog-fingerprint-missing`：当前结构与最新证明不符/缺失；
    - `missing-version` / `checksum-drift`：相对期望迁移计划缺版本或指纹不符；
    - `unknown-version`：登记了期望版本集之外的 applied 版本（如伪造 999）；
    - `legacy-chain-incomplete` / `legacy-chain-drift`：遗产链版本或指纹与冻结代码不符；
    - `adoption-receipt-missing`：遗产链已在库里，但某组件既无回执也无迁移记录；
    - `missing-table` / `extra-table`：目录与声明不一致（额外表不能悄悄漏备）。
    """
    _validate_components(components)
    expected = official_expected() if expected is None else expected
    catalogs = catalogs or {}
    issues = []
    present = set(present_tables)
    whitelist = set(table_whitelist(components))
    # 已接管但插件已移出的组件：表进 retained，不判 unknown-component。
    retained_components = retained_component_tables(rows, components)
    whitelist |= {table for tables in retained_components.values() for table in tables}

    if not registry_present:
        issues.append({"kind": "registry-missing"})

    by_component = {}
    for row in rows:
        component = row.get("component")
        if component not in components and component not in retained_components:
            issues.append({"kind": "unknown-component", "component": component})
            continue
        by_component.setdefault(component, []).append(row)

    for component in expected:
        if component not in components and component not in retained_components:
            raise UnknownComponent(f"迁移计划含未声明组件: {component}")

    for component in sorted(by_component):
        items = by_component[component]
        receipts = [row for row in items if row.get("origin") == ORIGIN_ADOPTED]
        applied_rows = [row for row in items if row.get("origin") != ORIGIN_ADOPTED]
        adopted = bool(receipts)
        if adopted and any(int(row["version"]) <= KERNEL_BASELINE_VERSION
                           for row in applied_rows):
            issues.append({"kind": "double-baseline", "component": component})
        for row in receipts:
            try:
                normalized = validate_receipt(row)
            except ReceiptError as exc:
                issues.append({"kind": "invalid-receipt", "component": component,
                               "detail": str(exc)})
                continue
            expected_tables = (list(components[component]) if component in components
                               else retained_components.get(component))
            if normalized["metadata"]["tables"] != expected_tables:
                issues.append({"kind": "receipt-tables-drift", "component": component})
        # 最新 applied 行证明该组件升级后的结构；接管回执保持最初证据不变。
        # 没有后续增量时才以 adopted 回执作为当前结构基准。
        current = catalogs.get(component)
        if current is not None:
            latest = max(applied_rows, key=lambda row: int(row["version"]), default=None)
            proof = latest if latest is not None else (receipts[0] if receipts else None)
            fingerprint = proof.get("catalog_fingerprint") if proof is not None else None
            if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
                issues.append({"kind": "catalog-fingerprint-missing", "component": component})
            elif current != fingerprint:
                issues.append({"kind": "catalog-drift", "component": component})

    for component in sorted(expected):
        versions = {int(row["version"]): row for row in by_component.get(component, [])
                    if row.get("origin") != ORIGIN_ADOPTED}
        adopted = any(row.get("origin") == ORIGIN_ADOPTED
                      for row in by_component.get(component, []))
        for version in sorted(expected[component]):
            row = versions.get(version)
            if row is None:
                # 接管回执覆盖基线：旧库不重放基线 CREATE，也不算缺版本。
                if adopted and version == KERNEL_BASELINE_VERSION:
                    continue
                issues.append({"kind": "missing-version", "component": component,
                               "version": version})
            elif row.get("checksum") != expected[component][version]:
                issues.append({"kind": "checksum-drift", "component": component,
                               "version": version})

    # retained 也要拒绝未登记在代码期望集中的增量，不能借移出插件绕过版本核验。
    for component in sorted(by_component):
        known = set(expected.get(component, {}))
        for row in by_component[component]:
            version = int(row["version"])
            if row.get("origin") == ORIGIN_ADOPTED:
                continue
            if version not in known:
                issues.append({"kind": "unknown-version", "component": component,
                               "version": version})

    if legacy_identity is not None:
        if legacy_identity.get("versions_missing") or legacy_identity.get("versions_unknown"):
            issues.append({"kind": "legacy-chain-incomplete",
                           "missing": list(legacy_identity.get("versions_missing") or []),
                           "unknown": list(legacy_identity.get("versions_unknown") or [])})
        if legacy_identity.get("checksum_mismatches"):
            issues.append({"kind": "legacy-chain-drift",
                           "versions": list(legacy_identity["checksum_mismatches"])})
    if legacy_chain_present:
        # 遗产链在库就必须有接管回执或组件迁移；「没登记」不能当成「已接管」。
        for component, tables in components.items():
            if not tables or by_component.get(component):
                continue
            issues.append({"kind": "adoption-receipt-missing", "component": component})

    for component, tables in {**components, **retained_components}.items():
        for table in tables:
            if table not in present:
                issues.append({"kind": "missing-table", "component": component,
                               "table": table})
    for table in sorted(present - whitelist):
        issues.append({"kind": "extra-table", "table": table})

    return {"ok": not issues, "issues": issues}


# --- 数据库入口（调用方负责事务与维护锁） -------------------------------------


def ensure_registry_table(conn):
    """建登记表（幂等，DDL）。只允许 owner CLI 在迁移锁下调用。"""
    conn.execute(REGISTRY_DDL)


def registry_rows(conn):
    """登记行；表不存在返回 None（权限错误照常上抛）。"""
    try:
        with conn.transaction():
            return conn.execute(
                "SELECT component, version, description, checksum, applied_at, "
                "origin, catalog_fingerprint, metadata "
                "FROM component_schema_migrations ORDER BY component, version"
            ).fetchall()
    except Exception as exc:
        if pg_migrations.is_missing_table_error(exc):
            return None
        raise


def registry_report(conn, components=None, expected=None, *, check_catalog=True):
    """只读核对：登记行 + 组合声明 + 实际 catalog → 可安全打印的结构。

    **纯只读**：只用 SELECT 与 `SHOW`；不建表、不做 DDL、不重放标准 catalog、
    不改会话参数（没有 `SET`/`SET LOCAL`）。因此可以在备份的
    `REPEATABLE READ READ ONLY` + `SET TRANSACTION SNAPSHOT` 事务里由受限账号
    调用；独立标准 catalog 只在接管（owner 写路径）里构造。

    输出形状（备份/升级协调方直接消费，不必再读注册表）：

        {
          "engine": "postgres",
          "ok": bool, "issues": [...],
          "registry_present": bool,
          "baseline_version": 1,
          "expected_versions": {component: [version, ...]},
          "legacy_chain_versions": [...],
          "legacy_chain_ok": bool | None,
          "present_tables": [...],        # 实际存在的 public 表
          "retained_tables": [...],       # 未安装插件的历史表 + 遗产保留表
          "retained_components": {组件: [table, ...]},  # 已接管但插件已移出
          "tables": {component: [table, ...]},
          "table_owners": {table: component},
          "table_whitelist": [...],       # 允许全集（含 retained 与登记表）
          "components": [{component, version, origin, checksum,
                          catalog_fingerprint, applied_at, description,
                          metadata}, ...]
        }
    """
    declared = _validate_components(
        declared_components() if components is None else components
    )
    rows = registry_rows(conn)
    legacy = pg_migrations.applied(conn)
    legacy_versions = sorted(v for v in legacy if v >= pg_migrations.PG_SCHEMA_VERSION)
    present_tables = pg_migrations.table_names(conn)
    legacy_identity = None
    # 接管回执依赖冻结遗产链；版本表整体缺失不能把既有库伪装成组件新库。
    requires_legacy = any(row.get("origin") == ORIGIN_ADOPTED for row in (rows or []))
    if "schema_migrations" in present_tables or requires_legacy:
        recorded = {
            version: {"checksum": row.get("checksum")}
            for version, row in legacy.items() if version >= pg_migrations.PG_SCHEMA_VERSION
        }
        legacy_identity = pg_migrations.migration_identity(recorded)

    retained_components = retained_component_tables(rows, declared)
    catalogs = {}
    if check_catalog and rows:
        for component in sorted({row.get("component") for row in rows}):
            # applied 基线/升级与已移出插件均需核对当前结构。
            tables = declared.get(component) or retained_components.get(component)
            if not tables or not set(tables) <= set(present_tables):
                continue
            catalogs[component] = catalog_fingerprint(catalog_entries(conn, tables))

    effective_expected = official_expected() if expected is None else expected
    check = check_registry(
        rows or [], declared, present_tables,
        legacy_chain_present=bool(legacy_versions),
        expected=effective_expected,
        catalogs=catalogs, registry_present=rows is not None,
        legacy_identity=legacy_identity,
    )
    return {
        "engine": "postgres",
        "ok": check["ok"],
        "issues": check["issues"],
        "registry_present": rows is not None,
        "baseline_version": KERNEL_BASELINE_VERSION,
        "expected_versions": {
            component: sorted(versions)
            for component, versions in effective_expected.items()
        },
        "legacy_chain_versions": legacy_versions,
        "legacy_chain_ok": (legacy_identity["compatible"]
                            if legacy_identity is not None else None),
        "present_tables": list(present_tables),
        "retained_tables": sorted(
            set(retained_tables(declared))
            | {table for tables in retained_components.values() for table in tables}),
        "retained_components": {
            name: list(tables) for name, tables in retained_components.items()
        },
        "tables": {component: list(tables) for component, tables in declared.items()},
        "table_owners": table_owners(declared),
        "table_whitelist": list(table_whitelist(declared)),
        "components": [
            {
                "component": row["component"],
                "version": int(row["version"]),
                "origin": row["origin"],
                "description": row["description"],
                "checksum": row["checksum"],
                "catalog_fingerprint": row["catalog_fingerprint"],
                "applied_at": row["applied_at"],
                "metadata": _metadata_dict(row.get("metadata")),
            }
            for row in (rows or [])
        ],
    }


def _plan(migration_plan, components):
    """校验迁移计划：组件已声明、版本 ≥1 且递增、描述文本、语句非空。"""
    _validate_components(components)
    for component, items in migration_plan.items():
        if component not in components:
            raise UnknownComponent(f"迁移计划含未声明组件: {component}")
        versions = []
        for item in items:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ComponentRegistryError(f"{component}: 迁移项必须是 (version, description, statements)")
            version, description, statements = item
            if type(version) is not int or version < 1:
                raise ComponentRegistryError(f"{component}: 迁移版本必须是 ≥1 的整数")
            if not isinstance(description, str) or not description:
                raise ComponentRegistryError(f"{component}: 迁移描述不能为空")
            if not isinstance(statements, tuple) or not statements or any(
                not isinstance(s, str) or not s.strip() for s in statements
            ):
                raise ComponentRegistryError(f"{component}: 迁移语句必须是非空字符串元组")
            versions.append(version)
        if versions != sorted(set(versions)):
            raise ComponentRegistryError(f"{component}: 迁移版本必须递增且不重复")
    return migration_plan


def apply_components(conn, migration_plan=None, components=None):
    """按计划的插入顺序执行各组件的未应用迁移并登记（幂等）。

    调用方负责事务与维护锁（与 `pg_migrations.apply` 同一约定）：本函数自身
    不取锁、不提交。重复执行返回空 `applied`；已登记版本指纹不符则抛
    `ComponentChecksumDrift`，不静默跳过。

    起点分工：未接管的遗产库拒绝应用基线；已接管组件自动跳过由回执覆盖的
    基线，只追加后续增量。每次执行先核已有结构，新增 applied 行保存升级后
    catalog 指纹；原接管回执与冻结遗产链均不修改。
    """
    declared = _validate_components(
        declared_components() if components is None else components)
    plan = _plan(COMPONENT_MIGRATIONS if migration_plan is None else migration_plan, declared)
    # 基线只能从空业务库起步；IF NOT EXISTS 不能把已有错误结构登记成新标准。
    existing_rows = registry_rows(conn) or []
    if not existing_rows and any(version == KERNEL_BASELINE_VERSION
                                 for items in plan.values() for version, _, _ in items):
        business_tables = set(pg_migrations.table_names(conn)) - {REGISTRY_TABLE}
        if business_tables:
            raise ComponentRegistryError(
                "基线只能装进空库；已有业务表请先证明结构并 adopt")
    ensure_registry_table(conn)
    existing = {(row["component"], int(row["version"])): row for row in existing_rows}
    adopted_components = {row["component"] for row in existing_rows
                          if row.get("origin") == ORIGIN_ADOPTED}
    legacy = pg_migrations.applied(conn)
    for component in sorted(plan):
        if (legacy and component not in adopted_components
                and KERNEL_BASELINE_VERSION in {item[0] for item in plan[component]}):
            raise ComponentRegistryError(
                f"{component} 基线 v{KERNEL_BASELINE_VERSION} 只能装进空库；"
                "既有库请用 adopt 写接管回执（不重放 CREATE）"
            )
    # 先按已登记前缀核对，再执行缺失的增量；不能把已有漂移登记成新标准。
    if existing_rows:
        expected = expected_from_migrations(plan)
        prefix_expected = {
            component: {version: digest for version, digest in versions.items()
                        if version <= max((int(row["version"]) for row in existing_rows
                                           if row["component"] == component), default=0)
                        or (component in adopted_components and version == KERNEL_BASELINE_VERSION)}
            for component, versions in expected.items()
        }
        before = registry_report(conn, declared, expected=prefix_expected)
        if not before["ok"]:
            raise ComponentRegistryError(
                "迁移前组件登记/结构不兼容: "
                + json.dumps(before["issues"][:3], ensure_ascii=False))
    applied_now = []
    skipped = []
    for component, items in plan.items():
        for version, description, statements in items:
            if component in adopted_components and version == KERNEL_BASELINE_VERSION:
                skipped.append({"component": component, "version": version})
                continue
            digest = pg_migrations.checksum(statements)
            row = existing.get((component, version))
            if row is not None:
                if row["checksum"] != digest:
                    raise ComponentChecksumDrift(
                        f"{component} 版本 {version} 的 SQL 指纹与记录不符："
                        f"记录 {row['checksum'][:12]}… 现算 {digest[:12]}…；"
                        "已登记迁移不得修改，请新增版本号"
                    )
                skipped.append({"component": component, "version": version})
                continue
            for statement in statements:
                conn.execute(statement)
            fingerprint = catalog_fingerprint(catalog_entries(conn, declared[component]))
            conn.execute(
                "INSERT INTO component_schema_migrations "
                "(component, version, description, checksum, origin, catalog_fingerprint) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (component, version, description, digest, ORIGIN_APPLIED, fingerprint),
            )
            applied_now.append({"component": component, "version": version,
                                "checksum": digest})
    return {"applied": applied_now, "skipped": skipped}


def _legacy_checksums(conn):
    """冻结遗产链的版本→指纹；不兼容（缺版本/指纹不符/未知版本）即拒绝。"""
    recorded = pg_migrations.applied(conn)
    identity = pg_migrations.migration_identity(recorded)
    if not identity["compatible"]:
        raise ComponentRegistryError(
            "冻结遗产链与代码不一致（缺版本/指纹不符/未知版本），拒绝登记接管："
            f"missing={identity['versions_missing']} "
            f"mismatches={identity['checksum_mismatches']} "
            f"unknown={identity['versions_unknown']}"
        )
    versions = sorted(v for v in recorded if v >= pg_migrations.PG_SCHEMA_VERSION)
    return versions, {str(v): recorded[v]["checksum"] for v in versions}


def adopt_legacy(conn, components=None):
    """为已在库的组件写接管回执：只登记结构，不重放 CREATE/seed，不 DROP。

    前置（全部失败都在写任何回执之前）：

    1. 冻结遗产链已完整应用且指纹/版本与代码一致（`_legacy_checksums`）；
    2. 每个有表组件的表都在 catalog 里；
    3. **目标结构等于冻结遗产链重放的独立标准 catalog**（列/默认/约束/索引/序列）；
       期望值不从待接管目标自身生成，也不为匹配指纹改历史 SQL 或放宽容差。

    重复执行幂等：同一回执再次计算得到同一 checksum 就跳过；内容不同则拒绝覆盖。
    调用方负责事务与维护锁；中途失败必须整体回滚，不留半份回执。
    """
    declared = _validate_components(
        declared_components() if components is None else components
    )
    ensure_registry_table(conn)
    versions, checksums = _legacy_checksums(conn)
    present = set(pg_migrations.table_names(conn))
    existing = {
        (row["component"], int(row["version"])): row
        for row in (registry_rows(conn) or [])
    }
    missing = {
        component: sorted(set(tables) - present)
        for component, tables in declared.items() if set(tables) - present
    }
    if missing:
        raise ComponentRegistryError(
            f"接管缺表 {missing}：先证明结构等价再登记，不猜"
        )
    verified = verify_legacy_structure(conn, declared)
    if not verified["ok"]:
        raise ComponentRegistryError(
            "目标结构与冻结遗产链标准不一致，拒绝写回执（先查结构漂移）："
            + json.dumps(verified["differences"][:3], ensure_ascii=False)
        )
    adopted_now = []
    skipped = []
    for component, tables in declared.items():
        if not tables:
            continue
        fingerprint = catalog_fingerprint(catalog_entries(conn, tables))
        receipt = adoption_receipt(
            component, sorted(tables), versions, checksums, fingerprint,
        )
        row = existing.get((component, ADOPTION_VERSION))
        if row is not None:
            if row["checksum"] != receipt["checksum"]:
                raise ComponentRegistryError(
                    f"{component} 已有不同的接管回执，拒绝覆盖（先查清结构漂移）"
                )
            skipped.append(component)
            continue
        conn.execute(
            "INSERT INTO component_schema_migrations "
            "(component, version, description, checksum, origin, "
            " catalog_fingerprint, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (receipt["component"], receipt["version"], receipt["description"],
             receipt["checksum"], receipt["origin"], receipt["catalog_fingerprint"],
             json.dumps(receipt["metadata"], ensure_ascii=False, sort_keys=True)),
        )
        adopted_now.append({"component": component, "checksum": receipt["checksum"],
                            "catalog_fingerprint": fingerprint})
    return {"adopted": adopted_now, "skipped": skipped, "legacy_versions": versions}


def _standard_catalog(conn, tables, statements):
    """在一次性 schema 里重放给定 DDL，读出独立标准 catalog。

    只在**接管**（owner 写路径）调用：需要 CREATE/DROP SCHEMA 权限。整段在
    一个事务里完成，读完立即 `DROP SCHEMA ... CASCADE`，不碰 `public`、不留
    残留；`search_path` 只用 `SET LOCAL`，随事务结束自动还原。
    """
    schema = "b7_standard_" + uuid.uuid4().hex[:8]
    with conn.transaction():
        conn.execute('CREATE SCHEMA "%s"' % schema)
        conn.execute('SET LOCAL search_path = "%s", public' % schema)
        for statement in statements:
            conn.execute(statement)
        entries = catalog_entries(conn, tables, schema=schema)
        conn.execute('DROP SCHEMA "%s" CASCADE' % schema)
    return {table: spec for table, spec in entries.items()}


def catalog_differences(expected, actual):
    """两份 catalog 的逐表逐项差异 → `[{table, key, expected, actual}]`（有界）。"""
    differences = []
    for table in sorted(set(expected) | set(actual)):
        left = expected.get(table) or {}
        right = actual.get(table) or {}
        for key in ("columns", "constraints", "indexes", "sequences"):
            if list(left.get(key) or []) != list(right.get(key) or []):
                differences.append({
                    "table": table, "key": key,
                    "expected": list(left.get(key) or [])[:20],
                    "actual": list(right.get(key) or [])[:20],
                })
    return differences


def verify_legacy_structure(conn, components, *, legacy_statements=None):
    """接管前证明：目标结构 == 冻结遗产链重放的独立标准 catalog。

    期望值**不来自待接管目标自身**：`legacy_statements` 由冻结的
    `pg_migrations.PG_MIGRATIONS` 提供（调用方不传就用它），在隔离 schema 里
    重放后逐组件比较。缺表、多列、默认/约束/索引/序列差异都返回有界差异，
    由 `adopt_legacy()` 在任何写操作之前拒绝。
    """
    _validate_components(components)
    statements = [statement
                  for _version, _description, group in pg_migrations.PG_MIGRATIONS
                  for statement in group] if legacy_statements is None else legacy_statements
    all_tables = tuple(sorted({table for tables in components.values() for table in tables}))
    standard = _standard_catalog(conn, all_tables, statements)
    actual = catalog_entries(conn, all_tables)
    differences = catalog_differences(standard, actual)
    if not differences:
        return {"ok": True, "differences": []}
    owners = table_owners(components)
    for difference in differences:
        difference["component"] = owners.get(difference["table"], "retained")
    return {"ok": False, "differences": differences}
