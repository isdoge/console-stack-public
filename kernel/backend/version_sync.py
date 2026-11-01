# -*- coding: utf-8 -*-
"""定时版本同步脚本（NAS cron 每 30 分钟经 docker exec 运行）。

F16（2026-09-08）：git 子进程 30 秒超时；非零退出/超时/非法计数绝不当作
版本或 behind=0/最新；source 失败保留上一轮成功行（含 last_checked），不为
不可用 checkout 新插 Unknown/零行；逐服务失败摘要只含 服务/阶段/类别/耗时，
不输出命令、stderr 或 remote URL。

采集门禁（PG-02 / Q1）：本脚本只应由 authority 实例运行。client 上 `sync_all_versions()`
直接返回并打印一行说明——避免 Mac 侧误装 cron 后成为第二个采集者。
"""
import json
import os
import re

# 如果有 gitpython 可以引入，这里我们用标准 subprocess 保证零依赖/极简环境也可执行
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

import collect_guard
import composition
import paths
import storage_dispatch as sd
import version_service as vs

DB_PATH = paths.DB_PATH
GIT_TIMEOUT_SECONDS = 30


class SourceGitFailure(Exception):
    """source 类型 git 同步失败（脱敏：仅 stage/category/耗时）。

    不带 stderr/异常原文/命令——那些可能包含 remote URL 或路径。
    """

    def __init__(self, stage, category, elapsed_ms=None):
        self.stage = stage
        self.category = category
        self.elapsed_ms = elapsed_ms
        super().__init__("%s:%s" % (stage, category))


@dataclass
class GitResult:
    """结构化 git 结果。失败绝不返回“Error: …”字符串冒充 stdout。

    stderr/stdout 可能包含 remote URL，不对外输出；仅 category 供摘要使用。
    """
    ok: bool
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def value(self):
        """成功时的去空白 stdout；失败返回空串（绝不返回错误原文）。"""
        return self.stdout.strip() if self.ok else ""

    @property
    def category(self):
        """脱敏失败类别：timeout / exit / error，成功为 None。"""
        if self.ok:
            return None
        if self.timed_out:
            return "timeout"
        if self.stderr.strip() or self.stdout.strip():
            return "exit"
        return "error"


