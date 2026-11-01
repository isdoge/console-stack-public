# -*- coding: utf-8 -*-
import hmac
import os

from flask import Response, request


def auth_enabled():
    return os.environ.get("CONSOLE_AUTH_ENABLED", "false").lower() in ("1", "true", "yes")


def _check_basic_auth():
    user = os.environ.get("CONSOLE_AUTH_USER", "").strip()
    password = os.environ.get("CONSOLE_AUTH_PASSWORD", "").strip()
    if not user or not password:
        return False
    auth = request.authorization
    return bool(
        auth
        and hmac.compare_digest(auth.username or "", user)
        and hmac.compare_digest(auth.password or "", password)
    )


def _unauthorized(message="需要登录"):
    return Response(message, 401, {"WWW-Authenticate": 'Basic realm="Console Stack"'})


def _allowed_origins():
    raw = os.environ.get("CONSOLE_CORS_ORIGINS", "").strip()
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def register_auth_hooks(app):
    @app.before_request
    def guard_console():
        if not auth_enabled():
            return None
        if _check_basic_auth():
            return None
        return _unauthorized()

    @app.after_request
    def restrict_cors(response):
        """覆盖 flask-cors 的通配配置；生产默认同源，开发按环境变量显式放行。"""
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed = _allowed_origins()
        if origin and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            response.headers.pop("Access-Control-Allow-Origin", None)
            response.headers.pop("Access-Control-Allow-Credentials", None)
        return response
