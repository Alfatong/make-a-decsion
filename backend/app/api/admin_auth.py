"""B 端管理鉴权：登录签发 token（Redis 存，TTL 12h）+ admin 接口校验依赖。
账号从环境变量 ADMIN_USERNAME / ADMIN_PASSWORD 读取。"""
import uuid, os
from fastapi import APIRouter, Depends, Header, HTTPException
import redis

from ..core.config import settings
from ..core.response import ok, BizError

router = APIRouter(prefix="/admin/api", tags=["admin-auth"])

TOKEN_TTL = 60 * 60 * 12
_redis = None


def _r():
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def verify_admin(x_admin_token: str = Header(default="")):
    """admin 接口鉴权依赖：校验 token 有效性。"""
    if not x_admin_token or not _r().get(f"admin:token:{x_admin_token}"):
        raise BizError(4004, "未登录或登录已过期")
    return x_admin_token


from pydantic import BaseModel


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn):
    au = os.environ.get("ADMIN_USERNAME", "admin")
    ap = os.environ.get("ADMIN_PASSWORD", "")
    if not ap or body.username != au or body.password != ap:
        raise BizError(4004, "账号或密码错误")
    token = uuid.uuid4().hex
    _r().setex(f"admin:token:{token}", TOKEN_TTL, body.username)
    return ok({"token": token, "expires_in": TOKEN_TTL})


@router.post("/logout")
def logout(token: str = Depends(verify_admin)):
    _r().delete(f"admin:token:{token}")
    return ok({})
