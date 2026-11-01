"""组合构建身份：只读取固定白名单字段，不输出路径或运行配置。"""
import json
import math
import os
import re
from pathlib import Path

import composition

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")


class IdentityError(ValueError):
    """构建产物身份缺失、损坏或与应用不匹配。"""


def validate(document):
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "kernel", "plugins", "composition_digest",
    }:
        raise IdentityError("组合构建身份字段无效")
    if type(document["schema_version"]) not in (int, float) or document["schema_version"] != 1:
        raise IdentityError("组合构建身份版本不支持")
    kernel = document["kernel"]
    if not isinstance(kernel, dict) or set(kernel) != {"version", "digest"}:
        raise IdentityError("内核构建身份无效")
    if not isinstance(kernel["version"], str) or not _VERSION.fullmatch(kernel["version"]):
        raise IdentityError("内核版本无效")
    for digest in (kernel["digest"], document["composition_digest"]):
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise IdentityError("组合摘要无效")
    if not isinstance(document["plugins"], list):
        raise IdentityError("组合插件身份必须是数组")
    seen = set()
    for plugin in document["plugins"]:
        if not isinstance(plugin, dict) or set(plugin) != {"id", "version", "digest"}:
            raise IdentityError("插件构建身份字段无效")
        pid = plugin["id"]
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid in seen:
            raise IdentityError("插件构建身份重复或无效")
        if not isinstance(plugin["version"], str) or not _VERSION.fullmatch(plugin["version"]):
            raise IdentityError("插件版本无效")
        if not isinstance(plugin["digest"], str) or not _DIGEST.fullmatch(plugin["digest"]):
            raise IdentityError("插件摘要无效")
        seen.add(pid)
    return document


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IdentityError(f"组合 JSON 含重复键: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise IdentityError(f"组合 JSON 不允许非有限数值: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        _reject_constant(value)
    return result


def read_json(path, *, label="组合构建身份"):
    """严格读取构建输入；路径与其祖先均不能经过符号链接。"""
    try:
        file = Path(path)
        if not file.is_absolute():
            raise IdentityError(f"{label}必须是绝对路径")
        file = composition.resolve_root_path(Path.cwd(), label, file)
        if not file.is_file():
            raise IdentityError(f"{label}缺失或不是普通文件")
        return json.loads(
            file.read_text(encoding="utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except IdentityError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise IdentityError(f"{label}不可读取或格式错误") from exc


def read(path):
    return validate(read_json(path))


def current():
    configured = (os.environ.get("CONSOLE_BUILD_IDENTITY_FILE") or "").strip()
    return read(configured) if configured else None


def verify_static(static_root):
    expected = current()
    if expected is not None:
        actual = read(Path(static_root) / "build-identity.json")
        if actual != expected:
            raise IdentityError("前后端组合身份不一致，拒绝启动")
    return expected
