# -*- coding: utf-8 -*-
"""组合层附件备份适配（B7-2 隔离验证批次）。

PG dump 本身只覆盖数据库。私人组合里还有「原件」——账单导入的 Excel/CSV、
reference 历史、raw 回执目录——它们必须与同一导出 snapshot 的批次记录同组
备份，否则恢复出来的库会指向不存在的文件。公共组合没有原件，就不该凭空产出
一个空的 Pay 包（那会让「有没有附件」不可判别）。

本模块只做编排，不认识任何具体附件格式：

- `ATTACHMENT_ADAPTERS` 是**唯一**的组合→适配器清单，值是适配器模块名；
- 适配器按需**懒导入**，公共组合不 import 私人依赖；
- 适配器只需实现 `write_snapshot(dest, *, root, query, attach_root) ->
  {"path", "sha256", "bytes"}`，与 `backup_pay.write_pay_snapshot` 同签名；
- 组合没有声明附件（`plugins/composition.json` 里无任何插件 `owns.files`，
  且未显式设置 `CONSOLE_ATTACHMENT_ADAPTER`）时，`write_attachments` 返回 `None`，
  调用方据此不写附件成员、不写附件文件。

边界：

- 不连库、不发布文件：`write_attachments` 只写到调用方给的临时路径，发布仍由
  `pg_backup.publish_group` 在完整标记之前统一完成。
- 不改变已有 `pg-backup-manifest-1`：本批只提供成员构造函数，是否写进 manifest
  由 `backup_db.write_pg_dump` 的调用方决定（B7-2 剩余切片接线）。
- 附件根必须是组合 backup 根之外的独立目录：把它放进备份根会让每次备份
  把上一次的备份再打包一遍。
"""
import importlib
import json
import os
from pathlib import Path

# 组合 → 适配器契约。新增附件种类只改这里：
#   module —— 可懒导入的模块名；
#   writer —— 写快照函数名，签名 `(dest, *, root, query, <root_kwarg>=None)`；
#   root_kwarg —— 该函数接收原件根的关键字名（各适配器历史命名不同，显式对齐
#                 而不去猜，避免「函数名像就当成能用」）；
#   suffix —— 归档成员后缀。
ATTACHMENT_ADAPTERS = {
    "pay": {
        "module": "backup_pay",
        "writer": "write_pay_snapshot",
        "root_kwarg": "pay_root",
        "suffix": ".pay.tar",
    },
}
ATTACHMENT_ENV = "CONSOLE_ATTACHMENT_ADAPTER"

# 插件 id → 附件适配器名。**只有**与 PG 批次记录耦合、必须与同一导出 snapshot
# 同组备份的原件目录才登记在这里；普通配置文件不在此列。
ADAPTER_FOR_PLUGIN = {
    "ledger": "pay",
}

# `owns.files` 非空、但**不**产出独立归档的插件：它们的数据由别的机制覆盖，
# 不登记会让「有文件就有归档」的推断把真实组合判成多来源。
NON_ARCHIVE_FILE_OWNERS = {
    "panels": "panels.yaml 由 backup_runtime 的部署快照组覆盖",
    "personal_workbench": "个人工作台目录不属于 PG 批次原件",
}

_FORMAT = "attachment-adapter-1"


class AttachmentError(RuntimeError):
    """附件组合不可判定或与适配器契约不符：有界失败，不猜。"""


def _adapter_name():
    """当前组合的附件适配器名；无声明返回 None。

    优先级：显式 `CONSOLE_ATTACHMENT_ADAPTER` > 组合声明。显式值为空串表示
    「明确无附件」，与「未设置」区分——否则公共组合无法压掉私人组合声明。
    """
    raw = os.environ.get(ATTACHMENT_ENV)
    if raw is not None:
        name = raw.strip()
        if not name:
            return None
        if name not in ATTACHMENT_ADAPTERS:
            raise AttachmentError(
                f"{ATTACHMENT_ENV} 不是已登记的附件适配器: {name!r}"
                f"（只允许 {', '.join(sorted(ATTACHMENT_ADAPTERS))}）"
            )
        return name
    from_composition = composition_attachment_name()
    return from_composition


