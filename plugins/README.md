# 插件目录与信息索引

本目录放 console-stack 的功能插件，NAS 对应 `/opt/console-stack/plugins/`。主框架保留总控台、设备与框架设置；功能代码按 `plugins/<id>/` 组织。

本文件是**已安装插件的介绍与目录索引**；程序实际读取每个插件的 `manifest.json`，不读取本表。启用和显隐状态由 NAS 的 `settings.yaml` 保存，在「设置 → 插件」管理，当前状态以 `GET /api/plugins` 为准。

## 插件清单

| ID / 目录 | 名称 | 版本 | 简介 | 页面入口 | API 前缀 |
| --- | --- | --- | --- | --- | --- |
| [feed](feed/) | 信息聚合 | 1.1.0 | GitHub 动态与信息流，保留书签接口。 | `/feed` | `/api/feed`、`/api/bookmarks` |
| [ledger](ledger/) | 个人记账 | 1.1.0 | 账单导入、分类、对冲与收支统计。 | `/ledger` | `/api/ledger` |
| [renewals](renewals/) | 到期提醒 | 1.2.0 | 续费台账、到期提醒与成本查询。 | `/renewals` | `/api/renewals` |
| [audit](audit/) | 审计与事件 | 1.2.0 | 操作记录与监控事件查询；基础操作留痕持续保留。 | `/audit` | `/api/oplog`、`/api/events` |
| [panels](panels/) | 面板 | 1.2.0 | 按设备组织的外部面板，支持内嵌或新标签打开、星标与保活会话。 | `/panel/:id` | `/api/panels`、`/panel-proxy` |

| [personal_workbench](personal_workbench/) | 个人工作台 | 1.1.0 | 博客审阅。 | `/workbench` | `/api/personal-workbench` |

| [backup_evidence](backup_evidence/) | 备份证据 | 1.0.0 | 备份完整性与恢复证据：只读扫描备份目录、五级成熟度、事故台账与恢复回执。 | `/backup-evidence` | `/api/backup-evidence` |

以上五个原有插件使用 `api_version: 1`，并以 `client_mode: "shared"` 保留迁入时的既有 client 行为。新插件默认使用 `authority`；详见[运行时契约](../.agents/contracts/plugin-runtime.md)。

面板插件另提供 `Sidebar`、「设置 → 面板」和 `loadNavigation` 导航贡献（命令检索、设备/服务关联入口），并使用常驻页面与自有顶栏。其它四个插件使用普通页面宿主。审计插件负责查询展示，框架仍负责操作留痕与设备事件采集；停用审计插件不停止基础记录。

## 目录内容

```text
plugins/
  README.md                  本索引
  __init__.py                插件包入口
  <id>/
    manifest.json            程序读取的插件信息、页面和 API 声明
    __init__.py              Python 包入口
    backend/
      __init__.py            register(app, context)
      ...                    后端业务与 test_*.py
    frontend/
      index.tsx              默认页面；按清单声明导出 Settings / Sidebar
      ...                    组件、工具函数与 *.test.ts(x)
```

插件目录和清单不能使用符号链接。运行数据、备份、回收物与凭据按[宿主目录契约](../.agents/contracts/host-storage-layout.md)存放；不放入插件源码目录。

## 维护要求

- 新增、改名、升级或移出插件时，同批更新本表的目录、名称、版本、简介、页面入口和 API 前缀；这些字段须与对应 `manifest.json` 一致。
- 调整启停或显隐不改本表，也不在此维护某个环境的运行状态。使用行为见[功能插件说明](../docs/plugins.md)。
- 新插件按[根 README 的插件开发指南](../README.md#插件开发指南)实现；清单字段、生命周期、数据与兼容约束以[运行时契约](../.agents/contracts/plugin-runtime.md)为准。
- 后端使用只读 `PluginContext` 和有限贡献槽；初始化失败只隔离该插件，清单、导入及注册错误仍阻止启动。前端页面、侧栏、设置和 toolbar 按插件分别隔离错误。具体失败语义见[运行时契约](../.agents/contracts/plugin-runtime.md)。
- `manifest.json` 的 `owns.tables/settings/files/tasks` 是数据归属元数据，需四类都声明；所有权冲突会被拒绝。它不迁移数据、不在卸载时清理，也不改变 secrets 边界。
- 新增骨架、插件树检查和隔离移出验证分别使用 `scripts/new_plugin.py`、`scripts/check_plugins.py`、`scripts/check_plugin_removal.py`；命令行与验收边界见[开发循环](../.agents/operations/dev-loop.md)。
- 新增能力的依赖、数据归属、配置及使用说明应随实现补齐；插件变更按[开发与发布流程](../.agents/operations/dev-loop.md)验证、提交和部署。
