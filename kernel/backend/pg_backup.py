"""通用 PG 备份原语；只依赖标准库，不导入私人附件或业务模块。

快照持有者、摘要查询和 pg_dump 使用同一 exported snapshot。附件由组合层显式
提供适配器，完整标记最后发布；旧宿主入口通过薄委托继续使用这些原语。
"""
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROW_DIGEST_ALGORITHM = "md5-pg-row-json-1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def snapshot_sql(snapshot):
    if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-\d+", snapshot):
        raise RuntimeError("PG 导出 snapshot 标识无效")
    return ("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
            f"SET TRANSACTION SNAPSHOT '{snapshot}'; ")


@contextmanager
def exported_pg_snapshot(psql_cmd, *, timeout=600):
    """持有只读快照直至全部消费者结束；父进程消失后 stdin EOF 自动回滚。"""
    if not psql_cmd or psql_cmd[-1] not in ("-c", "-Atc"):
        raise RuntimeError("未配置可导出 snapshot 的 psql 命令")
    command = list(psql_cmd[:-1])
    if command[:2] == ["docker", "exec"] and "-i" not in command[:4]:
        command.insert(2, "-i")
    command += ["-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        try:
            proc.stdin.write((
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                f"SET LOCAL idle_in_transaction_session_timeout = '{int(2 * timeout + 180)}s';\n"
                "SELECT pg_export_snapshot();\n").encode())
            proc.stdin.flush()
            deadline, output = time.monotonic() + min(timeout, 60), b""
            while b"\n" not in output:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([proc.stdout], [], [], max(0, remaining))[0]:
                    raise RuntimeError("PG 导出 snapshot 超时")
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    raise RuntimeError("PG 导出 snapshot 进程提前退出")
                output += chunk
                if len(output) > 4096:
                    raise RuntimeError("PG 导出 snapshot 输出异常")
            snapshot = output.decode().strip()
            snapshot_sql(snapshot)
            yield snapshot
            if proc.poll() is not None:
                raise RuntimeError("PG snapshot 保持进程提前退出，拒绝发布备份")
        finally:
            try:
                proc.communicate(b"ROLLBACK;\n\\q\n", timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()


def query_rows(psql_cmd, sql, *, timeout=120, stage="", snapshot=None):
    """失败返回 None，成功空查询返回 []；不把连接/权限错误伪装成空台账。"""
    if not psql_cmd:
        print(f"WARN: 未配置 psql 命令，无法读取{stage}", file=sys.stderr)
        return None
    try:
        command = list(psql_cmd)
        if snapshot is not None:
            sql = snapshot_sql(snapshot) + sql + "; COMMIT;"
            command = [*command[:-1], "-X", "-q", "-v", "ON_ERROR_STOP=1", command[-1]]
        result = subprocess.run([*command, sql], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: {stage}查询不可用（{type(exc).__name__}）", file=sys.stderr)
        return None
    if result.returncode != 0:
        # 不输出任意服务器错误正文：其中可能包含查询参数或私人数据。
        print(f"WARN: {stage}查询失败（exit {result.returncode}）", file=sys.stderr)
        return None
    return [line for line in (result.stdout or "").splitlines() if line.strip()]


def run_streaming(args, dest_tmp, *, stdin_path=None, timeout=600, quiet=False):
    """命令输出落盘并 fsync；错误只报告退出码，不回显 DSN/命令参数。"""
    out = open(os.devnull if quiet else dest_tmp, "wb")
    stdin_handle = open(stdin_path, "rb") if stdin_path else None
    try:
        result = subprocess.run(
            args, stdout=out, stdin=stdin_handle if stdin_handle else subprocess.DEVNULL,
            timeout=timeout,
        )
    finally:
        if stdin_handle is not None:
            stdin_handle.close()
        out.close()
    if not quiet:
        with open(dest_tmp, "rb+") as handle:
            os.fsync(handle.fileno())
    if result.returncode != 0:
        if not quiet:
            Path(dest_tmp).unlink(missing_ok=True)
        raise RuntimeError(f"PG 工具失败 (exit {result.returncode})")
    return dest_tmp


def read_command(command, timeout=30):
    if not command:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or "").strip() or None if result.returncode == 0 else None


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_digest_of(tables):
    payload = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def digest_sql(tables, amount_columns=None):
    """表与列必须来自已验证的组件 owner；禁止把任意 SQL 标识符直接拼入。"""
    tables = tuple(tables)
    amount_columns = amount_columns or {}
    if (not tables or len(set(tables)) != len(tables)
            or any(not isinstance(t, str) or not _IDENTIFIER.fullmatch(t) for t in tables)
            or set(amount_columns) - set(tables)):
        raise ValueError("备份表清单无效")
    parts = []
    for table in tables:
        parts.append(f"SELECT '{table}.rows', count(*)::text FROM \"{table}\"")
        parts.append(
            f"SELECT '{table}.row_hash', md5(coalesce(string_agg("
            f"row_to_json(t)::text, chr(10) ORDER BY row_to_json(t)::text), '')) "
            f"FROM \"{table}\" t"
        )
        for column in amount_columns.get(table, ()):
            if not isinstance(column, str) or not _IDENTIFIER.fullmatch(column):
                raise ValueError("备份金额列无效")
            parts.append(
                f"SELECT '{table}.amount.{column}', "
                f"coalesce(sum(\"{column}\")::text, '-') FROM \"{table}\""
            )
    return "\nUNION ALL\n".join(parts)


def content_digest(psql_cmd, tables, *, amount_columns=None, timeout=120, snapshot=None):
    tables = tuple(tables)
    rows = query_rows(
        psql_cmd, digest_sql(tables, amount_columns), timeout=timeout,
        stage="内容摘要", snapshot=snapshot,
    )
    if rows is None:
        return None
    result = {}
    for line in rows:
        key, _, value = line.partition("|")
        parts = key.split(".")
        if len(parts) < 2 or parts[0] not in tables:
            return None
        entry = result.setdefault(parts[0], {})
        if parts[1] == "rows":
            try:
                entry["rows"] = int(value)
            except ValueError:
                return None
        elif parts[1] == "row_hash":
            entry["row_hash"] = value
        elif parts[1] == "amount" and len(parts) == 3:
            entry.setdefault("amounts", {})[parts[2]] = value
        else:
            return None
    if set(result) != set(tables) or any("rows" not in e or "row_hash" not in e for e in result.values()):
        return None
    return {"algorithm": ROW_DIGEST_ALGORITHM, "tables": result, "digest": row_digest_of(result)}


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_group(pairs):
    """按顺序发布成员，最后一项必须是完成标记；原组保留恢复硬链接。"""
    pairs = [(Path(source), Path(target)) for source, target in pairs]
    if len(pairs) < 2:
        raise ValueError("备份组至少需要 dump 与完成标记")
    targets = [target for _, target in pairs]
    if len(set(targets)) != len(targets) or len({p.parent for p in targets}) != 1:
        raise ValueError("备份组成员必须同目录且不重复")
    for source, target in pairs:
        if source.is_symlink() or not source.is_file():
            raise ValueError("待发布备份成员必须是普通文件")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("PG 备份组成员必须是普通文件")
    previous, prior = None, {}
    parent = targets[0].parent
    if any(target.exists() for target in targets):
        previous = Path(tempfile.mkdtemp(dir=parent, prefix=f".{targets[0].name}.previous-"))
        for target in targets:
            if target.exists():
                saved = previous / target.name
                os.link(target, saved)
                prior[target] = saved
        fsync_directory(previous)
        fsync_directory(parent)
    settled = False
    try:
        targets[-1].unlink(missing_ok=True)
        fsync_directory(parent)
        for source, target in pairs:
            os.replace(source, target)
        fsync_directory(parent)
        settled = True
    except BaseException:
        targets[-1].unlink(missing_ok=True)
        for target in targets:
            if target not in prior:
                target.unlink(missing_ok=True)
                continue
            descriptor, name = tempfile.mkstemp(dir=parent, prefix=target.name + ".restore-")
            os.close(descriptor)
            temporary = Path(name)
            temporary.unlink()
            try:
                os.link(prior[target], temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        fsync_directory(parent)
        settled = True
        raise
    finally:
        if settled and previous is not None:
            for saved in prior.values():
                saved.unlink()
            previous.rmdir()
