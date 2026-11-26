"""业务路由汇总：cc10050 场景显式接口先注册，通用资源路由兜底分派。"""
from __future__ import annotations

from app.routers import resource, scenario

ROUTERS = [scenario, resource]
