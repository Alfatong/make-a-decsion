"""FastAPI 应用入口（M1 骨架）
当前提供健康检查与版本信息，业务模块（内容/权益/订单/生成/审核）随 M1 逐步挂载。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="银发 AI 互动小说 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 内测期放开；上线前收敛到域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "novel-api", "version": "0.1.0"}


@app.get("/api/info")
def info():
    return {
        "name": "银发 AI 互动小说",
        "stage": "M1 内容管线开发",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "env": os.environ.get("APP_ENV", "dev"),
    }
