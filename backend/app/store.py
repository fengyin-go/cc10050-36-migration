"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
cc10050 迁移场景额外支持可选的 JSON 落盘（app.runtime_state），让重启后凭据、
回执与迁移状态仍可回查；落盘不可用时自动退回纯内存。
"""
from __future__ import annotations

from typing import Any

from app.runtime_state import load_runtime_db, persist_runtime_db
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        persisted = load_runtime_db()
        if persisted is not None:
            self._tables: dict[str, list[dict[str, Any]]] = {
                name: [dict(row) for row in rows] for name, rows in persisted.items()
            }
            # 新模块/新种子键兜底：落盘里缺的模块用种子补齐。
            for name, rows in SEED_ROWS.items():
                self._tables.setdefault(name, [dict(row) for row in rows])
        else:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, **criteria: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if all(row.get(key) == value for key, value in criteria.items()):
                return row
        return None

    def persist(self) -> bool:
        """把当前全量表落盘（best-effort）。"""
        return persist_runtime_db(self._tables)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