def composition_attachment_name(plugin_root=None):
    """从插件清单推断附件适配器名；无耦合原件返回 None。

    **不看「有没有 `owns.files`」**：真实组合里 `panels.yaml` 与个人工作台
    目录也声明文件，但它们不是 PG 批次原件。判据是插件 id 是否登记在
    `ADAPTER_FOR_PLUGIN`；声明了文件却没登记的插件必须显式归类到
    `NON_ARCHIVE_FILE_OWNERS`，否则 fail closed —— 新插件悄悄带来一份
    未备份的原件目录，比报错危险得多。
    """
    import composition

    root = Path(plugin_root) if plugin_root is not None else _default_plugin_root()
    declared = composition.plugin_sources(root)
    kinds = set()
    for pid in declared:
        manifest_path = root / pid / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AttachmentError(f"{pid}: 清单不可读取") from exc
        owns = data.get("owns") if isinstance(data, dict) else None
        files = owns.get("files") if isinstance(owns, dict) else None
        if not files:
            continue
        if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
            raise AttachmentError(f"{pid}: owns.files 必须是字符串数组")
        adapter_name = ADAPTER_FOR_PLUGIN.get(pid)
        if adapter_name is None:
            if pid in NON_ARCHIVE_FILE_OWNERS:
                continue
            raise AttachmentError(
                f"{pid} 声明了 owns.files 但没有附件归档归属："
                "登记到 ADAPTER_FOR_PLUGIN，或说明为何不需要归档后加入 "
                "NON_ARCHIVE_FILE_OWNERS"
            )
        kinds.add(adapter_name)
    if not kinds:
        return None
    if len(kinds) > 1:
        raise AttachmentError(
            "组合声明了多种附件来源 "
            + ", ".join(sorted(kinds))
            + f"；请显式设置 {ATTACHMENT_ENV}"
        )
    return kinds.pop()


def _default_plugin_root():
    import plugin_imports

    return plugin_imports.plugin_root()


def adapter(name=None):
    """按名字取 `(模块, 写快照函数)`（懒导入）；名字为 None 表示无附件。"""
    name = _adapter_name() if name is None else name
    if name is None:
        return None
    spec = ATTACHMENT_ADAPTERS.get(name)
    if spec is None:
        raise AttachmentError(f"未登记的附件适配器: {name!r}")
    try:
        module = importlib.import_module(spec["module"])
    except ImportError as exc:
        raise AttachmentError(
            f"附件适配器 {spec['module']} 不可导入：{name} 组合缺少对应实现"
        ) from exc
    writer = getattr(module, spec["writer"], None)
    if not callable(writer):
        raise AttachmentError(
            f"附件适配器 {spec['module']} 缺少写快照函数 {spec['writer']}"
        )
    return module, writer


def suffix(name=None):
    """附件成员后缀；无附件返回 None。"""
    name = _adapter_name() if name is None else name
    if name is None:
        return None
    try:
        return ATTACHMENT_ADAPTERS[name]["suffix"]
    except KeyError:
        raise AttachmentError(f"未登记的附件后缀: {name!r}") from None


def attach_root(name=None, *, env="CONSOLE_PAY_DATA_DIR"):
    """附件原件根；未配置返回 None（由适配器决定是否用默认值）。

    只做 expanduser，**不** resolve：软链接是适配器要拒绝的理由，解析掉就
    看不见了。相对路径在这里就拒绝——备份进程的 cwd 与部署目录无关，
    相对路径会把原件根悄悄指到别处。
    """
    name = _adapter_name() if name is None else name
    if name is None:
        return None
    if name == "pay":
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise AttachmentError(f"{env} 必须是绝对路径: {raw!r}")
        return path
    return None


def _backup_root():
    """组合 backup 根；无组合配置返回 None。

    组合根只是**附加**防线（R06）：守卫的主判据是调用方实际输出目录，这里
    读不到组合配置时守卫**不会**因此被绕过。
    """
    import composition

    try:
        return Path(composition.root_path("backup"))
    except Exception:
        return None


def _resolve_strict(path):
    """解析路径；解析失败（含 OSError/RuntimeError）直接抛 AttachmentError。

    R06：以前 resolve 失败会静默跳过守卫——「看不清就当没看见」正是把旧备份
    当原件打包的入口。现在任何一侧解析不了都 fail closed。
    """
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise AttachmentError(f"附件守卫无法解析路径 {path!r}：{exc}") from exc


