"""FastAPI 应用入口（M1）
挂载内容管线接口，统一响应格式、错误码、全局异常处理、建表。
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import os

from .core.response import err, BizError, ERR_INTERNAL
from .db import Base, engine
from .api import content as content_api

app = FastAPI(title="银发 AI 互动小说 API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 建表（生产建议用 Alembic 迁移，M1 直接 create_all）
Base.metadata.create_all(bind=engine)


@app.exception_handler(BizError)
def biz_error_handler(request: Request, exc: BizError):
    return err(exc.code, exc.msg, exc.http_status)


@app.exception_handler(Exception)
def unhandled_handler(request: Request, exc: Exception):
    return err(ERR_INTERNAL, f"内部错误: {exc}", http_status=500)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "novel-api", "version": "0.2.0"}


@app.get("/api/info")
def info():
    return {"name": "银发 AI 互动小说", "stage": "M1 内容管线",
            "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            "env": os.environ.get("APP_ENV", "dev")}


app.include_router(content_api.router)
