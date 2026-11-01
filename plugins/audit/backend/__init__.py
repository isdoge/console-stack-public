"""插件后端入口；导入领域模块时不注册 HTTP 或启动任务。"""


def register(app, context):
    from .routes import register as register_routes
    register_routes(app, context)