def _decode_bytes(value):
    """stderr/stdout 在超时时可能是 bytes，text=True 时是 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def run_git(repo_path, args):
    """
    在指定目录执行 git 命令，30 秒超时，返回 GitResult。

    失败绝不返回“Error: …”字符串冒充 stdout；调用方必须把失败当失败，
    不能把错误文本当版本或把失败当 behind=0/最新。
    """
    cmd = ["git", "-C", repo_path] + args
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            check=True, timeout=GIT_TIMEOUT_SECONDS,
        )
        return GitResult(ok=True, stdout=res.stdout, stderr=res.stderr)
    except subprocess.TimeoutExpired as exc:
        return GitResult(ok=False, timed_out=True, stderr=_decode_bytes(exc.stderr))
    except subprocess.CalledProcessError as exc:
        return GitResult(ok=False, stdout=_decode_bytes(exc.stdout),
                         stderr=_decode_bytes(exc.stderr))
    except (OSError, ValueError) as exc:
        return GitResult(ok=False, stderr=str(exc))


def run_git_cmd(repo_path, args):
    """
    兼容薄包装：返回成功 stdout 字符串；失败返回空字符串。
    （source 同步主路径使用结构化的 run_git，不再把错误文本当返回值。）
    """
    return run_git(repo_path, args).value


def _source_git_step(deploy_path, stage, args):
    """执行单个 git 阶段；失败抛 SourceGitFailure（脱敏类别 + 耗时）。"""
    started = time.monotonic()
    result = run_git(deploy_path, args)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not result.ok:
        raise SourceGitFailure(stage, result.category or "error", elapsed_ms)
    return result


def _resolve_assets_path():
    return str(composition.config_path("assets.yaml", "CONSOLE_ASSETS_PATH"))


ASSETS_PATH = _resolve_assets_path()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def gh_api_request(url_path):
    """
    请求 GitHub REST API，自动附带只读 Token
    """
    url = f"https://api.github.com{url_path}"
    headers = {
        "Accept": "application.vnd.github.v3+json",
        "User-Agent": "OpsConsole-Sync/1.0"
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"GitHub API Error for {url_path}: {e}")
        return None


def get_docker_image_digest_from_github(repo_url):
    """
    通过 GitHub Packages 或 Docker Hub 镜像，这里用最轻量的 GitHub Atom feed 获取最新 Release 作为替代比对，
    因为直连 GHCR/Registry 握手极为繁琐。
    """
    # 提取 owner/repo
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url)
    if not match:
        return None
    owner, repo = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]

    # 查最新 Release tag
    data = gh_api_request(f"/repos/{owner}/{repo}/releases/latest")
    if data and "tag_name" in data:
        return data["tag_name"]
    return None


def sync_all_versions(db_path=None, conn=None):
    """同步所有开启版本追踪的服务；非 authority 实例直接返回（Q1）。"""
    if not collect_guard.collection_allowed():
        print(collect_guard.collection_skipped("版本同步"))
        return

    if not os.path.exists(ASSETS_PATH):
        print(f"Assets yaml not found at {ASSETS_PATH}")
        return

    with open(ASSETS_PATH, 'r', encoding='utf-8') as f:
        services = yaml.safe_load(f) or []

    failed = []
    for svc in services:
        name = svc.get("name")
        v_conf = svc.get("version", {})
        if not v_conf.get("enabled"):
            continue

        try:
            _sync_one(conn, svc, name, v_conf, db_path=db_path)
        except SourceGitFailure as exc:
            # F16：source git 失败已保留 prior 行（或未写任何行），这里只记
            # 脱敏摘要（服务/阶段/类别/耗时），绝不输出 stderr/命令/remote。
            failed.append("%s: %s(%s, %dms)"
                          % (name, exc.stage, exc.category, exc.elapsed_ms or 0))
        except Exception as exc:
            # 单个服务出错**绝不能**中止整轮同步。2026-09-07 实测过代价：
            # `axonhub-oracle`（21 个开版本追踪的服务里排第 2）因 us 缺 docker 能力
            # 抛 ValueError，把后面 19 个服务的版本数据一起冻住，每 900s 复现一次，
            # 而外层只打印一句 "background version sync failed" —— 从现象上看不出
            # 波及面。现在改成逐服务隔离：坏一个跳一个，末尾汇总报出来。
            failed.append("%s: %s" % (name, exc))

    if failed:
        print("Version synchronization completed with %d failure(s): %s"
              % (len(failed), "; ".join(failed)))
    else:
        print("Version synchronization completed.")


def _upsert_version(conn, name, local_version, upstream_version, behind_count,
                    update_status, recent_changes, now_str):
    """写一行版本记录。表由 schema 迁移建好，应用侧不做 DDL。"""
    ph = sd.ph()
    conn.execute(
        f"""
        INSERT INTO versions (svc_name, local_version, upstream_version, behind_count,
                              update_status, recent_changes, last_checked)
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        ON CONFLICT(svc_name) DO UPDATE SET
            local_version=excluded.local_version,
            upstream_version=excluded.upstream_version,
            behind_count=excluded.behind_count,
            update_status=excluded.update_status,
            recent_changes=excluded.recent_changes,
            last_checked=excluded.last_checked
        """,
        (name, local_version, upstream_version, behind_count, update_status,
         recent_changes, now_str),
    )


def _sync_source(conn, svc, name, v_conf, deploy_path, db_path=None):
    """source 类型同步。

    任何 git 失败抛 SourceGitFailure → 不执行下方 UPSERT → DB 中的 prior
    成功行（含 last_checked）自然保留，也不为不可用 checkout 新插 Unknown/零行。
    """
    branch = v_conf.get("branch", "main")
    repo_url = v_conf.get("repo_url", "")

    # 1. 读本地 / fetch / 计数 / 远端，任一失败即中止本服务（保留 prior 行）。
    local_result = _source_git_step(
        deploy_path, "local", ["rev-parse", "--short", "HEAD"])
    local_version = local_result.value
    _source_git_step(deploy_path, "fetch", ["fetch", "-q"])
    behind_result = _source_git_step(
        deploy_path, "behind", ["rev-list", "--count", f"HEAD..origin/{branch}"])
    # 非法计数（空/非数字）不得当作 behind=0：rev-list --count 合法输出必为数字
    if not behind_result.value.isdigit():
        raise SourceGitFailure("behind", "invalid")
    behind_count = int(behind_result.value)
    upstream_result = _source_git_step(
        deploy_path, "upstream", ["rev-parse", "--short", f"origin/{branch}"])
    upstream_version = upstream_result.value

    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    recent_changes = "[]"
    update_status = "落后" if behind_count > 0 else "最新"

    # 2. 提取上游 github owner/repo（gh 失败返回 None → 保留 origin 短 hash）
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url)
    if match:
        owner, repo = match.groups()
        if repo.endswith(".git"):
            repo = repo[:-4]
        rel_data = gh_api_request(f"/repos/{owner}/{repo}/releases/latest")
        if rel_data and "tag_name" in rel_data:
            upstream_version = rel_data["tag_name"]
        commits_data = gh_api_request(
            f"/repos/{owner}/{repo}/commits?sha={branch}&per_page=5")
        if commits_data:
            changes = []
            for c in commits_data:
                msg = c.get("commit", {}).get("message", "").split("\n")[0]
                sha = c.get("sha", "")[:7]
                changes.append({"sha": sha, "msg": msg})
            recent_changes = json.dumps(changes)

    # 3. 只有全流程成功才写行 / 刷新 last_checked。
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        _upsert_version(own, name, local_version, upstream_version, behind_count,
                        update_status, recent_changes, now_str)


def _sync_one(conn, svc, name, v_conf, db_path=None):
    """同步单个服务的版本。抛异常由调用方隔离，不再牵连其余服务。"""
    kind = v_conf.get("kind", "image")
    deploy_path = svc.get("deploy_path", "")

    local_version = "Unknown"
    upstream_version = "Unknown"
    behind_count = 0
    update_status = "Unknown"
    recent_changes = "[]"
    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if kind == "source" and deploy_path and os.path.exists(deploy_path):
        _sync_source(conn, svc, name, v_conf, deploy_path, db_path=db_path)
        return

    if kind == "source":
        # source 但 deploy_path 缺失/不存在 → checkout 不可用，不写伪装成功行
        raise SourceGitFailure("deploy_path", "error")

    # image / http：委托 version_service。未接管（返回 None）→ 写兜底 Unknown 行。
    # 其余 kind 保持既有语义：不委托，直接写兜底行。
    if kind in ("image", "http"):
        row = vs.sync_service_version(svc, db_path=db_path, conn=conn)
        if row:
            return
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        _upsert_version(own, name, local_version, upstream_version, behind_count,
                        update_status, recent_changes, now_str)


if __name__ == "__main__":
    sync_all_versions()
