# -*- coding: utf-8 -*-
"""备份 manifest v2 的组合事实块：从**当前组合**收集，不从调用方传参补空。

备份必须能自证「我属于哪个组合、装了哪些组件、全表清单是什么、附件有哪些」。
这四块事实各有唯一来源：

- `composition`  ← `composition_identity`（构建产物身份文件，白名单字段）；
- `components`   ← `component_migrations.registry_report()`（组件登记表）；
- `tables`       ← 同一份登记报告的表归属（表名 → 组件）；
- `attachments`  ← 实际发布的附件成员（由 `write_pg_dump` 传入）。

刻意**不**在这里读插件目录、重算摘要或猜组件：那会造出第二份真相，与
运行中的库对不上时还看不出是哪边错。

边界：无组合身份文件时返回空 dict，由调用方决定是否降级 v1 —— 现役私人
部署还没接构建身份（B7-4 才接），本批不能因此让生产备份失败。
"""
import backup_contracts
import composition_identity


def composition_block(identity=None):
    """构建身份 → manifest 的 `composition` 块；无身份返回 None。"""
    document = composition_identity.current() if identity is None else identity
    if document is None:
        return None
    return {
        "kernel_version": document["kernel"]["version"],
        "composition_digest": document["composition_digest"],
        "plugins": [
            {"id": plugin["id"], "version": plugin["version"], "digest": plugin["digest"]}
            for plugin in document["plugins"]
        ],
    }


def components_block(report):
    """组件登记报告 → `components` 数组（按组件名、版本排序，形状确定）。"""
    if not isinstance(report, dict):
        raise ValueError("组件登记报告必须是映射")
    entries = report.get("components")
    if not isinstance(entries, list):
        raise ValueError("组件登记报告缺少 components")
    rows = []
    for entry in entries:
        rows.append({
            "name": entry["component"],
            "version": int(entry["version"]),
            "origin": entry["origin"],
            "checksum": entry["checksum"],
            "catalog_fingerprint": entry.get("catalog_fingerprint"),
        })
    rows.sort(key=lambda row: (row["name"], row["version"]))
    return rows


def check_report_usable(report, composition):
    """发布 v2 前对登记报告做**严格**校验（R07）；不合格有界拒绝，不降级。

    校验的是「这份报告配得上一份声称 complete 的 v2」：

    - `ok=true` 且 `registry_present=true`——失败的登记核验、未接管库都不能
      被包装成 complete v2（复核 R07 实测：`ok=false / catalog-drift` 曾被
      `_build_manifest` 接受）；
    - `components` 非空——登记为空是未完成迁移，不是「零组件」；
    - `table_owners` 非空——没有表归属就无法与实表对账；
    - kernel 组件必须在登记里。**不**要求构建声明的每个插件都有组件行
      （audit 等 `owns.tables=[]` 的插件不写组件回执，这是合法形态）；
      登记里有、构建身份里没有的插件组件是插件移出后的 retained 合法形态，
      由 `tables_block` 的 retained 集合对账，不在此处单向拒绝。
    """
    if not isinstance(report, dict):
        raise ValueError("组件登记报告必须是映射")
    if not report.get("registry_present"):
        raise ValueError("组件登记报告 registry_present=false：库未完成组件接管")
    if report.get("ok") is not True:
        issues = report.get("issues") or []
        kinds = sorted({issue.get("kind") for issue in issues
                        if isinstance(issue, dict) and issue.get("kind")})
        raise ValueError(f"组件登记报告 ok=false，拒绝发布 v2（issue: {kinds}）")
    entries = report.get("components")
    if not isinstance(entries, list) or not entries:
        raise ValueError("组件登记报告 components 为空：登记未完成迁移，拒绝发布 v2")
    owners = report.get("table_owners")
    if not isinstance(owners, dict) or not owners:
        raise ValueError("组件登记报告缺少 table_owners，无法与实表对账")
    names = {entry.get("component") for entry in entries}
    if "kernel" not in names:
        raise ValueError("组件登记缺少 kernel 组件")
    plugins = composition.get("plugins") if isinstance(composition, dict) else None
    if not isinstance(plugins, list):
        raise ValueError("构建身份缺少 plugins")
    declared = report.get("tables")
    if not isinstance(declared, dict):
        raise ValueError("组件登记报告缺少 tables 组合声明")
    selected = {"plugin:" + plugin["id"] for plugin in plugins}
    if set(declared) != {"kernel"} | selected:
        raise ValueError("组件报告声明与构建插件集合不符")
    retained = report.get("retained_components")
    if not isinstance(retained, dict) or names - set(declared) - set(retained):
        raise ValueError("组件登记含未经核验的移出插件")
    tables_block(report)
    return report


RETAINED = backup_contracts.RETAINED_TABLE_OWNER


def tables_block(report):
    """组件登记报告 → `tables`（表名 → 归属），覆盖导出 snapshot 内的**实表**。

    事实源是 `present_tables`（同一导出 snapshot 内现查的 public 实表，由
    registry_report 提供）：manifest 声明的是「这份备份里有什么」，不能把
    白名单里**尚未建表**的项也写成备份事实——零插件新装的合法报告就带 12 张
    未建的私人表，照抄白名单会让 manifest 虚报。

    归属判定：`table_owners`（表 → kernel/plugin:*）优先；不归任何组件的表
    必须出现在 `retained_tables`（登记表、panels、未选装插件的遗留表），标为
    `retained`。实表两个集合都进不去 = 报告自相矛盾；归属集合与实表对不上
    同样拒绝——伪造归属会让 manifest 撒谎，恢复方按它去找迁移脚本就会落空。

    `present_tables`、白名单与retained集合均须存在，不接受旧形状降级。
    """
    if not isinstance(report, dict):
        raise ValueError("组件登记报告必须是映射")
    owners = report.get("table_owners")
    if not isinstance(owners, dict):
        raise ValueError("组件登记报告缺少 table_owners")
    present = report.get("present_tables")
    retained = report.get("retained_tables")
    whitelist = report.get("table_whitelist")
    for name, value in (("present_tables", present), ("retained_tables", retained),
                        ("table_whitelist", whitelist)):
        if (not isinstance(value, list)
                or any(not isinstance(table, str) or not table for table in value)
                or len(set(value)) != len(value)):
            raise ValueError(f"组件登记报告的 {name} 必须是不重复表名数组")
    if not set(present) <= set(whitelist) or not set(owners) <= set(whitelist):
        raise ValueError("实表或表归属不在白名单内")
    owners_set, retained_set = set(owners), set(retained)
    overlap = sorted(owners_set & retained_set)
    if overlap:  # 一张表既归组件又算保留：报告内部矛盾，不猜
        raise ValueError(f"表同时归属组件与 retained: {overlap}")
    tables = {}
    for table in present:
        if table in owners_set:
            tables[table] = owners[table]
        elif table in retained_set:
            tables[table] = RETAINED
        else:
            raise ValueError(f"实表 {table} 既不属于任何组件也不在保留集合内")
    absent = sorted(owners_set - set(present))
    if absent:
        # 组件声明了表、导出 snapshot 里却没有：登记与实库脱节，不静默丢表。
        raise ValueError(f"组件声明表不在导出实表内: {absent}")
    return tables


def attachments_block(members):
    """已发布的附件成员 → `attachments` 数组；无附件返回空数组（不是 None）。"""
    if not members:
        return []
    rows = []
    for member in members:
        rows.append({
            "path": member["path"],
            "sha256": member["sha256"],
            "bytes": int(member["bytes"]),
        })
    rows.sort(key=lambda row: row["path"])
    return rows
