"""运行期状态：数据目录寻址、JSON 原子落盘与轻量数据库持久化。

设计说明：
- 原型默认是内存仓库（app.store），本模块在其之外提供一份可选的 JSON 落盘，
  使“新版构建与部署/重启后仍可回查、凭据可复验”成立。
- 路径一律相对 backend 根目录解析，可用环境变量覆盖；Docker 内对应 /srv/app/data。
- 任何落盘失败都不得打断主流程（退回纯内存行为）。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SEED_VERSION = "cc10050-v1"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(BACKEND_ROOT / "data")))
CATALOG_DIR = Path(os.getenv("VERSION_CATALOG_DIR", str(DATA_DIR / "version-catalog")))
DB_FILE = DATA_DIR / "runtime_db.json"
STATE_FILE = DATA_DIR / "scenario_state.json"


def ensure_dirs() -> None:
    for path in (
        DATA_DIR,
        CATALOG_DIR,
        CATALOG_DIR / "snapshots",
        CATALOG_DIR / "receipts",
        CATALOG_DIR / "deliveries",
        CATALOG_DIR / "attachments",
        CATALOG_DIR / "legacy",
        CATALOG_DIR / "rollbacks",
    ):
        path.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Any) -> bool:
    """原子写入 JSON；失败返回 False（调用方按纯内存模式继续）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_name, path)
        return True
    except Exception:
        return False


def read_json(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_runtime_db() -> dict[str, list[dict[str, Any]]] | None:
    payload = read_json(DB_FILE)
    if not isinstance(payload, dict):
        return None
    if payload.get("seed_version") != SEED_VERSION:
        return None
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return None
    return {str(k): list(v) for k, v in tables.items() if isinstance(v, list)}


def persist_runtime_db(tables: dict[str, list[dict[str, Any]]]) -> bool:
    return atomic_write_json(
        DB_FILE, {"seed_version": SEED_VERSION, "tables": tables}
    )


def default_state() -> dict[str, Any]:
    return {
        "seed_version": SEED_VERSION,
        "active_caliber": "legacy",
        "receipt_seq": 0,
        "rollback_seq": 0,
        "delivery_seq": 0,
        "latest_receipt_id": None,
        "migration_status": "not_run",
        "switch_history": [],
        "rollback_history": [],
    }


def load_state() -> dict[str, Any]:
    payload = read_json(STATE_FILE)
    state = default_state()
    if isinstance(payload, dict):
        state.update({k: v for k, v in payload.items() if k in state})
    return state


def save_state(state: dict[str, Any]) -> bool:
    return atomic_write_json(STATE_FILE, state)
