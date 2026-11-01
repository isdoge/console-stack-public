# -*- coding: utf-8 -*-
"""组合层附件备份适配（B7-2）。

两类断言同样重要：

- **公共组合（无附件）不产出假 Pay 包**——否则「有没有附件」不可判别；
- **私人组合（ledger 声明 owns.files）必须走真实 Pay 适配器**，且原件根
  与备份根重叠、相对路径、未登记适配器都必须 fail closed。
"""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console/backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_attachments as att  # noqa: E402
import pg_contracts  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PLUGINS = REPO / "plugins"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """默认无显式适配器声明，让组合清单自己说话。"""
    monkeypatch.delenv(att.ATTACHMENT_ENV, raising=False)
    monkeypatch.delenv("CONSOLE_PAY_DATA_DIR", raising=False)
    monkeypatch.delenv("CONSOLE_COMPOSITION_FILE", raising=False)
    monkeypatch.delenv("OPS_BACKUP_DIR", raising=False)


def _plugin_root(tmp_path, entries):
    """合成插件根：`entries` = {plugin_id: owns.files}。"""
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "contract.json").write_text(json.dumps({"api_version": 1}), encoding="utf-8")
    (root / "composition.json").write_text(
        json.dumps({"plugins": sorted(entries)}), encoding="utf-8")
    for pid, files in entries.items():
        (root / pid).mkdir()
        (root / pid / "manifest.json").write_text(json.dumps({
            "id": pid, "api_version": 1, "owns": {"tables": [], "files": files},
        }), encoding="utf-8")
    return root


# --- 组合判定 ---------------------------------------------------------------


def test_real_private_composition_detects_pay():
    """真实 `plugins/`（ledger 声明 data/pay）→ pay 适配器。"""
    assert att.composition_attachment_name(PLUGINS) == "pay"


def test_composition_without_attachments_is_empty(tmp_path):
    root = _plugin_root(tmp_path, {"audit": [], "feed": []})
    assert att.composition_attachment_name(root) is None


def test_explicit_empty_env_overrides_composition(monkeypatch):
    """`CONSOLE_ATTACHMENT_ADAPTER=` 明确表示无附件，压掉组合声明。"""
    monkeypatch.setenv(att.ATTACHMENT_ENV, "")
    assert att._adapter_name() is None
    assert att.describe() == {"adapter": None, "suffix": None, "files": False,
                              "format": att._FORMAT}


def test_unknown_explicit_adapter_is_refused(monkeypatch):
    monkeypatch.setenv(att.ATTACHMENT_ENV, "dropbox")
    with pytest.raises(att.AttachmentError, match="不是已登记的附件适配器"):
        att._adapter_name()


def test_undeclared_file_owner_fails_closed(tmp_path):
    """声明了 owns.files 却没登记归档归属：不能静默当成「没有附件」。"""
    root = _plugin_root(tmp_path, {"gallery": ["data/photos"]})
    with pytest.raises(att.AttachmentError, match="没有附件归档归属"):
        att.composition_attachment_name(root)


def test_non_archive_file_owner_is_explicitly_allowed(tmp_path):
    """panels/personal_workbench 的文件不属于 PG 批次原件，显式归类后才放行。"""
    root = _plugin_root(tmp_path, {"panels": ["console/panels.yaml"],
                                   "personal_workbench": ["data/workbench"]})
    assert att.composition_attachment_name(root) is None


def test_multiple_attachment_kinds_require_explicit_choice(tmp_path, monkeypatch):
    """两个已登记适配器的插件同时存在时必须显式二选一，不按顺序猜。"""
    monkeypatch.setitem(att.ADAPTER_FOR_PLUGIN, "gallery", "pay")
    root = _plugin_root(tmp_path, {"ledger": ["data/pay"], "gallery": ["data/photos"]})
    monkeypatch.setitem(att.ADAPTER_FOR_PLUGIN, "ledger", "pay")
    # 两个都映射到 pay 时不算多来源
    assert att.composition_attachment_name(root) == "pay"
    monkeypatch.setitem(att.ADAPTER_FOR_PLUGIN, "gallery", "photos")
    with pytest.raises(att.AttachmentError, match="多种附件来源"):
        att.composition_attachment_name(root)
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    assert att._adapter_name() == "pay"