def _check_separate(attach, backup, *, backup_label="备份根"):
    """附件根与备份目录必须可解析且相互独立（相等/任一嵌套都拒绝）。

    双向都判：原件根放进备份树会把旧备份当原件，备份目录放进原件根则让
    备份产物混进原件目录——两个方向都是「下一轮备份打包上一轮」的形态。
    """
    if attach is None or backup is None:
        return
    attach_resolved = _resolve_strict(attach)
    backup_resolved = _resolve_strict(backup)
    if (attach_resolved == backup_resolved
            or backup_resolved in attach_resolved.parents
            or attach_resolved in backup_resolved.parents):
        raise AttachmentError(
            f"附件原件根 {attach_resolved} 与{backup_label} {backup_resolved} "
            "相互嵌套或相同，拒绝把备份产物当作原件打包"
        )


def _pay_effective_root(root, attach):
    """pay 适配器**最终生效**的原件根（与 backup_pay.write_pay_snapshot 同一解析）。

    R06 的关键：`CONSOLE_PAY_DATA_DIR` 未设置时适配器自己会回退到
    `<root>/console/backend/data/pay`——守卫若只看 `attach_root()` 的 None，
    就查不到这个实际生效的根。必须按适配器同一套默认值对账。
    """
    if attach is not None:
        return attach
    return Path(root) / "console/backend/data/pay"


def _effective_attach_root(name, root, attach):
    """适配器最终生效的原件根；未配置且无默认规则时返回 None（无从守卫）。"""
    if name == "pay":
        return _pay_effective_root(root, attach)
    return attach


def write_attachments(dest, *, root, query, name=None):
    """把当前组合的原件写成 `dest` 处的单个归档；无附件组合返回 None。

    `query` 必须读取调用方**已导出的同一个 PG snapshot**，否则归档里的批次
    记录会与 dump 不一致。返回成员描述（含 `path`/`sha256`/`bytes`），形状与
    `pg_backup.publish_group` 的输入一致。

    守卫（R06）比较**调用方实际输出目录**（`dest.parent`）与适配器最终生效的
    原件根——不是组合声明的 backup 根：定时备份的实际输出目录与组合根可能
    不同（`OPS_BACKUP_DIR` 覆盖、`timed_backup_dir` 重定向），以声明的根做
    判据会把旧备份文件当成原件。守卫在任何写入发生**之前**执行，解析失败
    有界拒绝，不静默跳过。
    """
    name = _adapter_name() if name is None else name
    if name is None:
        return None
    _module, writer = adapter(name)
    dest = Path(dest)
    attach = attach_root(name)
    effective = _effective_attach_root(name, root, attach)
    # 主判据：实际输出目录 × 实际生效原件根。两边都必须可解析，失败即拒绝。
    _check_separate(effective, dest.parent, backup_label="实际输出目录")
    # 附加防线：组合声明的 backup 根（读不到时不算通过，只不算这条证据）。
    _check_separate(effective, _backup_root(), backup_label="组合备份根")
    member = writer(dest, root=Path(root), query=query,
                    **{ATTACHMENT_ADAPTERS[name]["root_kwarg"]: attach})
    if not isinstance(member, dict):
        raise AttachmentError(f"{name} 适配器没有返回成员描述")
    for key in ("path", "sha256", "bytes"):
        if key not in member:
            raise AttachmentError(f"{name} 适配器成员描述缺少 {key}")
    if Path(member["path"]).name != dest.name:
        raise AttachmentError(
            f"{name} 适配器写的文件名与请求不符: {member['path']} != {dest.name}"
        )
    if dest.is_symlink() or not dest.is_file():
        raise AttachmentError(f"{name} 适配器没有在 {dest} 生成普通文件")
    return member


def describe(name=None):
    """当前组合的附件面（可安全打印，不含路径原文）。"""
    name = _adapter_name() if name is None else name
    if name is None:
        return {"adapter": None, "suffix": None, "files": False, "format": _FORMAT}
    return {
        "adapter": name,
        "suffix": suffix(name),
        "files": True,
        "format": _FORMAT,
    }
