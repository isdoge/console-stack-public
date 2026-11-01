# -*- coding: utf-8 -*-
"""一键巡检报告（C1）：把散在各端点的运维事实聚合成一份能贴回 daily 的快照。

三条硬约定：

1. **只读，绝不触发探测**。关注项 / 服务健康走 peek 缓存，监控事件读 Console PG；
   机器探活取 `probe_history` 里最后一条**真实**探测记录，不调 check_machine_probe。
   容器运行态（docker ps）因此不进报告——查它必须真的连 docker，与本约定冲突。
2. **每段带 sampled_at，标的是采样时间不是生成时间**。口径统一为「该段最旧的采样
   时刻」（最坏新鲜度）：读缓存的段给缓存写入时刻，直读 DB 的段给生成时刻。一个
   都取不到时显式 null，不拿当前时间冒充——报告的价值全在「这个数有多旧」上。
3. **单源挂不打死报告**。每段独立 try，失败只在该段留 error 文本，其余照出。

`?format=json` 给 agent，`?format=markdown` 给人，两者同源于一份 build_report()。
JSON 里 observability 是独立一段；markdown 按固定骨架把它折进「关注项」——
骨架六节固定：总评 → 关注项 → 机器 → 服务 → 续费 → 运维状态。

时间一律 UTC（容器无 TZ，本地化会是假的），ISO-8601 带 `Z`；从别处原样带出的
字段（如 last_deploy.deployed_at）保留其自带偏移，不改写。
"""
import datetime
from datetime import timezone

from flask import Response, jsonify, request

import attention
import composition
import machine_config as mc
import monitoring_business as mb
import node_probe
import node_traffic as nt
import ops_status
import paths
import plugin_contributions
import probe_cache as pc
import service_manager as sm

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"
# recent_events 是滚动流水，全量带出会把 markdown 冲垮；报告只要最近这些。
_MAX_RECENT_EVENTS = 20
_FORMATS = ("json", "markdown")


# ---- 时间归一 ----------------------------------------------------------


