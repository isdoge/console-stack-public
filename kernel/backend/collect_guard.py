# -*- coding: utf-8 -*-
"""自动采集的实例门禁（提案 §3.1 / Q1：只由 NAS 采集；契约版本 `pg00-1`）。

为什么单独一个叶子模块：采集门禁要在**多个入口**（`probe_cache` 的两种探测、
`feed_aggregator` 的抓取、`node_traffic` 的记账、`version_sync` 的同步）判断同一件事。
放进 `runtime_mode` 会让它长出业务语义，放进 `app.py` 会让模块反向依赖应用。

本模块区分**两个不同的问题**（RO-01 的根因就是把它们合成一个判断）：

1. **本实例是不是采集者**（角色面）——只看 `CONSOLE_INSTANCE_ROLE`。它决定
   「观测请求本地执行还是转交权威端」，维护模式**不改变**这个身份：
   NAS 进入 `read_only` 仍然是权威端，观测 GET 照旧本地服务。
2. **现在允不允许真的去采集/写**（角色面 + 维护面）——采集、seed、同步、清理
   全都要写库，`read_only` 期间必须**在发起网络请求或写事务之前**跳过。
   底层写拒绝（`pgdb.pg_transaction` → `runtime_mode.require_writable`）保持不动，
   这一层只是让后台线程别去撞它。

**默认安全侧**：`runtime_mode.DEFAULT_INSTANCE_ROLE = "client"` 且
`DEFAULT_MAINTENANCE_MODE = "normal"`，所以**不设环境变量就不采集**。这与 Q1
「只由 NAS 自动采集」一致——漏配的 Mac 只会退化成只读观测，不会偷偷变成第二个采集者。

门禁只管**自动采集与初始化动作**，不管业务写：Mac 的账本编辑、布局保存、书签增删
仍由 `runtime_mode.require_writable()` 与 PG 权限面约束（D8：NAS/Mac 对等业务读写）。
"""


class CollectionNotAllowed(RuntimeError):
    """本实例/当前模式不允许执行自动采集。HTTP 层不应把它当 500 抛给用户。"""


def is_collector():
    """本实例是否是权威采集者（**只看角色**，不看维护模式）。

    用于「本地执行 vs 转交权威端」的分流：维护模式不改变实例身份——NAS 在
    `read_only` 下仍是权威端，观测类 GET 必须由它自己服务，不能转交给自己。
    """
    import runtime_mode

    return runtime_mode.is_authority()


def collection_allowed():
    """本实例**现在**是否允许执行自动采集 / seed / 同步 / 清理。

    `read_only` 维护模式下一律不允许：这些动作都要写库（§9 D.5「开放前新 NAS/Mac
    以 read_only 启动，所有自动采集/seed 关闭」）。RO-01 首红就是因为这里只看角色，
    于是 `seed_workstation_data()` 在 read_only 下仍然进写事务，`ReadOnlyMode`
    直接把进程掀翻、应用根本没监听 HTTP。
    """
    import runtime_mode

    if not runtime_mode.is_authority():
        return False
    return not runtime_mode.is_read_only()


def collection_skip_reason():
    """不允许执行采集动作的原因（脱敏，供日志与诊断）；允许时返回 None。"""
    import runtime_mode

    if not runtime_mode.is_authority():
        return "本实例不是 authority，自动采集只在 NAS 运行"
    if runtime_mode.is_read_only():
        return "维护模式 read_only：自动采集/seed/清理已暂停（底层写拒绝不受影响）"
    return None


def require_collection(what="自动采集"):
    """采集入口的统一门禁；不允许时抛 `CollectionNotAllowed`。

    调用时机：**发起任何网络请求或写库之前**——拒绝得越早，越不会留下半条采集痕迹。
    """
    if not collection_allowed():
        raise CollectionNotAllowed(
            f"{what} 当前不可执行：{collection_skip_reason()}"
        )
    return True


def collection_skipped(what):
    """跳过采集时的统一日志文案（脱敏，不含环境变量原值以外的信息）。"""
    return f"[collect] skip {what}: {collection_skip_reason() or '采集门禁未放行'}"
