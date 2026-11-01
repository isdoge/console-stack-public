#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§8.3 首次割接的准备阶段：外置控制器在动状态机**之前**必须完成的现场准备。

V4-02（主控 §11.5.2）缺的就是这一段：修复前 `run_cutover()` 一上来就要求权威 root
的 HEAD 已经等于 `--target-commit`，等于把「候选 release 加载 / 旧 root 的 YAML 收编 /
推进到最终 SHA / 镜像预构建 / 首次 PG 服务创建」全部推给人工——而 §11.5 明令演练
不允许用「人工先把 root 改成目标 SHA / 预启动预建目标」代替这些步骤。

本模块按 §8.3 的顺序执行，并把每一步的实际产物写进回执
（`console-cutover-prepare-1`，默认 `backup/cutover/prepare.json`）：

1. `requested_commit` 必须是**生产 root 的 git 里已存在**的完整提交（§8.3 步骤 1：
   NAS 先 fetch 候选 SHA；本模块不联网、不 fetch）。
2. **release 校验**：release 目录（`--release-dir`，默认本模块所在树的根 = 候选
   release）里每个受控文件的字节必须等于 `requested_commit:` 下的 blob——「已审查
   候选版本」不是口头声明，是逐文件摘要。
3. **YAML 收编**：线上 UI 手改的四个 YAML 先整理成一个提交（与 `deploy.sh` 同语义），
   避免随后的 Git 更新覆盖线上配置。
4. **推进 root 到最终 SHA**：能快进就快进；收编产生本地提交时用 rebase 把本地提交
   重放到 `requested_commit` 之上（= `deploy.sh` 的 `git pull --rebase` 语义）；分叉且
   无法自动收敛时拒绝，交人工（§8.3：冲突未解决不进入停写阶段）。
5. **最终 SHA 复核**：移动后 root 上的受控文件必须与 release 逐字节一致——「记录的
   版本」与「执行的版本」在进入状态机之前就已经锁定。
6. **运行 env 阶段配置**：把 `CONSOLE_STORAGE_ENGINE` / `CONSOLE_INSTANCE_ROLE` 写进
   生产 env 文件（记下旧值以便回滚）。`CONSOLE_MAINTENANCE_MODE=read_only` 由
   **冻结步骤**（`cutover_writers.py freeze --deploy-env`）写，本阶段不碰。
7. **镜像预构建**：默认 `docker compose build console`——只构建、不启动新 app。
8. **PG 服务创建/启动 + 就绪**：默认 `docker compose up -d --wait console-postgres`，
   随后用真实连接确认就绪（不是只看容器 running）。

退出码：0 成功；1 校验/执行失败；2 用法或配置错误（缺 root/env 文件/未声明角色）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_runtime as runtime  # noqa: E402
from host_paths import backup_path  # noqa: E402

RECEIPT_FORMAT = "console-cutover-prepare-1"
DEFAULT_REPORT_RELATIVE = "backup/cutover/prepare.json"
YAML_FILES = ("console/panels.yaml", "console/assets.yaml",
              "console/machines.yaml", "console/settings.yaml")
# release 校验的受控文件：割接执行身份 + 部署/编排面 + 应用入口。
RELEASE_FILES = tuple(dict.fromkeys((
    *runtime.CUTOVER_EXECUTION_FILES,
    "scripts/cutover_prepare.py",
    "scripts/cutover_backup_restore.py",
    "scripts/pg_migration_lib.py",
    "scripts/pg_schema.py",
    "scripts/deploy.sh",
    "docker-compose.yml",
    "docker-compose.ci.yml",
    "console/backend/db.py",
    "console/backend/db_sqlite.py",
    "console/backend/app.py",
    "console/backend/paths.py",
    "console/backend/money.py",
    "console/backend/runtime_mode.py",
    "console/backend/pg_locks.py",
    "console/backend/pg_contracts.py",
    "console/backend/ledger_batch_contracts.py",
    "console/backend/pg_migration_contracts.py",
)))
ENV_KEYS = ("CONSOLE_STORAGE_ENGINE", "CONSOLE_INSTANCE_ROLE")
# 与 console/backend/runtime_mode.py 的 INSTANCE_ROLES 必须一致（有测试钉住）。
INSTANCE_ROLES = ("authority", "client", "test")


class PrepareError(RuntimeError):
    pass


class UsageError(PrepareError):
    pass


def _utc_now():
    return runtime._utc_now()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return runtime.file_digest(path)


def _git(root: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                            text=True, timeout=120)
    if check and result.returncode:
        raise PrepareError(
            f"git {args[0] if args else ''} 失败 (exit {result.returncode})："
            f"{(result.stderr or result.stdout or '').strip()[-300:]}")
    return result