def _to_utc(value):
    """把各模块混用的时间表示归一成 aware UTC datetime；认不出返回 None。

    probe_cache 存 epoch 秒，node_probe / observability 存 `...Z`，
    deploy.sh 存带偏移的 ISO。裸时间串按 UTC 解（SQLite datetime('now') 即 UTC）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt(moment):
    # 必须先换算到 UTC 再贴 Z：带 +08:00 的输入直接 strftime 会得到一个偏 8 小时的假 UTC
    return moment.astimezone(timezone.utc).strftime(_ISO_Z) if moment else None


def _oldest(values):
    """段级 sampled_at = 最旧采样时刻；全都认不出返回 None。"""
    stamps = [moment for moment in (_to_utc(v) for v in values) if moment]
    return min(stamps) if stamps else None


def _safe_error(exc):
    """段内异常摘要：压成单行并截断，不把原始 traceback / 路径整片带出。"""
    text = " ".join("{}: {}".format(type(exc).__name__, exc).split())
    return text[:200] + "…" if len(text) > 200 else text


def _failed_section(exc, **extra):
    section = {"sampled_at": None, "error": _safe_error(exc)}
    section.update(extra)
    return section


# ---- 各段构建 ----------------------------------------------------------


def _health_details(services):
    """服务名 → peek 到的健康缓存副本（无缓存的键不出现）。绝不发起探测。"""
    details = {}
    for svc in services:
        name = (svc or {}).get("name")
        if not name:
            continue
        detail = pc.peek_health_detail(name)
        if detail:
            details[name] = detail
    return details


def _load_services(assets_path):
    return sm.load_assets(assets_path) or []


def _build_attention(assets_path, db_path):
    items = attention.collect_attention(assets_path, db_path)
    # 关注项自己不带时间戳——它是若干缓存的派生物。取它真正读过的那些缓存里
    # 最旧的采样时刻，作为这一段的新鲜度下界。
    # 单独 try：算不出采样时刻只该损失 sampled_at 的精度，不该把已经拿到的关注项一起弄丢。
    stamps = []
    try:
        stamps = [
            detail.get("ts")
            for detail in _health_details(_load_services(assets_path)).values()
        ]
        snapshot = mb.observability_snapshot()
        if isinstance(snapshot, dict):
            stamps.append(snapshot.get("updated_at"))
    except Exception:
        stamps = []

    counts = {}
    for item in items:
        severity = str((item or {}).get("severity") or "info")
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "sampled_at": _fmt(_oldest(stamps)),
        "error": None,
        "counts": counts,
        "items": items,
    }


def _last_probe(machine_id, db_path):
    """probe_history 里最后一条真实探测；没配 probe 的机器不查库。"""
    if not (mc.get_machine(machine_id) or {}).get("probe"):
        return None
    try:
        rows = node_probe.history(machine_id, limit=1, db_path=db_path)
    except Exception:
        return None
    return rows[-1] if rows else None


def _machine_traffic(machine_id, db_path):
    try:
        return nt.usage(machine_id, db_path=db_path)
    except Exception:
        return None


def _build_machines(db_path):
    rows, stamps = [], []
    for machine in mc.machines_for_api():
        probe = _last_probe(machine["id"], db_path) or {}
        if probe.get("ts"):
            stamps.append(probe["ts"])
        rows.append(
            {
                "id": machine["id"],
                "label": machine["label"],
                "role": machine["role"],
                "provider": machine["provider"],
                "region": machine["region"],
                # 没配 probe 或还没落过样本时是 None，不写成 up/down 也不写 unknown：
                # 「没测过」和「测了测不通」是两回事。
                "probe_status": probe.get("status"),
                "probe_ts": _fmt(_to_utc(probe.get("ts"))),
                "latency_ms": probe.get("latency_ms"),
                "traffic": _machine_traffic(machine["id"], db_path),
            }
        )
    return {"sampled_at": _fmt(_oldest(stamps)), "error": None, "items": rows}


def _version_rows(db_path):
    """versions 表快照；表还没建出来时当作没有版本数据。

    PG-04：在线 PG 经 pg_diagnostics 只读事务（多行一致视图）；显式
    db_path / SQLite 引擎按既有只读约定。
    """
    import pg_diagnostics

    try:
        with pg_diagnostics.readonly_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT svc_name, local_version, upstream_version, update_status FROM versions"
            ).fetchall()
    except Exception:
        return {}
    return {row["svc_name"]: dict(row) for row in rows}


def _build_services(assets_path, db_path):
    services = _load_services(assets_path)
    details = _health_details(services)
    versions = _version_rows(db_path)

    rows, stamps = [], []
    for svc in services:
        name = (svc or {}).get("name")
        if not name:
            continue
        detail = details.get(name) or {}
        version = versions.get(name) or {}
        if detail.get("ts"):
            stamps.append(detail["ts"])
        rows.append(
            {
                "name": name,
                "machine": svc.get("machine") or "",
                "type": svc.get("type") or "",
                # 无缓存 → None（「本轮没采到」），不伪造成 Unknown（「没有 checker」）
                "health": detail.get("status"),
                "health_msg": detail.get("msg") or "",
                "health_ts": _fmt(_to_utc(detail.get("ts"))),
                "ssl_days": detail.get("ssl_days"),
                "local_version": version.get("local_version"),
                "upstream_version": version.get("upstream_version"),
                "update_status": version.get("update_status"),
            }
        )
    return {"sampled_at": _fmt(_oldest(stamps)), "error": None, "items": rows}


def _days_left(expires_at, today):
    if not expires_at:
        return None
    try:
        return (datetime.date.fromisoformat(str(expires_at).strip()) - today).days
    except ValueError:
        return None


def _build_observability():
    snapshot = mb.observability_snapshot()
    if not isinstance(snapshot, dict):
        # 没有可用 PG 事件快照不是故障，如实标为不可用。
        return {
            "sampled_at": None,
            "error": None,
            "available": False,
            "ok": None,
            "source_errors": [],
            "active_alerts": [],
            "recent_events": [],
            "events_truncated": False,
        }
    events = list(snapshot.get("recent_events") or [])
    return {
        "sampled_at": _fmt(_to_utc(snapshot.get("updated_at"))),
        "error": None,
        "available": True,
        "ok": snapshot.get("ok"),
        "source_errors": list(snapshot.get("source_errors") or []),
        "active_alerts": list(snapshot.get("active_alerts") or []),
        "recent_events": events[:_MAX_RECENT_EVENTS],
        "events_truncated": len(events) > _MAX_RECENT_EVENTS,
    }


def _build_ops_status(db_path, generated_at):
    status = ops_status.get_ops_status(db_path)
    # env / 挂载文件 / DB 直读，采样时刻就是生成时刻
    return {"sampled_at": _fmt(generated_at), "error": None, "status": status}


# ---- 总评 --------------------------------------------------------------


def _summary(sections):
    """总评只汇总各段已有事实，不重新判定；段挂掉时如实标 degraded。"""
    degraded = sorted(key for key, sec in sections.items() if sec.get("error"))
    counts = sections["attention"].get("counts") or {}
    machines = sections["machines"].get("items") or []
    services = sections["services"].get("items") or []
    renewals = sections["renewals"].get("items") or []

    down = [m["id"] for m in machines if m.get("probe_status") == "down"]
    unhealthy = [s["name"] for s in services if s.get("health") == "Unhealthy"]
    expired = [r.get("name") for r in renewals if (r.get("days_left") or 0) < 0
               and r.get("days_left") is not None]
    due_soon = [r.get("name") for r in renewals
                if r.get("days_left") is not None and 0 <= r["days_left"] <= 7]

    errors = int(counts.get("error") or 0)
    warns = int(counts.get("warn") or 0)
    if errors or down or unhealthy or expired:
        verdict = "需处理"
    elif warns or due_soon or degraded:
        verdict = "留意"
    else:
        verdict = "正常"
    return {
        "verdict": verdict,
        "attention_error": errors,
        "attention_warn": warns,
        "machines_total": len(machines),
        "machines_down": down,
        "services_total": len(services),
        "services_unhealthy": unhealthy,
        "renewals_total": len(renewals),
        "renewals_expired": expired,
        "renewals_due_7d": due_soon,
        "sections_degraded": degraded,
    }


# ---- 报告装配 ----------------------------------------------------------


def _default_assets_path():
    """与 app.py 共用配置根和专用环境覆盖。"""
    return str(composition.config_path("assets.yaml", "CONSOLE_ASSETS_PATH"))


def build_report(assets_path=None, db_path=None):
    """聚合六段只读快照。任何单段异常都被就地收成 error 字段，不向上抛。"""
    assets = assets_path or _default_assets_path()
    database = db_path or paths.DB_PATH
    generated_at = datetime.datetime.now(timezone.utc).replace(microsecond=0)

    builders = (
        ("attention", lambda: _build_attention(assets, database), {"counts": {}, "items": []}),
        ("machines", lambda: _build_machines(database), {"items": []}),
        ("services", lambda: _build_services(assets, database), {"items": []}),
        ("renewals", lambda: plugin_contributions.collect("inspection", db_path=database, generated_at=generated_at).get("renewals", {"items": [], "sampled_at": None, "disabled": True}), {"items": []}),
        ("observability", _build_observability, {
            "available": False, "ok": None, "source_errors": [],
            "active_alerts": [], "recent_events": [], "events_truncated": False,
        }),
        ("ops_status", lambda: _build_ops_status(database, generated_at), {"status": {}}),
    )

    sections = {}
    for key, builder, fallback in builders:
        try:
            sections[key] = builder()
        except Exception as exc:
            sections[key] = _failed_section(exc, **fallback)

    return {
        "generated_at": _fmt(generated_at),
        "summary": _summary(sections),
        "sections": sections,
    }


# ---- markdown 渲染 -----------------------------------------------------


def _cell(value, dash="—"):
    """表格单元格：None/空给破折号，竖线转义，换行压平，避免撑破表格。"""
    if value is None or value == "":
        return dash
    return " ".join(str(value).split()).replace("|", "\\|")


def _sampled(section):
    stamp = section.get("sampled_at")
    text = "采样 {}".format(stamp) if stamp else "采样时间未知"
    if section.get("error"):
        text += "；本段取数失败：{}".format(section["error"])
    return "_{}_".format(text)


def _table(headers, rows):
    if not rows:
        return []
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _render_summary(report, out):
    summary = report["summary"]
    out.append("## 总评")
    out.append("")
    out.append("**{}**".format(summary["verdict"]))
    out.append("")
    out.append(
        "- 关注项：{} error / {} warn".format(
            summary["attention_error"], summary["attention_warn"]
        )
    )
    out.append(
        "- 机器：{} 台，掉线 {}".format(
            summary["machines_total"], _cell("、".join(summary["machines_down"]), "无")
        )
    )
    out.append(
        "- 服务：{} 个，探测失败 {}".format(
            summary["services_total"], _cell("、".join(summary["services_unhealthy"]), "无")
        )
    )
    out.append(
        "- 续费：{} 条，已过期 {}，7 天内到期 {}".format(
            summary["renewals_total"],
            _cell("、".join(str(n) for n in summary["renewals_expired"]), "无"),
            _cell("、".join(str(n) for n in summary["renewals_due_7d"]), "无"),
        )
    )
    if summary["sections_degraded"]:
        out.append(
            "- ⚠️ 取数失败的段：{}（该段内容不可信）".format(
                "、".join(summary["sections_degraded"])
            )
        )
    out.append("")


def _render_attention(report, out):
    section = report["sections"]["attention"]
    out.append("## 关注项")
    out.append("")
    out.append(_sampled(section))
    out.append("")
    items = section.get("items") or []
    if not items:
        out.append("无关注项。")
        out.append("")
    else:
        rows = [
            [
                _cell(item.get("severity")),
                _cell(item.get("kind")),
                _cell(item.get("title")),
                _cell(item.get("detail")),
            ]
            for item in items
        ]
        out.extend(_table(["级别", "类型", "标题", "详情"], rows))
        out.append("")

    obs = report["sections"]["observability"]
    out.append("### 监控事件（Beszel）")
    out.append("")
    out.append(_sampled(obs))
    out.append("")
    if obs.get("error"):
        out.append("")
        return
    if not obs.get("available"):
        out.append("扩展监控快照尚未采集（本次报告不触发采集）。")
        out.append("")
        return
    for err in obs.get("source_errors") or []:
        out.append(
            "- ⚠️ 数据源 {} 失败：{}".format(_cell(err.get("source")), _cell(err.get("message")))
        )
    alerts = obs.get("active_alerts") or []
    out.append("- 活跃告警：{} 条".format(len(alerts)))
    events = obs.get("recent_events") or []
    suffix = "（已截断，仅列最近 {} 条）".format(_MAX_RECENT_EVENTS) if obs.get("events_truncated") else ""
    out.append("- 最近事件：{} 条{}".format(len(events), suffix))
    out.append("")
    rows = [
        [_cell(e.get("machine")), _cell(e.get("name") or e.get("kind")), _cell(e.get("value")),
         _cell(e.get("created") or e.get("ts"))]
        for e in (alerts + events)
    ]
    if rows:
        out.extend(_table(["机器", "事件", "值", "时间"], rows))
        out.append("")


def _render_machines(report, out):
    section = report["sections"]["machines"]
    out.append("## 机器")
    out.append("")
    out.append(_sampled(section))
    out.append("")
    rows = []
    for item in section.get("items") or []:
        traffic = item.get("traffic") or {}
        rows.append(
            [
                _cell(item.get("label") or item.get("id")),
                _cell(item.get("role")),
                _cell(item.get("provider")),
                _cell(item.get("region")),
                _cell(item.get("probe_status"), "未采样"),
                _cell(item.get("probe_ts")),
                _cell(
                    "{}/{} GB（{}%）".format(
                        traffic.get("used_gb"), traffic.get("quota_gb"), traffic.get("pct")
                    )
                    if traffic
                    else None
                ),
            ]
        )
    if rows:
        out.extend(_table(["机器", "角色", "供应商", "地区", "探活", "探活时间", "月流量"], rows))
    else:
        out.append("无机器台账。")
    out.append("")


def _render_services(report, out):
    section = report["sections"]["services"]
    out.append("## 服务")
    out.append("")
    out.append(_sampled(section))
    out.append("")
    out.append("_运行态（docker ps）需要真连 Docker，报告只读不触发，故本段只有健康与版本。_")
    out.append("")
    rows = []
    for item in section.get("items") or []:
        version = item.get("local_version")
        upstream = item.get("upstream_version")
        rows.append(
            [
                _cell(item.get("name")),
                _cell(item.get("machine")),
                _cell(item.get("health"), "未采样"),
                _cell(item.get("health_msg")),
                _cell(item.get("health_ts")),
                _cell(item.get("ssl_days")),
                _cell("{} → {}".format(version, upstream) if version or upstream else None),
                _cell(item.get("update_status")),
            ]
        )
    if rows:
        out.extend(
            _table(
                ["服务", "机器", "健康", "详情", "采样时间", "证书天数", "版本", "版本状态"],
                rows,
            )
        )
    else:
        out.append("无服务台账。")
    out.append("")


def _render_renewals(report, out):
    section = report["sections"]["renewals"]
    out.append("## 续费")
    out.append("")
    out.append(_sampled(section))
    out.append("")
    rows = []
    for item in section.get("items") or []:
        cost = item.get("cost")
        currency = item.get("currency") or ""
        rows.append(
            [
                _cell(item.get("name")),
                _cell(item.get("kind")),
                {"active": "待续费", "not_renewing": "不再续费", "ended": "已结束"}.get(
                    item.get("status", "active"), "未知"
                ),
                _cell(item.get("expires_at"), "不限时"),
                _cell(item.get("days_left")),
                _cell("{} {}".format(cost, currency).strip() if cost is not None else None),
                _cell(item.get("cycle")),
                "是" if item.get("auto_renew") else "否",
            ]
        )
    if rows:
        out.extend(_table(["名称", "类型", "状态", "到期日", "剩余天数", "费用", "周期", "自动续费"], rows))
    else:
        out.append("无续费条目。")
    out.append("")


def _render_ops_status(report, out):
    section = report["sections"]["ops_status"]
    out.append("## 运维状态")
    out.append("")
    out.append(_sampled(section))
    out.append("")
    status = section.get("status") or {}
    out.append("- running commit：{}".format(_cell(status.get("running_commit"))))
    out.append("- 最近备份 tag：{}".format(_cell(status.get("last_backup_tag"))))
    out.append("- 版本采集时间：{}".format(_cell(status.get("versions_last_checked"))))
    out.append("- 信息流抓取时间：{}".format(_cell(status.get("feed_last_fetched"))))
    deploy = status.get("last_deploy") or {}
    if deploy:
        out.append(
            "- 上次部署：{} ｜ 备份 {} ｜ commit {} ｜ 迁移 {}".format(
                _cell(deploy.get("deployed_at")),
                _cell(deploy.get("backup_tag")),
                _cell(deploy.get("commit")),
                _cell(deploy.get("migrations_applied")),
            )
        )
    else:
        out.append("- 上次部署：— _(backup/last_deploy.json 不可读；未挂载或尚未部署过)_")
    out.append("")


def render_markdown(report):
    """固定骨架：总评 → 关注项（含监控事件）→ 机器 → 服务 → 续费 → 运维状态。"""
    generated = report.get("generated_at") or "未知时间"
    out = [
        "# 巡检报告 {}".format(generated),
        "",
        "> 只读快照，生成过程不触发任何探测。各段 `采样` 标的是**该段最旧的采样时刻**"
        "（最坏新鲜度），不是生成时间；时间一律 UTC。",
        "",
    ]
    _render_summary(report, out)
    _render_attention(report, out)
    _render_machines(report, out)
    _render_services(report, out)
    _render_renewals(report, out)
    _render_ops_status(report, out)
    return "\n".join(out).rstrip() + "\n"


# ---- 路由 --------------------------------------------------------------


def register_routes(app, resolve_paths=None):
    """挂到既有 app 上；app.py 只负责 import 与调用这一处。

    `resolve_paths` 是**每请求求值**的 `() -> (assets_path, db_path)`：app.py 的
    ASSETS_PATH / DB_PATH 是模块级全局，注册时捕获值会让测试的 monkeypatch 失效，
    也会在两者被重算时留下陈旧引用。
    """

    @app.route("/api/inspection/report", methods=["GET"])
    def get_inspection_report():
        """巡检报告只读聚合。`?format=markdown` 返回 text/markdown，默认 json；format 非法 400。"""
        fmt = (request.args.get("format") or "json").strip().lower()
        if fmt not in _FORMATS:
            return jsonify({"error": "format 只支持 json 或 markdown"}), 400
        assets_path, db_path = resolve_paths() if resolve_paths else (None, None)
        report = build_report(assets_path, db_path)
        if fmt == "markdown":
            # 用 content_type 而不是 mimetype：给 mimetype 传带 charset 的串，
            # Flask 会对 text/* 再补一次，实测头变成 charset=utf-8; charset=utf-8
            return Response(
                render_markdown(report), content_type="text/markdown; charset=utf-8"
            )
        return jsonify(report)

    return app
