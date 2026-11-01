"""审计与事件：HTTP 入口及与框架的接线。"""

from flask import jsonify, request

import monitor_events
from plugin_sdk import storage as sd


def register(app, context):
    @app.route("/api/oplog", methods=["GET"])
    def manage_oplog():
        """查询最新操作日志。q=关键字（host/svc/action 子串），limit=条数（1..500，默认 100）。"""
        raw_q = (request.args.get("q") or "").strip()
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "limit 必须是整数"}), 400
        limit = min(max(limit, 1), 500)
        ph = sd.ph()
        with sd.read_tx(db_path=context.db_path) as conn:
            if raw_q:
                like = f"%{raw_q}%"
                cursor = conn.execute(
                    f"SELECT * FROM oplog WHERE host {sd.like_op()} {ph}"
                    f" OR svc {sd.like_op()} {ph} OR action {sd.like_op()} {ph}"
                    f" ORDER BY id DESC LIMIT {ph}",
                    (like, like, like, limit),
                )
            else:
                cursor = conn.execute(
                    f"SELECT * FROM oplog ORDER BY id DESC LIMIT {ph}", (limit,)
                )
            logs = [dict(row) for row in cursor.fetchall()]
        return jsonify(logs)

    monitor_events.register_routes(app, lambda: context.db_path)
