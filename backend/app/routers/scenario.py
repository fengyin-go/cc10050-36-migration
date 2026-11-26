"""cc10050 迁移场景专用接口。

显式路径覆盖题面中的操作；同一能力尽量提供多种路径/方法别名，方便不同调用习惯
（HTTP 脚本、Playwright、服务内方法直调）。所有业务结果都带 ok/message；
找不到的资源用 404 + ok:false，业务不可消费用 200 + ok:false + reason。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.services import service_for
from app.version_catalog import get_attachment_path

router = APIRouter(prefix="/api", tags=["cc10050-迁移与交付凭据"])


def currency_service():
    return service_for("currency_conv")


def export_service():
    return service_for("price_export")


async def collect(request: Request) -> dict[str, Any]:
    """合并 query 参数与 JSON body（body 优先），让 GET/POST、query/json 都能调。"""
    data: dict[str, Any] = {k: v for k, v in request.query_params.items()}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:
            pass
    return data


def _float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ===========================================================================
# 货币换算器：迁移 / 回执 / 版本 / 定位 / 解析 / 换算 / 对账 / 回滚
# ===========================================================================

@router.post("/currency_conv/migrate")
async def migrate(request: Request):
    data = await collect(request)
    fail_after = data.get("fail_after")
    try:
        fail_after = int(fail_after) if fail_after not in (None, "") else None
    except (TypeError, ValueError):
        fail_after = None
    return currency_service().migrate(fail_after=fail_after)


@router.post("/currency_conv/backfill")
async def backfill(_: Request):
    return currency_service().backfill()


@router.get("/currency_conv/migration")
async def migration_status():
    from app.version_catalog import get_receipt

    receipt = get_receipt("latest")
    return {"ok": True, "latest_receipt": receipt,
            "migration": receipt}


@router.get("/currency_conv/migration/receipt")
async def latest_receipt():
    from app.version_catalog import get_receipt

    receipt = get_receipt("latest")
    if receipt is None:
        # 兼容未触发 lifespan/未显式迁移的调用：惰性出具一份幂等回执。
        currency_service().migrate()
        receipt = get_receipt("latest")
    if receipt is None:
        return {"ok": False, "message": "尚无迁移回执，请先调用 /api/currency_conv/migrate"}
    return {"ok": True, "receipt": receipt, **receipt}


@router.get("/currency_conv/migration/receipt/{receipt_id}")
async def one_receipt(receipt_id: str):
    from app.version_catalog import get_receipt

    receipt = get_receipt(receipt_id)
    if receipt is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": f"回执 {receipt_id} 不存在"})
    return {"ok": True, "receipt": receipt, **receipt}


@router.get("/currency_conv/receipts/latest")
async def receipts_latest_alias():
    return await latest_receipt()


@router.post("/currency_conv/verify-source")
async def verify_source(request: Request):
    data = await collect(request)
    return currency_service().verify_source(
        association_key=data.get("association_key") or data.get("关联键"),
        snapshot_id=data.get("snapshot_id") or data.get("id"),
        source=str(data.get("source") or data.get("来源") or data.get("补核来源") or ""),
        provider=str(data.get("provider") or data.get("提供方") or ""),
        receipt_id=data.get("receipt_id"),
        verified_by=str(data.get("verified_by") or "运营补核"),
    )


@router.post("/currency_conv/migration/receipt/{receipt_id}/verify-source")
async def verify_receipt_source(receipt_id: str, request: Request):
    data = await collect(request)
    return currency_service().verify_source(
        association_key=data.get("association_key") or data.get("关联键"),
        snapshot_id=data.get("snapshot_id"),
        source=str(data.get("source") or data.get("来源") or ""),
        provider=str(data.get("provider") or ""),
        receipt_id=receipt_id,
    )


@router.get("/currency_conv/versions")
async def versions(date: str | None = None, pair: str | None = None):
    return currency_service().list_versions(date=date, pair=pair)


@router.get("/currency_conv/locate")
async def locate(date: str | None = None, pair: str | None = None,
                 source: str | None = None, target: str | None = None,
                 association_key: str | None = None):
    return currency_service().locate(
        date=date, pair=pair, source=source, target=target,
        association_key=association_key)


@router.get("/currency_conv/resolve")
async def resolve(date: str | None = None, pair: str | None = None,
                  source: str | None = None, target: str | None = None,
                  association_key: str | None = None,
                  snapshot_id: str | None = None, version: str | None = None,
                  prefer: str | None = None):
    from app.version_catalog import resolve_snapshot

    return resolve_snapshot(date=date, pair=pair, source=source, target=target,
                            association_key=association_key,
                            snapshot_id=snapshot_id, version=version, prefer=prefer)


@router.api_route("/currency_conv/convert", methods=["GET", "POST"])
async def convert(request: Request):
    data = await collect(request)
    amount = _float(data.get("amount") or data.get("金额"), 0.0) or 0.0
    return currency_service().convert_query(
        amount=amount,
        date=data.get("date") or data.get("快照日期"),
        pair=data.get("pair") or data.get("币对"),
        source=data.get("source") or data.get("源货币"),
        target=data.get("target") or data.get("目标货币"),
        association_key=data.get("association_key") or data.get("关联键"),
        snapshot_id=data.get("snapshot_id"),
        version=data.get("version"),
        prefer=data.get("prefer"),
    )


@router.get("/currency_conv/reconcile")
async def reconcile():
    return currency_service().reconcile()


@router.post("/currency_conv/rollback")
async def rollback(request: Request):
    data = await collect(request)
    return currency_service().rollback(reason=str(data.get("reason") or "模拟回滚旧环境"))


@router.post("/currency_conv/switch-blocked")
async def switch_blocked(request: Request):
    data = await collect(request)
    return currency_service().mark_switch_blocked(str(data.get("reason") or "新版切换受阻"))


@router.post("/currency_conv/simulate-failed-switch")
@router.post("/currency_conv/simulate-switch-failure")
async def simulate_failed_switch(_: Request):
    return currency_service().simulate_failed_switch()


# 顶层迁移别名（不同命名习惯）
@router.post("/migration/run")
async def migration_run(request: Request):
    return await migrate(request)


@router.post("/migration/backfill")
async def migration_backfill(_: Request):
    return currency_service().backfill()


@router.get("/migration/receipt")
async def migration_receipt_alias():
    return await latest_receipt()


@router.get("/migration/reconcile")
async def migration_reconcile_alias():
    return currency_service().reconcile()


@router.post("/migration/rollback")
async def migration_rollback_alias(request: Request):
    return await rollback(request)


# ===========================================================================
# 比价导出通道：凭据 / 继续办理 / 复算 / 关联键 / 绑定 / 附注 / 交付
# ===========================================================================

@router.post("/price_export/continue")
async def continue_application(request: Request):
    data = await collect(request)
    credential = str(data.get("credential") or data.get("交付凭据") or "").strip()
    return export_service().continue_with_credential(credential)


@router.api_route("/price_export/credential/{credential}", methods=["GET", "POST"])
async def credential_info(credential: str):
    info = export_service().credential_info(credential)
    if not info.get("found"):
        return JSONResponse(status_code=404, content=info)
    return info


@router.api_route("/price_export/credential/{credential}/recompute", methods=["GET", "POST"])
async def credential_recompute(credential: str, request: Request):
    data = await collect(request)
    amount = _float(data.get("amount") or data.get("金额"))
    return export_service().recompute(credential, amount)


@router.get("/price_export/credential/{credential}/attachments/{filename}")
async def credential_attachment(credential: str, filename: str):
    path = get_attachment_path(credential, filename)
    if path is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": f"附件 {filename} 不存在"})
    from urllib.parse import quote

    response = FileResponse(str(path))
    response.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(path.name)}"
    )
    return response


@router.get("/price_export/association/{key:path}")
async def association_lookup(key: str):
    result = export_service().association_lookup(key)
    if not result.get("found"):
        return JSONResponse(status_code=404, content=result)
    return result


@router.post("/price_export/association/lookup")
async def association_lookup_post(request: Request):
    data = await collect(request)
    key = str(data.get("association_key") or data.get("key") or data.get("关联键") or "")
    return export_service().association_lookup(key)


@router.api_route("/price_export/external/resolve", methods=["POST"])
@router.api_route("/price_export/import-external", methods=["POST"])
async def resolve_external(request: Request):
    """解析外部旧格式（旧表头 rq/hb/hl/gly/glj 或中文表头）并凭关联键找真实换算来源。"""
    import csv as _csv
    import io as _io

    data = await collect(request)
    key = str(data.get("association_key") or data.get("key") or data.get("关联键")
              or data.get("glj") or "").strip()
    legacy_preview: dict[str, Any] = {}

    text = data.get("csv") or data.get("content") or data.get("text")
    if isinstance(text, str) and text.strip():
        reader = _csv.DictReader(_io.StringIO(text.lstrip("﻿")))
        for row in reader:
            normalized = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            key = key or normalized.get("glj(关联键)") or normalized.get("glj") \
                or normalized.get("关联键") or ""
            legacy_preview = normalized
            break
    row = data.get("row")
    if isinstance(row, dict):
        legacy_preview = {**row}
        key = key or str(row.get("glj") or row.get("关联键") or "")

    if not key:
        return {"ok": False, "found": False,
                "message": "外部旧格式中没有关联键（glj），无法定位真实换算来源；"
                           "若同时缺少币对/日期，则不能编造汇率。",
                "legacy_row": legacy_preview}
    result = export_service().association_lookup(key)
    result["parsed_legacy_row"] = legacy_preview
    return result


@router.post("/price_export/{entry_id}/bind")
async def bind_snapshot(entry_id: int, request: Request):
    data = await collect(request)
    return export_service().bind_snapshot(entry_id, data)


@router.post("/price_export/{entry_id}/note")
async def generate_note(entry_id: int, request: Request):
    data = await collect(request)
    return export_service().generate_note(entry_id, data)


@router.post("/price_export/{entry_id}/deliver")
async def deliver(entry_id: int, request: Request):
    data = await collect(request)
    ok = data.get("ok", True)
    if isinstance(ok, str):
        ok = ok not in ("false", "0", "否", "失败")
    return export_service().deliver(entry_id, ok=bool(ok))


@router.post("/price_export/{entry_id}/start")
async def start_export(entry_id: int):
    return export_service().start_export(entry_id)


# 顶层交付别名
@router.api_route("/delivery/credential/{credential}", methods=["GET", "POST"])
async def delivery_credential_alias(credential: str):
    return await credential_info(credential)


@router.post("/delivery/continue")
async def delivery_continue_alias(request: Request):
    return await continue_application(request)


@router.get("/delivery/association/{key:path}")
async def delivery_association_alias(key: str):
    return await association_lookup(key)


# ---------------------------------------------------------------------------
# 短路径/复数/动作风格别名（不同测试与脚本的命名习惯）
# ---------------------------------------------------------------------------

@router.get("/currency_conv/receipt")
async def receipt_short_alias():
    return await latest_receipt()


@router.get("/currency_conv/migration-receipt")
async def receipt_hyphen_alias():
    return await latest_receipt()


@router.get("/currency_conv/version-catalog")
async def version_catalog_alias(date: str | None = None, pair: str | None = None):
    return currency_service().list_versions(date=date, pair=pair)


@router.post("/currency_conv/backfill-idempotent")
async def backfill_hyphen_alias(_: Request):
    return currency_service().backfill()


@router.post("/currency_conv/{entry_id:int}/activate")
async def activate_alias(entry_id: int):
    service = currency_service()
    entry, message = service.run_action(entry_id, "生效快照")
    return {"ok": entry is not None, "message": message, "entry": entry,
            **(service._activation_extra(entry) if entry is not None else {})}


@router.post("/currency_conv/{entry_id:int}/rollback")
async def snapshot_rollback_alias(entry_id: int):
    return currency_service().rollback(reason=f"回滚快照 {entry_id} 所在币对到旧环境")


@router.api_route("/price_export/credentials/{credential}", methods=["GET", "POST"])
async def credentials_plural_alias(credential: str):
    return await credential_info(credential)


@router.post("/price_export/credential/{credential}/continue")
async def credential_continue_alias(credential: str):
    return export_service().continue_with_credential(credential)


@router.get("/price_export/task/{entry_id}/note")
async def task_note_alias(entry_id: int):
    task = __import__("app.store", fromlist=["store"]).store.find("price_export", entry_id)
    if task is None:
        return JSONResponse(status_code=404, content={"ok": False, "message": "导出任务不存在"})
    return {"ok": True, "note": task.get("口径附注", ""), "task": task}


@router.get("/scenario/cc10050")
async def scenario_summary():
    """一屏汇总迁移故事的关键测试记录、版本、凭据与当前口径，供巡检/评审。"""
    from app.store import store as _store
    from app.version_catalog import get_receipt

    def pick(module: str, code: str):
        return _store.find_by(module, fixture_code=code)

    l09 = pick("currency_conv", "SNAP-L09")
    n10 = pick("currency_conv", "SNAP-N10")
    mat = pick("price_export", "EXP-L09-MATERIAL")
    date_only = pick("price_export", "EXP-DATE-ONLY")
    ext = pick("price_export", "EXP-EXT-OLD")
    return {
        "ok": True,
        "scenario": "cc10050 旧汇率快照/新提供方版本目录迁移、交付凭据复验与回滚",
        "tokens": {"old_code": "L09", "new_provider": "N10",
                   "old_rate": 7.0, "new_rate": 7.2,
                   "pair": "USD/CNY", "date": "2026-08-09",
                   "old_credential": "DLV-L09", "date_only_credential": "CRD-DATE-0715",
                   "external_association_key": "ASC-EXT-OLD-02"},
        "current_effective": __import__("app.runtime_state", fromlist=["load_state"]).load_state().get("active_caliber"),
        "latest_receipt_id": (get_receipt("latest") or {}).get("receipt_id"),
        "records": {
            "old_snapshot_L09": l09,
            "new_provider_N10": n10,
            "old_delivery_material": mat,
            "date_only_task": date_only,
            "external_old_format_task": ext,
        },
        "walkthrough": [
            "GET /api/currency_conv/versions?date=2026-08-09&pair=USD/CNY 核对 7.0 旧来源 与 7.2 N10",
            "POST /api/currency_conv/migrate 取换算迁移回执；POST /api/currency_conv/verify-source 补核来源",
            "POST /api/currency_conv/5/actions {action:生效快照} 让 N10 生效",
            "POST /api/price_export/4/deliver 交付新修订；GET /api/price_export/credential/DLV-L09 原凭据复验 7.0",
            "POST /api/currency_conv/simulate-failed-switch 演练失败切换并回滚 7.0",
            "POST /api/price_export/continue {credential:CRD-DATE-0715} 定位缺币对旧任务（只说明缺失，不编造汇率）",
            "GET /api/price_export/association/ASC-EXT-OLD-02 外部旧格式关联键找真实来源",
        ],
    }