def test_real_non_archive_owners_are_registered():
    """真实组合里声明文件的插件都必须有明确归属，否则真实部署会启动即失败。"""
    import composition
    declared = composition.plugin_sources(PLUGINS)
    for pid in declared:
        data = json.loads((PLUGINS / pid / "manifest.json").read_text(encoding="utf-8"))
        files = (data.get("owns") or {}).get("files") or []
        if not files:
            continue
        assert pid in att.ADAPTER_FOR_PLUGIN or pid in att.NON_ARCHIVE_FILE_OWNERS, pid


def test_adapter_spec_matches_real_pay_module():
    """清单里的 module/writer 必须真的存在：不复述、不猜函数名。"""
    _module, writer = att.adapter("pay")
    import backup_pay
    assert writer is backup_pay.write_pay_snapshot
    import inspect
    parameters = inspect.signature(writer).parameters
    assert att.ATTACHMENT_ADAPTERS["pay"]["root_kwarg"] in parameters


# --- 原件根约束 -------------------------------------------------------------


def test_relative_attach_root_is_refused(monkeypatch):
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", "data/pay")
    with pytest.raises(att.AttachmentError, match="必须是绝对路径"):
        att.attach_root("pay")


def test_attach_root_inside_backup_root_is_refused(tmp_path, monkeypatch):
    """原件在组合声明的 backup 根内：组合根附加防线拒绝（守卫第二判据）。"""
    backup = tmp_path / "backup"
    attach = backup / "pay"
    attach.mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(attach))
    monkeypatch.setenv("OPS_BACKUP_DIR", str(backup))
    dest = output / ("console.dump.t" + att.suffix("pay"))
    with pytest.raises(att.AttachmentError, match="组合备份根"):
        att.write_attachments(dest, root=tmp_path, query=lambda sql: [])
    assert not dest.exists()


def test_attach_root_sibling_of_backup_root_is_allowed(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    attach = tmp_path / "pay"
    attach.mkdir()
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(attach))
    monkeypatch.setenv("OPS_BACKUP_DIR", str(backup))
    # 空批次 + 空目录 → 合法空 Pay 归档（组合声明了附件就必须有归档）
    dest = output / ("console.dump.t" + att.suffix("pay"))
    member = att.write_attachments(dest, root=tmp_path, query=lambda sql: [])
    assert member["path"] == dest.name
    assert dest.is_file()


# --- 无附件组合 -------------------------------------------------------------


def test_no_attachment_composition_writes_nothing(tmp_path, monkeypatch):
    root = _plugin_root(tmp_path, {"audit": []})
    monkeypatch.delenv(att.ATTACHMENT_ENV, raising=False)
    monkeypatch.setattr(att, "_default_plugin_root", lambda: root)
    dest = tmp_path / "console.dump.t"
    assert att.write_attachments(dest, root=tmp_path, query=lambda sql: []) is None
    assert not dest.exists()
    assert not list(tmp_path.glob("*.pay.tar"))
    assert att.describe()["adapter"] is None


def test_no_attachment_composition_never_imports_pay(tmp_path, monkeypatch):
    """公共组合不能因为「反正有模块」就去导入私人适配器。"""
    root = _plugin_root(tmp_path, {"audit": []})
    monkeypatch.setattr(att, "_default_plugin_root", lambda: root)
    monkeypatch.setattr(att, "importlib", _ExplodingImport())
    assert att.write_attachments(tmp_path / "console.dump.t", root=tmp_path,
                                 query=lambda sql: []) is None


class _ExplodingImport:
    def import_module(self, name):
        raise AssertionError(f"无附件组合不应导入 {name}")


# --- 真实 Pay 端到端 --------------------------------------------------------