def _blob_bytes(root: Path, commit: str, relative: str) -> bytes:
    result = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=str(root),
                            capture_output=True, timeout=120)
    if result.returncode:
        raise PrepareError(f"目标提交 {commit[:12]}… 里没有 {relative}")
    return result.stdout


def _run_steps(commands, root: Path, env=None, timeout=1800):
    result = subprocess.run(list(commands), cwd=str(root), capture_output=True,
                            text=True, timeout=timeout, env=env)
    return {"cmd": " ".join(str(part) for part in commands), "exit": result.returncode,
            "stdout_tail": (result.stdout or "")[-500:],
            "stderr_tail": (result.stderr or "")[-500:]}


def verify_release(root: Path, release_dir: Path, commit: str) -> dict:
    """release 目录里的受控文件必须等于 `commit:` 下的 blob（逐字节）。"""
    checked, missing, mismatched = [], [], []
    for relative in RELEASE_FILES:
        path = release_dir / relative
        if not path.is_file():
            missing.append(relative)
            continue
        blob = _blob_bytes(root, commit, relative)
        digest = _sha256_file(path)
        checked.append(relative)
        if digest != _sha256_bytes(blob):
            mismatched.append({"file": relative, "release": digest[:12],
                               "commit": _sha256_bytes(blob)[:12]})
    if missing or mismatched:
        raise PrepareError(
            f"release 校验失败：缺文件 {missing}；摘要不符 {mismatched}"
            "（§8.3 步骤 1：release 必须是该 SHA 的 git archive 展开）")
    return {"dir": str(release_dir), "commit": commit, "files_checked": len(checked),
            "digest": _release_digest(release_dir)}


def _release_digest(release_dir: Path) -> str:
    payload = {relative: _sha256_file(release_dir / relative) for relative in RELEASE_FILES}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verify_root_matches_release(root: Path, release_dir: Path) -> dict:
    """移动后复核：root 上的受控文件与 release 逐字节一致（执行的就是审查过的那份）。"""
    mismatched = []
    for relative in RELEASE_FILES:
        left, right = root / relative, release_dir / relative
        if not left.is_file() or not right.is_file() or _sha256_file(left) != _sha256_file(right):
            mismatched.append(relative)
    if mismatched:
        raise PrepareError(f"最终 SHA 复核失败：root 与 release 不一致 {mismatched}")
    return {"files_checked": len(RELEASE_FILES), "digest": _release_digest(release_dir)}


def adopt_yaml(root: Path) -> dict:
    """收编线上手改的四个 YAML（与 deploy.sh 第 200–214 行同语义）。"""
    dirty = _git(root, "status", "--porcelain", "--", *YAML_FILES).stdout.strip()
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    if not dirty:
        return {"adopted": False, "base": base, "reason": "无本地 YAML 改动"}
    _git(root, "add", *YAML_FILES)
    commit = subprocess.run(
        ["git", "commit", "-m",
         f"chore(nas): 收编线上 YAML 台账手改 {runtime.datetime.datetime.now():%Y%m%d-%H%M%S}"],
        cwd=str(root), capture_output=True, text=True, timeout=120)
    if commit.returncode:
        raise PrepareError(
            "YAML 收编提交失败（检查 git user.name/user.email）："
            f"{(commit.stderr or commit.stdout or '').strip()[-300:]}")
    return {"adopted": True, "base": base,
            "commit": _git(root, "rev-parse", "HEAD").stdout.strip(),
            "entries": dirty.splitlines()[:20]}


def move_root_to_target(root: Path, requested_commit: str, adoption: dict) -> dict:
    """把 root 推进到 requested_commit；收编提交已存在时 rebase 到目标之上。"""
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if head == requested_commit:
        return {"mode": "already-at-target", "final_commit": head}
    ancestor = _git(root, "merge-base", "--is-ancestor", head, requested_commit,
                    check=False).returncode == 0
    if ancestor:
        _git(root, "merge", "--ff-only", requested_commit)
        return {"mode": "fast-forward", "final_commit": _git(root, "rev-parse", "HEAD").stdout.strip()}
    if _git(root, "merge-base", "--is-ancestor", requested_commit, head,
            check=False).returncode == 0:
        # HEAD 已经包含候选提交：上一次准备已把收编提交叠在它上面（最终 SHA ≠ 候选
        # SHA 是 §8.3 明说的正常形态）。受控文件随后由 `verify_root_matches_release`
        # 逐字节复核——幂等重跑不会因为「HEAD 不等于 requested」而误判成分叉。
        return {"mode": "already-ahead-of-target", "final_commit": head}
    if adoption.get("adopted"):
        # 收编提交产生后 HEAD 与候选 SHA 已经分叉（各自由收编前的基点出发）。只要
        # **收编前的基点**是候选 SHA 的祖先，就能把本地提交 rebase 到候选之上
        # （= deploy.sh 的 `git pull --rebase` 语义），最终 SHA 随之变化并重新记录。
        base = adoption.get("base")
        if base and _git(root, "merge-base", "--is-ancestor", base, requested_commit,
                         check=False).returncode == 0:
            rebase = subprocess.run(["git", "rebase", requested_commit], cwd=str(root),
                                    capture_output=True, text=True, timeout=300)
            if rebase.returncode:
                subprocess.run(["git", "rebase", "--abort"], cwd=str(root),
                               capture_output=True, text=True)
                raise PrepareError(
                    "YAML 收编提交 rebase 到候选 SHA 失败（冲突未解决不进入停写阶段，"
                    f"§8.3）：{(rebase.stderr or rebase.stdout or '').strip()[-300:]}")
            return {"mode": "rebase-adoption", "base": base,
                    "final_commit": _git(root, "rev-parse", "HEAD").stdout.strip()}
    raise PrepareError(
        f"root 与候选 SHA 已分叉且无法自动收敛（HEAD={head[:12]}… target={requested_commit[:12]}…）："
        "按 §8.3 人工解决后再割接，不自动 reset/覆盖")


