"""
主应用入口 - FastAPI
整合 NL2SQL + 多数据源 + 安全 + 审计
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from loguru import logger

from backend.adapters.registry import get_adapter
from backend.config.config import settings
from backend.config.audit import init_audit_db
from backend.database import init_db
from .routers import chat_module, user



"""
在 FastAPI 中，lifespan 是应用生命周期钩子，用于：
应用启动时：执行初始化操作（如连接数据库、预加载数据）
应用关闭时：执行清理操作（如关闭连接、释放资源）
"""
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动 / 关闭时的生命周期钩子
    logger.info("Starting NL2SQL Pre-research System...")
    logger.info(f"当前 LLM Provider: {settings.llm_provider}")
    logger.info(f"当前模型: {settings.llm_model}")
    
    # 初始化审计数据库

    init_audit_db()
    init_db()
    logger.info("User ORM tables initialized.")
    
    # 预加载 demo 适配器
    get_adapter("sqlite_demo")
    logger.info("Demo SQLite adapter initialized.")
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="NL2SQL 智能查询系统 (预研版)",
    description="基于公司内部模型 / Qwen 的多数据源自然语言查询系统 - 可行性预研",
    version="0.1.0-mvp",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # 允许跨域请求携带认证信息 cookie
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有 HTTP 头
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Return frontend-friendly error payloads for management APIs."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": str(exc.detail),
            "data": None,
        },
    )


@app.get("/")
async def root():
    return {
        "message": "NL2SQL 智能查询系统运行中",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "supported_data_sources": ["sqlite_demo"],
        "docs": "/docs"
    }

app.include_router(chat_module.router)
app.include_router(user.router)
app.include_router(user.v1_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