def _pay_fixture(tmp_path, monkeypatch):
    pay = tmp_path / "pay"
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(pay))
    raw = b"category,value\nfood,12\n"
    sha = hashlib.sha256(raw).hexdigest()
    reference = {"version": "reference-manifest-1", "generated_at": "old-time",
                 "files": [{"path": "rules.csv", "sha256": sha, "bytes": len(raw),
                            "format_version": "csv"}]}
    digest = pg_contracts.reference_manifest_digest(reference)
    history = pay / "reference-history"
    (history / "files").mkdir(parents=True)
    (history / "manifests").mkdir()
    (history / "files" / sha).write_bytes(raw)
    (history / "manifests" / f"{digest}.json").write_text(json.dumps(reference))
    batch_id = "22222222-2222-4222-8222-222222222222"
    receipt = pg_contracts.build_batch_receipt(
        batch_id, "import", "test", "version-test",
        [{"name": "bill.csv", "sha256": sha, "bytes": len(raw)}], digest)
    archive = pay / "archive" / batch_id
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text(json.dumps(receipt))
    original = archive / f"01-{sha[:16]}.csv"
    original.write_bytes(raw)
    row = {key: receipt[key] for key in (
        "batch_id", "operation", "client", "code_version", "request_sha256",
        "reference_manifest_sha256")}
    row["artifacts_json"] = json.dumps(receipt["artifacts"])
    return pay, original, (lambda sql: [json.dumps(row)]), row


def test_private_composition_writes_restorable_pay_archive(tmp_path, monkeypatch):
    import backup_pay

    pay, original, query, _row = _pay_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    output = tmp_path / "out"
    output.mkdir()
    dest = output / ("console.dump.20261030-120000" + att.suffix("pay"))
    member = att.write_attachments(dest, root=tmp_path, query=query)
    assert member["path"] == dest.name
    assert member["bytes"] == dest.stat().st_size
    assert member["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()

    # 发布后原件被改动也不影响已发布归档
    original.write_bytes(b"changed after backup")
    manifest = {"members": [member]}
    restored = tmp_path / "restored"
    result = backup_pay.restore_pay_snapshot(
        output / "console.dump.20261030-120000", manifest, dest=restored, query=query)
    assert result["verified"] and result["batches"] == 1 and result["references"] == 1
    assert (restored / original.relative_to(pay)).read_bytes() == b"category,value\nfood,12\n"


def test_adapter_result_must_be_ordinary_file(tmp_path, monkeypatch):
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    output = tmp_path / "out"
    output.mkdir()

    class _Lying:
        @staticmethod
        def write_pay_snapshot(dest, **_kwargs):
            return {"path": Path(dest).name, "sha256": "0" * 64, "bytes": 0}

    monkeypatch.setitem(att.ATTACHMENT_ADAPTERS, "pay",
                        dict(att.ATTACHMENT_ADAPTERS["pay"], module="_lying_adapter"))
    monkeypatch.setitem(sys.modules, "_lying_adapter", _Lying)
    with pytest.raises(att.AttachmentError, match="没有在 .* 生成普通文件"):
        att.write_attachments(output / "x.pay.tar", root=tmp_path, query=lambda sql: [])


def test_adapter_writing_wrong_name_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    output = tmp_path / "out"
    output.mkdir()

    class _Renamer:
        @staticmethod
        def write_pay_snapshot(dest, **_kwargs):
            other = Path(dest).with_name("somewhere-else.tar")
            other.write_bytes(b"x")
            return {"path": other.name, "sha256": "0" * 64, "bytes": 1}

    monkeypatch.setitem(att.ATTACHMENT_ADAPTERS, "pay",
                        dict(att.ATTACHMENT_ADAPTERS["pay"], module="_renaming_adapter"))
    monkeypatch.setitem(sys.modules, "_renaming_adapter", _Renamer)
    with pytest.raises(att.AttachmentError, match="写的文件名与请求不符"):
        att.write_attachments(output / "x.pay.tar", root=tmp_path, query=lambda sql: [])


# --- R06：实际输出目录守卫 ---------------------------------------------------


def test_originals_inside_actual_dest_dir_are_refused(tmp_path, monkeypatch):
    """原件在**实际输出目录**下、组合根指别处：守卫必须仍拒绝（R06 反例）。

    以前守卫只比对组合声明的 backup 根（OPS_BACKUP_DIR），配置根与 timed
    实际输出根不一致时，原件错误落在输出树里也会被打包。
    """
    real_backup = tmp_path / "actual-backup"
    originals = real_backup / "pay-originals"
    originals.mkdir(parents=True)
    configured = tmp_path / "configured-backup"
    configured.mkdir()
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(originals))
    monkeypatch.setenv("OPS_BACKUP_DIR", str(configured))
    dest = real_backup / ("console.dump.t" + att.suffix("pay"))
    with pytest.raises(att.AttachmentError, match="实际输出目录"):
        att.write_attachments(dest, root=tmp_path, query=lambda sql: [])
    assert not dest.exists()


