"""可信插件可用的公开 SDK 面（B7-1，api_version = 1）。

插件不该直接依赖框架私有模块与实现细节。这里只导出**当前实际被插件使用**的
跨插件公共能力，命名即契约：

- `storage`：存储分派（`read_tx` / `write_tx` / `ph` / `like_op` / `executemany`
  / `is_missing_table`）。导出的是 `storage_dispatch` 模块对象本身，因此
  `monkeypatch.setattr(plugin_sdk.storage, ...)` 与直接 patch `storage_dispatch`
  等价，不额外造一层包装。
- `locks`：PG advisory 锁键与取锁入口（`pg_locks`）。
- `runtime_mode`：存储引擎 / 实例角色 / 维护只读判定。
- `money`：十进制金额与展示编码。
- `validation`：外部输入校验。
- `yaml`：YAML 读写（第三方库，统一从这里取，插件不必各自声明依赖）。

刻意**不**导出：`app`、任何路由宿主、`paths` 的具体布局、测试辅助
（`pg_test_support`）、框架私有接线。新增导出必须先有真实插件使用，并视为
`api_version` 兼容面的一部分；破坏性变化必须同批迁移已安装插件。
"""
import yaml

import money
import pg_locks as locks
import runtime_mode
import storage_dispatch as storage
import validation

__all__ = ['locks', 'money', 'runtime_mode', 'storage', 'validation', 'yaml']