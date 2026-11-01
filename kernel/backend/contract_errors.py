"""跨组件协议字段异常；HTTP 层保持既有 400 映射。"""


class ContractError(ValueError):
    """协议字段不合法；HTTP 层映射为 400，回执不一致映射为 409。"""
