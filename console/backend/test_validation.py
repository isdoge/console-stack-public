# -*- coding: utf-8 -*-
"""Regression tests for validation.py — input sanitizers guarding shell/YAML/audit paths."""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from validation import (  # noqa: E402
    AUDIT_ACTIONS,
    SERVICE_ACTIONS,
    validate_action,
    validate_health_probe,
    validate_identifier,
    validate_port,
    validate_timestamp,
)


class TestValidateIdentifier:
    @pytest.mark.parametrize(
        "value",
        ["axonhub", "new-api", "mihomo.v2", "svc_01", "A", "1panel-axonhub"],
    )
    def test_accepts_legal_names(self, value):
        assert validate_identifier(value, "svc") == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "-leading-dash",
            ".leading-dot",
            "has space",
            "back`tick",
            "semi;colon",
            "pipe|inside",
            "dollar$sign",
            "quote'single",
            'quote"double',
            "back\\slash",
            "nul\x00byte",
            "newline\nhere",
            "return\rhere",
            "a" * 129,
        ],
    )
    def test_rejects_shell_metacharacters_and_control(self, value):
        with pytest.raises(ValueError):
            validate_identifier(value, "svc")

    def test_allow_empty_returns_blank(self):
        assert validate_identifier("", "svc", allow_empty=True) == ""
        assert validate_identifier(None, "svc", allow_empty=True) == ""

    def test_no_allow_empty_rejects_blank(self):
        with pytest.raises(ValueError):
            validate_identifier("", "svc")


class TestValidateAction:
    @pytest.mark.parametrize("action", sorted(SERVICE_ACTIONS))
    def test_accepts_service_actions(self, action):
        assert validate_action(action) == action

    @pytest.mark.parametrize(
        "action",
        ["exec", "rm", "kill", "prune", "build", "pull", "push", "cp", ""],
    )
    def test_rejects_non_whitelist(self, action):
        with pytest.raises(ValueError):
            validate_action(action)

    def test_audit_actions_superset(self):
        assert SERVICE_ACTIONS.issubset(AUDIT_ACTIONS)
        assert "caddy-reload" in AUDIT_ACTIONS
        assert "caddy-reload" not in SERVICE_ACTIONS


class TestValidatePort:
    @pytest.mark.parametrize("value,expected", [(80, 80), ("8080", 8080), (65535, 65535), (1, 1)])
    def test_accepts_valid(self, value, expected):
        assert validate_port(value) == expected

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_returns_none(self, value):
        assert validate_port(value) is None

    @pytest.mark.parametrize("value", [0, -1, 65536, 99999, "not-a-number", "8080abc"])
    def test_rejects_invalid(self, value):
        with pytest.raises(ValueError):
            validate_port(value)


class TestValidateTimestamp:
    def test_accepts_iso_like(self):
        assert validate_timestamp("2026-07-25T15:00:00Z") == "2026-07-25T15:00:00Z"

    @pytest.mark.parametrize("value", ["", None, "a" * 65, "has\nnewline", "has\x00nul", "has\rcr"])
    def test_rejects_bad(self, value):
        with pytest.raises(ValueError):
            validate_timestamp(value)


# ---------------------------------------------------------------------------
# Track L1：health_probe 三态与有限语义检查
# ---------------------------------------------------------------------------


class TestValidateHealthProbe:
    def test_none_and_true_mean_default_inference(self):
        assert validate_health_probe(None) is None
        assert validate_health_probe(True) is None

    @pytest.mark.parametrize("falsy", [False, "false", "FALSE", "0", "no", "off"])
    def test_falsy_disables_probe(self, falsy):
        assert validate_health_probe(falsy) is False

    def test_minimal_object_gets_defaults(self):
        """只写 `type: http` 也可用：target 默认 domain，期望 200。"""
        assert validate_health_probe({"type": "http"}) == {
            "type": "http",
            "target": "domain",
            "expected_status": 200,
        }

    def test_full_object_round_trip(self):
        assert validate_health_probe(
            {
                "type": "http",
                "target": "tailscale",
                "expected_status": 204,
                "max_latency_ms": 1500,
                "body_contains": '"status":"ok"',
            }
        ) == {
            "type": "http",
            "target": "tailscale",
            "expected_status": 204,
            "max_latency_ms": 1500,
            "body_contains": '"status":"ok"',
        }

    def test_expected_status_can_be_cleared(self):
        """显式给空 = 不校验状态码（只看耗时/内容）。"""
        probe = validate_health_probe(
            {"type": "http", "expected_status": None, "max_latency_ms": 800}
        )
        assert "expected_status" not in probe
        assert probe["max_latency_ms"] == 800

    def test_unknown_field_is_rejected_not_silently_dropped(self):
        """静默丢弃会让用户以为配置生效了。"""
        with pytest.raises(ValueError, match="不支持的字段"):
            validate_health_probe({"type": "http", "headers": {"A": "b"}})

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "tcp"},
            {"type": "grpc"},
            {"type": "http", "target": "somewhere"},
            {"type": "http", "expected_status": "abc"},
            {"type": "http", "expected_status": 99},
            {"type": "http", "expected_status": 600},
            {"type": "http", "expected_status": True},
            {"type": "http", "max_latency_ms": 0},
            {"type": "http", "max_latency_ms": 999999},
            {"type": "http", "max_latency_ms": "fast"},
            {"type": "http", "body_contains": "x" * 257},
        ],
    )
    def test_invalid_objects_rejected(self, bad):
        with pytest.raises(ValueError):
            validate_health_probe(bad)

    def test_non_dict_non_bool_rejected(self):
        with pytest.raises(ValueError, match="必须是 false 或对象"):
            validate_health_probe(["http"])

    def test_credentials_have_no_way_in(self):
        """token/header/凭据一律无字段可落 —— 设计 §5.5 明令不进 assets.yaml。"""
        for field in ("headers", "token", "authorization", "password", "script"):
            with pytest.raises(ValueError):
                validate_health_probe({"type": "http", field: "x"})
