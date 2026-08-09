"""统一响应格式与错误码（按技术架构约定）"""
from typing import Any, Optional
from fastapi.responses import JSONResponse

# 错误码（四位，按架构文档约定）
ERR_LIMIT = 4001          # 限额触顶
ERR_OWNED = 4002          # 已拥有该书
ERR_SNAPSHOT = 4003       # 商品快照过期
ERR_UNAUTHORIZED = 4101   # 未授权
ERR_NOT_FOUND = 4201      # 资源不存在
ERR_CONFLICT = 4300       # 一致性冲突（章节生成校验未过）
ERR_REVIEW = 4400         # 机审未过
ERR_INTERNAL = 5000       # 服务器内部错误


def ok(data: Any = None, msg: str = "ok") -> dict:
    return {"code": 0, "msg": msg, "data": data}


def err(code: int, msg: str, http_status: int = 200, data: Any = None) -> JSONResponse:
    return JSONResponse(status_code=http_status,
                        content={"code": code, "msg": msg, "data": data})


class BizError(Exception):
    """业务异常，由全局处理器转统一响应。"""
    def __init__(self, code: int, msg: str, http_status: int = 200):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(msg)
