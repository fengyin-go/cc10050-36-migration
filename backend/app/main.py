"""商品价格监控采集平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8020
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.runtime_state import load_state
from app.services import service_for
from app.store import store
from app.version_catalog import bootstrap as bootstrap_catalog


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 幂等建立“版本目录”、旧环境留档与种子交付凭据清单/附件。
    bootstrap_catalog()
    # 新版部署后即出具第一份换算迁移回执（已完成过则不重复）。
    state = load_state()
    if state.get("migration_status") in (None, "not_run"):
        try:
            service_for("currency_conv").migrate()
        except Exception:
            pass
    yield


app = FastAPI(title="商品价格监控采集平台", version="1.1.0-cc10050", lifespan=lifespan)

# 导入即幂等引导：兼容不触发 lifespan 的 TestClient/直接导入场景。
try:
    bootstrap_catalog()
    if load_state().get("migration_status") in (None, "not_run"):
        service_for("currency_conv").migrate()
except Exception:  # pragma: no cover - 引导失败不应阻断应用导入
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names()),
            "release": "cc10050-version-catalog"}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)