def configure_env_file(path: Path, *, instance_role: str) -> dict:
    """把引擎与实例角色写进生产 env（记旧值）；维护模式由冻结步骤写。"""
    if not path.is_file():
        raise UsageError(f"env 文件不存在：{path}（--env-file / CONSOLE_CUTOVER_ENV_FILE）")
    import cutover_writers as writers

    before = {key: writers._read_env_value(path, key) for key in ENV_KEYS}
    writers._set_env_value(path, "CONSOLE_STORAGE_ENGINE", "postgres")
    writers._set_env_value(path, "CONSOLE_INSTANCE_ROLE", instance_role)
    after = {key: writers._read_env_value(path, key) for key in ENV_KEYS}
    if after["CONSOLE_STORAGE_ENGINE"] != "postgres" or after["CONSOLE_INSTANCE_ROLE"] != instance_role:
        raise PrepareError(f"env 写入后复读不一致：{after}")
    return {"file": str(path), "before": before, "after": after,
            "maintenance_mode_written_by": "cutover_writers.freeze(--deploy-env)"}


def prepare(root: Path, *, requested_commit: str, release_dir: Path | None = None,
            report_path: Path | None = None, env_file: Path | None = None,
            instance_role: str | None = None, dry_run: bool = False) -> dict:
    root = Path(root).resolve()
    release_dir = (Path(release_dir).resolve() if release_dir
                   else Path(__file__).resolve().parents[1])
    report_path = Path(report_path) if report_path else backup_path(root, DEFAULT_REPORT_RELATIVE)
    env_file = env_file or (os.environ.get("CONSOLE_CUTOVER_ENV_FILE") or None)
    role = instance_role or (os.environ.get("CONSOLE_CUTOVER_INSTANCE_ROLE") or "").strip()
    receipt = {"format": RECEIPT_FORMAT, "root": str(root),
               "release_dir": str(release_dir), "requested_commit": requested_commit,
               "started_at": _utc_now()}
    if not role:
        raise UsageError(
            "未声明 CONSOLE_CUTOVER_INSTANCE_ROLE / --instance-role（authority|client|test）："
            "阶段配置必须显式，不猜（§11.5 V4-02）")
    if role not in INSTANCE_ROLES:
        # 拼错的角色（如 `prod`）此前会一路写进 env、到应用启动才炸；这里就地拒绝。
        raise UsageError(
            f"--instance-role 取值非法：{role!r}，只接受 {list(INSTANCE_ROLES)}"
            "（与 console/backend/runtime_mode.py 的枚举一致）")
    resolved = _git(root, "rev-parse", "--verify", requested_commit + "^{commit}").stdout.strip()
    if resolved != requested_commit:
        raise UsageError(f"--target-commit 不是完整提交：{requested_commit}")
    receipt["release"] = verify_release(root, release_dir, requested_commit)
    if dry_run:
        receipt["dry_run"] = True
        receipt["finished_at"] = _utc_now()
        return receipt
    if not env_file:
        raise UsageError("未声明 --env-file / CONSOLE_CUTOVER_ENV_FILE：拒绝在不知道"
                         "运行 env 在哪里的情况下改阶段配置")
    receipt["yaml_adoption"] = adopt_yaml(root)
    receipt["root_move"] = move_root_to_target(root, requested_commit, receipt["yaml_adoption"])
    receipt["final_commit"] = receipt["root_move"]["final_commit"]
    receipt["root_verification"] = verify_root_matches_release(root, release_dir)
    env_path = Path(env_file)
    if not env_path.is_file():
        raise UsageError(f"env 文件不存在：{env_path}（--env-file / CONSOLE_CUTOVER_ENV_FILE）")
    import cutover_writers as writers
    env_before = {key: writers._read_env_value(env_path, key) for key in ENV_KEYS}
    step = "image"
    try:
        # 顺序有意为之（复核 A）：先把镜像与 PG 服务准备好，**最后**才动生产 env。
        # env 一旦写成 postgres/新角色，下一次容器重建就会按目标态启动；准备阶段
        # 失败时不能留下这个状态，所以失败要回滚并落一份 failed 回执（含 before/after）。
        receipt["image"] = _build_image(root)
        step = "pg_service"
        receipt["pg_service"] = _start_pg_service(root)
        step = "env"
        receipt["env"] = configure_env_file(env_path, instance_role=role)
    except BaseException as exc:
        rollback = _restore_env(env_path, env_before)
        if rollback:
            receipt["env_rollback"] = rollback
        receipt.update({"status": "failed", "failed_step": step,
                        "error": type(exc).__name__, "finished_at": _utc_now(),
                        "env_before": env_before})
        _write_receipt(report_path, receipt)
        raise
    receipt.update({"status": "ok", "finished_at": _utc_now()})
    _write_receipt(report_path, receipt)
    return receipt