def test_unconfigured_pay_root_defaults_inside_repo_are_checked(tmp_path, monkeypatch):
    """CONSOLE_PAY_DATA_DIR 未设置：适配器默认根 `<root>/console/backend/data/pay`。

    输出目录正是默认根所在的 data 目录（部署里 timed 目录与数据目录相邻）：
    守卫按适配器**最终生效**根判断——不因 `attach_root()` 返回 None 而跳过。
    """
    root = tmp_path / "repo"
    output = root / "console" / "backend" / "data"
    output.mkdir(parents=True)
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.delenv("CONSOLE_PAY_DATA_DIR", raising=False)
    dest = output / ("console.dump.t" + att.suffix("pay"))
    with pytest.raises(att.AttachmentError, match="实际输出目录"):
        att.write_attachments(dest, root=root, query=lambda sql: [])


def test_unresolvable_paths_are_refused_not_skipped(tmp_path, monkeypatch):
    """路径 resolve 失败：写归档前有界拒绝，不再静默跳过守卫。"""
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(tmp_path / "pay"))
    with patch.object(att, "_resolve_strict",
                      side_effect=att.AttachmentError("synthetic unresolvable")):
        with pytest.raises(att.AttachmentError, match="unresolvable"):
            att.write_attachments(backup / "x.pay.tar", root=tmp_path,
                                  query=lambda sql: [])
    assert not (backup / "x.pay.tar").exists()


def test_composition_root_failure_does_not_bypass_guard(tmp_path, monkeypatch):
    """组合根解析抛错：_backup_root 返回 None，守卫改按实际输出目录继续（R06）。"""
    import composition
    real_backup = tmp_path / "timed"
    originals = real_backup / "pay"
    originals.mkdir(parents=True)
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(originals))
    dest = real_backup / ("console.dump.t" + att.suffix("pay"))
    with patch.object(composition, "root_path",
                      side_effect=composition.CompositionError("synthetic")):
        with pytest.raises(att.AttachmentError, match="实际输出目录"):
            att.write_attachments(dest, root=tmp_path, query=lambda sql: [])
    assert not dest.exists()


def test_output_dir_equals_originals_is_refused(tmp_path, monkeypatch):
    originals = tmp_path / "pay"
    originals.mkdir()
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(originals))
    with pytest.raises(att.AttachmentError, match="相互嵌套或相同"):
        att.write_attachments(originals / "x.pay.tar", root=tmp_path, query=lambda sql: [])


@pytest.mark.parametrize("which", ["output", "explicit-original", "default-original"])
def test_real_path_resolution_failure_refuses_before_writing(tmp_path, monkeypatch, which):
    """真实符号链接环触发Path.resolve失败；守卫必须在打包之前拒绝。"""
    output = tmp_path / "out"
    output.mkdir()
    original = tmp_path / "pay"
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    if which == "output":
        output = tmp_path / "loop"
        output.symlink_to(output.name)
    elif which == "explicit-original":
        original.symlink_to(original.name)
    else:
        original = tmp_path / "console/backend/data/pay"
        original.parent.mkdir(parents=True)
        original.symlink_to(original.name)
    if which != "default-original":
        monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(original))
    with pytest.raises(att.AttachmentError, match="无法解析路径"):
        att.write_attachments(output / "x.pay.tar", root=tmp_path, query=lambda sql: [])


def test_output_dir_inside_originals_is_refused_too(tmp_path, monkeypatch):
    """双向嵌套：输出目录在原件根**之下**同样拒绝。"""
    originals = tmp_path / "pay"
    out = originals / "backups"
    out.mkdir(parents=True)
    monkeypatch.setenv(att.ATTACHMENT_ENV, "pay")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(originals))
    with pytest.raises(att.AttachmentError, match="相互嵌套或相同"):
        att.write_attachments(out / ("x" + att.suffix("pay")), root=tmp_path,
                              query=lambda sql: [])