def _restore_env(path: Path, before: dict) -> dict | None:
    """把 env 恢复到准备前的值（只回写真的被改过的键）。"""
    import cutover_writers as writers
    reverted = {}
    for key, value in before.items():
        if writers._read_env_value(path, key) != value:
            writers._set_env_value(path, key, value)
            reverted[key] = value
    return {"restored_keys": sorted(reverted)} if reverted else None


def _write_receipt(report_path: Path, receipt: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def _configured_command(env_key: str, default: list[str]) -> list[str]:
    raw = (os.environ.get(env_key) or "").strip()
    return shlex.split(raw) if raw else list(default)


def _build_image(root: Path) -> dict:
    """预构建候选镜像（只 build，不 up；新 app 由维护窗口按阶段启动）。"""
    build = _run_steps(_configured_command("CONSOLE_CUTOVER_BUILD_CMD",
                                           ["docker", "compose", "build", "console"]), root)
    if build["exit"] != 0:
        raise PrepareError(f"镜像预构建失败：{build}")
    identity = _run_steps(_configured_command("CONSOLE_CUTOVER_IMAGE_ID_CMD",
                                              ["docker", "compose", "images", "-q", "console"]), root)
    tail = (identity["stdout_tail"] or "").strip()
    build["image_id"] = tail.splitlines()[-1] if tail else None
    build["image_id_cmd_exit"] = identity["exit"]
    return build


def _start_pg_service(root: Path) -> dict:
    """首次创建/启动 PG 服务并**真正**等就绪（只有本控制器负责首次服务创建，§9 D3）。

    就绪是**轮询到真能连上**为止，不是「命令返回 0 就算」：`docker compose up -d`
    返回时 PG 还在初始化是常态。
    """
    up = _run_steps(_configured_command("CONSOLE_CUTOVER_PG_UP_CMD",
                                        ["docker", "compose", "up", "-d", "--wait",
                                         "console-postgres"]), root)
    if up["exit"] != 0:
        raise PrepareError(f"PG 服务创建/启动失败：{up}")
    timeout = float(os.environ.get("CONSOLE_CUTOVER_PG_READY_TIMEOUT") or 90)
    deadline = runtime.time.monotonic() + timeout
    ready_cmd = (os.environ.get("CONSOLE_CUTOVER_PG_READY_CMD") or "").strip()
    last = None
    while True:
        if ready_cmd:
            last = _run_steps(shlex.split(ready_cmd), root)
            ok = last["exit"] == 0
        else:
            from pg_migration_lib import connect_target
            try:
                conn = connect_target(readonly=True, statement_timeout=30)
            except Exception as exc:  # noqa: BLE001 - 启动期任何异常都只是「还没好」
                last = {"ready_via": "psycopg", "error": type(exc).__name__}
                ok = False
            else:
                try:
                    row = conn.execute(
                        "SELECT current_database() AS database,"
                        " current_setting('server_version') AS server_version").fetchone()
                finally:
                    conn.close()
                return {"up": up, "ready_via": "psycopg", "ready": dict(row)}
        if ok:
            return {"up": up, "ready": last, "ready_via": "command"}
        if runtime.time.monotonic() > deadline:
            raise PrepareError(f"PG 在 {timeout:.0f}s 内未就绪：{last}")
        runtime.time.sleep(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--instance-role")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        receipt = prepare(args.root, requested_commit=args.target_commit,
                          release_dir=args.release_dir, report_path=args.report,
                          env_file=args.env_file, instance_role=args.instance_role,
                          dry_run=args.dry_run)
        if args.dry_run:
            # dry-run 不落盘（零副作用）；成功/失败路径都由 prepare 自己写回执
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (PrepareError, Exception) as exc:  # noqa: BLE001 - CLI 边界统一成退出码
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
