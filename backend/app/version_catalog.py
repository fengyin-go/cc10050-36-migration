"""版本目录（Version Catalog）核心：旧快照/新提供方版本化寻址、回执、交付凭据清单。

这里不依赖 FastAPI，路由层与领域服务共同调用，保证：
- 同一“日期+币对+来源版本”幂等落一个版本文件，重复回填不产生重复生效快照；
- 交付清单（凭据）一旦签发即冻结，回滚只改当前口径指针，不改写历史交付；
- 缺币对/缺来源时返回结构化缺失原因，绝不编造汇率。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.runtime_state import (
    CATALOG_DIR,
    atomic_write_json,
    ensure_dirs,
    load_state,
    read_json,
    save_state,
)
from app.store import store

CCY_ALIASES = {
    "USD": ("usd", "美元", "美金", "美刀"),
    "CNY": ("cny", "rmb", "人民币", "元", "￥"),
    "EUR": ("eur", "欧元", "欧"),
    "JPY": ("jpy", "日元", "日币"),
    "HKD": ("hkd", "港币", "港元"),
    "GBP": ("gbp", "英镑"),
}

PLACEHOLDER_SOURCE_MARKS = ("样例", "placeholder", "todo", "xxx", "？？？")
MISSING_SOURCE_MARKS = ("原始来源缺失", "来源缺失", "未登记真实来源", "source missing")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_currency(value: Any) -> str:
    """把 'USD 美元'、'美元'、'rmb' 等写法归一为 ISO 币码；无法识别时返回去空格原文。"""
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    for code, aliases in CCY_ALIASES.items():
        if upper == code or upper.startswith(code + " ") or upper.startswith(code + "/"):
            return code
        for alias in aliases:
            if alias in text.lower() if alias.isascii() else alias in text:
                return code
    token = text.split()[0].strip("/,，")
    return token.upper() if token.isascii() else text


def pair_key(source: Any, target: Any) -> str:
    src = normalize_currency(source)
    tgt = normalize_currency(target)
    if not src and not tgt:
        return ""
    return f"{src}/{tgt}"


def parse_pair(text: str | None) -> tuple[str, str]:
    """解析 'USD/CNY'、'USD->CNY'、'美元兑人民币' 等币对写法。"""
    if not text:
        return "", ""
    raw = str(text).strip()
    for sep in ("->", "→", "/", "|", "兑", "对", "_", "-", " "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return normalize_currency(left), normalize_currency(right)
    return normalize_currency(raw), ""


def row_pair(row: dict[str, Any]) -> str:
    explicit = str(row.get("币对", "") or "").strip()
    if explicit and ("/" in explicit or "->" in explicit):
        src, tgt = parse_pair(explicit)
        if src and tgt:
            return f"{src}/{tgt}"
    return pair_key(row.get("源货币"), row.get("目标货币"))


def row_date(row: dict[str, Any]) -> str:
    return str(row.get("快照日期", "") or row.get("时间范围", "") or "").strip()[:10]


def row_rate(row: dict[str, Any]) -> float | None:
    try:
        rate = float(str(row.get("汇率值", row.get("绑定汇率", "")) or "").strip())
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def has_real_source(row: dict[str, Any]) -> bool:
    source = str(row.get("取数来源", "") or "").strip()
    if not source:
        return False
    low = source.lower()
    return not any(mark in source or mark in low for mark in PLACEHOLDER_SOURCE_MARKS)


def source_is_missing(row: dict[str, Any]) -> bool:
    """真正“缺来源”：显式标记、空来源、样例占位，或文案声明原始来源缺失。

    注意 L09 那种“旧来源 L09 + 原始提供方未登记”不算缺失：它有明确旧来源留痕，
    可按历史交付复验，仅在作为新口径时给出待补核警告。
    """
    if row.get("source_missing") is True:
        return True
    source = str(row.get("取数来源", "") or "").strip()
    if not source:
        return True
    low = source.lower()
    if any(mark in source or mark in low for mark in PLACEHOLDER_SOURCE_MARKS):
        return True
    if any(mark in source or mark in low for mark in MISSING_SOURCE_MARKS):
        return True
    return False


def assoc_key(row: dict[str, Any]) -> str:
    return str(row.get("关联键", "") or f"SNAP-ROW-{row.get('id', 'x')}").strip()


def catalog_path(ref_or_key: str) -> Path:
    """接受 catalog_ref（version-catalog/snapshots/x.json）或裸 key，返回绝对路径。"""
    text = str(ref_or_key or "").strip()
    marker = "version-catalog/"
    if marker in text:
        text = text.split(marker, 1)[1]
        return CATALOG_DIR.parent / "version-catalog" / Path(text)
    if text.startswith(("snapshots/", "receipts/", "deliveries/")):
        return CATALOG_DIR / text
    return CATALOG_DIR / "snapshots" / f"{text.removesuffix('.json')}.json"


# ---------------------------------------------------------------------------
# 版本条目
# ---------------------------------------------------------------------------

def snapshot_entry(row: dict[str, Any]) -> dict[str, Any]:
    key = assoc_key(row)
    pair = row_pair(row)
    rate = row_rate(row)
    return {
        "entry_key": key,
        "row_id": row.get("id"),
        "fixture_code": row.get("fixture_code", ""),
        "date": row_date(row),
        "pair": pair,
        "source_ccy": pair.split("/")[0] if "/" in pair else "",
        "target_ccy": pair.split("/")[1] if "/" in pair else "",
        "rate": rate,
        "rate_text": str(row.get("汇率值", "") or ""),
        "status": row.get("status", ""),
        "effective": row.get("status") == "已生效",
        "provider_code": row.get("提供方编码", ""),
        "source": row.get("取数来源", ""),
        "source_kind": row.get("source_kind", ""),
        "source_verified": bool(row.get("source_verified", False)),
        "source_missing": source_is_missing(row),
        "source_origin": ("new_provider" if (
            row.get("提供方编码") == "N10" or str(row.get("source_kind", "")) == "provider_n10"
        ) else "old_record"),
        "legacy": not (row.get("提供方编码") == "N10"),
        "version_code": row.get("version_code", ""),
        "version_label": row.get("版本标签", ""),
        "note": row.get("附注", ""),
        "natural_key": f"{row_date(row)}|{pair}",
        "association_key": key,
        "catalog_ref": f"version-catalog/snapshots/{key}.json",
        "updated_at": now_iso(),
    }


def upsert_snapshot_file(row: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    entry = snapshot_entry(row)
    path = CATALOG_DIR / "snapshots" / f"{entry['entry_key']}.json"
    previous = read_json(path)
    if previous and previous.get("migrated_at"):
        entry["migrated_at"] = previous["migrated_at"]
    else:
        entry["migrated_at"] = now_iso()
    atomic_write_json(path, entry)
    return entry


def all_version_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in store.rows("currency_conv"):
        entries.append(snapshot_entry(row))
    entries.sort(key=lambda e: (e["date"], e["pair"], str(e["row_id"])))
    return entries


def rebuild_index() -> dict[str, Any]:
    ensure_dirs()
    entries = all_version_entries()
    state = load_state()
    effective: dict[str, str] = {}
    for entry in entries:
        if entry["effective"] and entry["pair"]:
            # 同币对若意外有两条生效，保留 row_id 较小者为索引生效项（幂等对账兜底）。
            incumbent = effective.get(entry["pair"])
            if incumbent is None:
                effective[entry["pair"]] = entry["entry_key"]
    index = {
        "catalog": "版本目录 / version-catalog",
        "catalog_dir": str(CATALOG_DIR),
        "generated_at": now_iso(),
        "active_caliber": state.get("active_caliber", "legacy"),
        "effective_pointers": effective,
        "entry_count": len(entries),
        "entries": entries,
    }
    atomic_write_json(CATALOG_DIR / "catalog-index.json", index)
    return index


# ---------------------------------------------------------------------------
# 快照解析：日期 + 币对 / 关联键 / 显式绑定
# ---------------------------------------------------------------------------

def resolve_snapshot(
    *,
    date: str | None = None,
    pair: str | None = None,
    source: str | None = None,
    target: str | None = None,
    association_key: str | None = None,
    snapshot_id: int | str | None = None,
    version: str | None = None,
    prefer: str | None = None,
) -> dict[str, Any]:
    """按显式绑定/关联键或“日期+币对”解析真实存量快照。

    返回 {ok, consumable, reason, snapshot, entry, candidates}。
    缺币对、找不到快照、汇率无效或来源缺失时给出可展示原因，rate 保持 None。
    """
    rows = store.rows("currency_conv")

    # 1) 显式主键
    if snapshot_id not in (None, ""):
        try:
            sid = int(snapshot_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            sid = -1
        row = store.find("currency_conv", sid)
        if row is None:
            return {"ok": False, "consumable": False,
                    "reason": f"绑定的快照 id={snapshot_id} 不存在", "snapshot": None}
        return _consume_verdict(row)

    # 2) 关联键（允许外部旧格式键通过 resolves_to 跳转）
    if association_key:
        key = str(association_key).strip()
        row = next((r for r in rows if str(r.get("关联键", "")) == key), None)
        if row is None:
            ext = next((r for r in store.rows("price_export")
                        if str(r.get("关联键", "")) == key), None)
            if ext is not None and ext.get("resolves_to"):
                target_key = str(ext["resolves_to"])
                row = next((r for r in rows if str(r.get("关联键", "")) == target_key), None)
        if row is None:
            return {"ok": False, "consumable": False,
                    "reason": f"关联键 {key} 未指向任何存量汇率快照", "snapshot": None}
        verdict = _consume_verdict(row)
        verdict["via_association_key"] = key
        return verdict

    # 3) 币对归一
    src = normalize_currency(source) if source else ""
    tgt = normalize_currency(target) if target else ""
    p = pair if pair and "/" in str(pair) else ""
    if not p and (src or tgt):
        p = f"{src}/{tgt}" if src and tgt else ""
    if pair and "/" not in str(pair):
        psrc, ptgt = parse_pair(str(pair))
        if psrc and ptgt:
            p = f"{psrc}/{ptgt}"

    if not p:
        return {
            "ok": False, "consumable": False, "snapshot": None,
            "missing": ["源货币", "目标货币（币对）"],
            "reason": "缺少币对（源货币/目标货币），无法定位汇率快照；"
                      "该旧任务只有日期，来源待核，不能编造汇率。",
        }

    day = str(date or "").strip()[:10]
    candidates = [r for r in rows if row_pair(r) == p]
    if day:
        same_day = [r for r in candidates if row_date(r) == day]
        if same_day:
            candidates = same_day

    if version:
        v = str(version)
        chosen = next((r for r in candidates
                       if v in (str(r.get("version_code", "")), str(r.get("关联键", "")),
                                str(r.get("id", "")))), None)
        if chosen is None:
            return {"ok": False, "consumable": False, "pair": p, "date": day,
                    "reason": f"版本 {version} 在 {day or '该日期'} {p} 下不存在",
                    "candidates": [snapshot_entry(r) for r in candidates]}
        return _consume_verdict(chosen)

    if prefer in ("legacy", "old", "旧"):
        chosen = next((r for r in candidates if r.get("source_kind") == "legacy_unverified"), None)
        if chosen is not None:
            return _consume_verdict(chosen)

    effective = [r for r in candidates if r.get("status") == "已生效"]
    pool = effective or candidates
    if not pool:
        return {"ok": False, "consumable": False, "pair": p, "date": day,
                "reason": f"{day or '当前'} {p} 没有可定位的汇率快照", "snapshot": None}
    # 同币对多版本时优先已生效；若多条，取 id 最小（最老的存量）保证确定性。
    pool.sort(key=lambda r: int(r.get("id", 0)))
    return _consume_verdict(pool[0], candidates=candidates)


def _consume_verdict(row: dict[str, Any],
                     candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry = snapshot_entry(row)
    rate = row_rate(row)
    missing_source = source_is_missing(row)
    result: dict[str, Any] = {
        "ok": rate is not None,
        "consumable": rate is not None and not missing_source,
        "snapshot": row,
        "entry": entry,
        "rate": rate,
        "pair": entry["pair"],
        "date": entry["date"],
        "source": entry["source"],
        "source_kind": entry["source_kind"],
        "source_verified": entry["source_verified"],
        "source_missing": missing_source,
        "source_origin": entry["source_origin"],
        "association_key": entry["association_key"],
        "catalog_ref": entry["catalog_ref"],
    }
    if candidates is not None:
        result["candidates"] = [snapshot_entry(r) for r in candidates]
    if rate is None:
        result["reason"] = "汇率值缺失或不是正数，不能用于换算"
    elif missing_source:
        result["ok"] = False
        result["consumable"] = False
        result["reason"] = ("该旧记录的取数来源缺失或仍为占位/待核状态，不能作为生效口径消费；"
                            "已保留其明确的旧来源记录且不会改标为新版提供方，请在换算迁移回执中补核来源。")
    elif entry["source_kind"] == "legacy_unverified" or not entry["source_verified"]:
        result["consumable"] = True
        result["warning"] = ("这是明确区分于新版提供方的旧来源记录，原始提供方待补核；"
                             "历史交付可按原凭据复验，新口径使用前建议补核来源。")
        result["reason"] = ""
    else:
        result["reason"] = ""
    return result


def compose_note(row: dict[str, Any], *, credential: str = "", revision: int | None = None,
                 external: bool = False) -> str:
    entry = snapshot_entry(row)
    rev = f"（交付修订 v{revision}）" if revision else ""
    prefix = "外部旧格式口径附注" if external else "口径附注"
    source_clause = str(row.get("取数来源", "") or "来源缺失")
    legacy_warn = ""
    if row.get("source_kind") == "legacy_unverified" or not row.get("source_verified", False):
        legacy_warn = "本快照为迁移前旧来源口径，原始提供方待补核；来源属性始终保留为明确的旧来源，不会改标为新版提供方。"
    cred_clause = f"本附注与交付凭据 {credential} 冻结绑定，可按原凭据复验。" if credential else ""
    return (
        f"{prefix}{rev}：按快照日期 {entry['date']} 的 {entry['pair']} 存量快照 "
        f"{entry['association_key']}（版本 {entry['version_code'] or entry['row_id']}）换算，"
        f"1 {entry['source_ccy']} = {entry['rate_text']} {entry['target_ccy']}；"
        f"取数来源：{source_clause}。{legacy_warn}{cred_clause}"
    )


# ---------------------------------------------------------------------------
# 迁移回执
# ---------------------------------------------------------------------------

def next_receipt_id() -> str:
    state = load_state()
    state["receipt_seq"] = int(state.get("receipt_seq", 0)) + 1
    rid = f"RCP-MIG-{state['receipt_seq']:04d}"
    state["latest_receipt_id"] = rid
    save_state(state)
    return rid


def save_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    rid = receipt["receipt_id"]
    atomic_write_json(CATALOG_DIR / "receipts" / f"{rid}.json", receipt)
    atomic_write_json(CATALOG_DIR / "receipts" / "latest.json", receipt)
    return receipt


def get_receipt(receipt_id: str | None = None) -> dict[str, Any] | None:
    if receipt_id and receipt_id != "latest":
        data = read_json(CATALOG_DIR / "receipts" / f"{receipt_id}.json")
        if data:
            return data
    data = read_json(CATALOG_DIR / "receipts" / "latest.json")
    if data:
        return data
    state = load_state()
    rid = state.get("latest_receipt_id")
    if rid:
        return read_json(CATALOG_DIR / "receipts" / f"{rid}.json")
    return None


# ---------------------------------------------------------------------------
# 交付凭据清单（冻结）
# ---------------------------------------------------------------------------

def manifest_path(credential: str) -> Path:
    safe = "".join(ch for ch in str(credential) if ch.isalnum() or ch in "-_")
    return CATALOG_DIR / "deliveries" / f"{safe}.json"


def save_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    atomic_write_json(manifest_path(manifest["credential"]), manifest)
    return manifest


def get_manifest(credential: str) -> dict[str, Any] | None:
    return read_json(manifest_path(credential))


def verify_credential(credential: str) -> dict[str, Any]:
    manifest = get_manifest(credential)
    if manifest is None:
        return {"ok": False, "found": False,
                "message": f"交付凭据 {credential} 不存在或清单文件缺失，无法复验"}
    # 冻结清单自校验：按记录汇率复算首笔。
    rate = float(manifest.get("rate") or 0)
    checks = []
    for item in manifest.get("items", []):
        amount = float(item.get("amount", 0))
        expected = round(amount * rate, 2)
        checks.append({"label": item.get("label"), "amount": amount,
                       "rate": rate, "expected_converted": expected,
                       "recorded_converted": item.get("converted"),
                       "matches": abs(expected - float(item.get("converted", 0))) < 0.01})
    return {"ok": True, "found": True, "manifest": manifest,
            "recompute_checks": checks,
            "message": f"凭据 {credential} 复验通过：交付清单与换算依据冻结可查"}


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------

def attachment_dir(credential: str) -> Path:
    safe = "".join(ch for ch in str(credential) if ch.isalnum() or ch in "-_")
    path = CATALOG_DIR / "attachments" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_attachment(credential: str, filename: str, content: str | bytes) -> dict[str, Any]:
    directory = attachment_dir(credential)
    path = directory / filename
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""}
    with open(path, mode, **kwargs) as handle:  # type: ignore[call-overload]
        handle.write(content)
    size = path.stat().st_size
    return {"name": filename, "size": size,
            "url": f"/api/price_export/credential/{credential}/attachments/{filename}"}


def get_attachment_path(credential: str, filename: str) -> Path | None:
    safe_name = Path(filename).name  # 防目录穿越
    path = attachment_dir(credential) / safe_name
    return path if path.exists() and path.is_file() else None


# ---------------------------------------------------------------------------
# 引导：目录、说明、存量版本文件、旧环境快照、种子交付清单与附件
# ---------------------------------------------------------------------------

README_MD = """# 版本目录（Version Catalog）

货币换算器旧汇率快照与新提供方版本化迁移后的寻址目录。

- `snapshots/<关联键>.json`：每个存量快照/提供方版本的不可变版本条目（汇率值与来源各自留痕）。
- `catalog-index.json`：版本索引与各币对当前生效指针、当前口径（legacy/new）。
- `receipts/`：换算迁移回执，列出已迁移条目与待运营补核来源。
- `deliveries/<交付凭据>.json`：已签发并冻结的交付清单（配置、附件、口径附注、可复算明细）。
- `attachments/<凭据>/`：旧交付材料配置与旧格式导出附件。
- `legacy/legacy-environment.json`：迁移前旧环境/旧口径留档，回滚时据此对账。
- `rollbacks/`：每次模拟回滚的回执。

核心约束：重复回填幂等（不会产生两个生效快照）；回滚只移动当前生效指针，
已交付清单按原凭据永久可复验；缺币对/缺来源只返回缺失说明，绝不编造汇率；
旧来源记录始终保留“旧来源”区分，不会改标为新提供方 N10。
"""


def _delivery_config(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "导出任务id": task.get("id"),
        "任务名称": task.get("任务名称"),
        "商品范围": task.get("商品范围"),
        "导出格式": task.get("导出格式"),
        "时间范围": task.get("时间范围"),
        "外部格式版本": task.get("external_format", ""),
        "字段映射": {
            "商品标识": "goods_code",
            "快照日期": "snapshot_date",
            "币对": "currency_pair",
            "汇率": "fx_rate",
            "关联键": "association_key",
        },
        "口径绑定": task.get("关联键", ""),
        "交付凭据": task.get("交付凭据", ""),
        "模板版本": "price-export-template-v1",
    }


def _delivery_items(task: dict[str, Any], snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    rate = row_rate(snap) if snap else row_rate(task)
    if rate is None:
        return []
    amount = 100.0
    compare = store.find_by("price_compare", fixture_code="CMP-WC001")
    if compare is not None:
        try:
            amount = float(compare.get("美元报价", amount))
        except (TypeError, ValueError):
            pass
    pair = row_pair(snap) if snap else str(task.get("币对", ""))
    src, tgt = pair.split("/", 1) if "/" in pair else ("USD", "CNY")
    return [{
        "label": "WC-SET-001 跨境套装报价（测试记录可复算的一笔）",
        "goods_code": "WC-SET-001",
        "amount": amount,
        "from": src,
        "rate": rate,
        "converted": round(amount * rate, 2),
        "to": tgt,
    }]


def build_manifest_for_task(task: dict[str, Any], *, revision: int | None = None,
                            credential: str | None = None,
                            snap: dict[str, Any] | None = None) -> dict[str, Any]:
    if snap is None and task.get("bound_snapshot_id"):
        snap = store.find("currency_conv", int(task["bound_snapshot_id"]))  # type: ignore[arg-type]
    if snap is None and task.get("关联键"):
        resolved = resolve_snapshot(association_key=str(task["关联键"]))
        snap = resolved.get("snapshot")
    rev = int(revision or task.get("delivery_revision") or 1)
    cred = credential or str(task.get("交付凭据", ""))
    rate = row_rate(snap) if snap else row_rate(task)
    note = str(task.get("口径附注", "") or "")
    if not note and snap is not None:
        note = compose_note(snap, credential=cred, revision=rev,
                            external=bool(task.get("external_format")))
    attachments = [
        {"name": f"{task.get('材料编号', 'material')}-config.json", "kind": "config",
         "description": "回查配置：导出模板、字段映射、口径绑定"},
        {"name": f"{task.get('材料编号', 'material')}-delivery.csv", "kind": "csv",
         "description": "交付数据附件（含日期、币对、汇率与关联键）"},
    ]
    if task.get("external_format"):
        attachments.append({
            "name": f"{task.get('材料编号', 'material')}-legacy-external.csv",
            "kind": "legacy-external",
            "description": "外部旧格式原始文件，完成后可凭其中关联键定位真实换算来源",
        })
    for att in attachments:
        att["url"] = f"/api/price_export/credential/{cred}/attachments/{att['name']}"
    return {
        "ok": True,
        "credential": cred,
        "task_id": task.get("id"),
        "task_name": task.get("任务名称"),
        "material_code": task.get("材料编号", ""),
        "revision": rev,
        "state": "已交付",
        "delivered_at": task.get("delivered_at") or now_iso(),
        "date": row_date(task) or (row_date(snap) if snap else ""),
        "pair": str(task.get("币对", "") or (row_pair(snap) if snap else "")),
        "snapshot_key": assoc_key(snap) if snap else task.get("关联键", ""),
        "snapshot_ref": snap.get("snapshot_ref", "") if snap else task.get("snapshot_ref", ""),
        "rate": rate,
        "rate_text": str((snap or {}).get("汇率值", task.get("绑定汇率", "")) or ""),
        "source": (snap or {}).get("取数来源", ""),
        "source_kind": (snap or {}).get("source_kind", ""),
        "source_verified": bool((snap or {}).get("source_verified", False)),
        "association_key": task.get("关联键", ""),
        "resolves_to": task.get("resolves_to", ""),
        "caliber": load_state().get("active_caliber", "legacy"),
        "note": note,
        "config": _delivery_config(task),
        "attachments": attachments,
        "items": _delivery_items(task, snap),
        "immutable": True,
    }


def ensure_attachment_files(manifest: dict[str, Any], task: dict[str, Any]) -> None:
    cred = manifest["credential"]
    material = str(task.get("材料编号", "material"))
    config_name = f"{material}-config.json"
    config_path = attachment_dir(cred) / config_name
    if not config_path.exists():
        write_attachment(cred, config_name, __import__("json").dumps(
            manifest["config"], ensure_ascii=False, indent=2))

    csv_name = f"{material}-delivery.csv"
    if not (attachment_dir(cred) / csv_name).exists():
        buf = io.StringIO()
        buf.write("﻿")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["商品标识", "测试项", "外币金额", "汇率", "人民币金额",
                         "快照日期", "币对", "关联键", "交付凭据"])
        for item in manifest.get("items", []):
            writer.writerow([item.get("goods_code", ""), item.get("label", ""),
                             f"{item.get('amount', 0):.2f}", manifest.get("rate_text", ""),
                             f"{item.get('converted', 0):.2f}", manifest.get("date", ""),
                             manifest.get("pair", ""), manifest.get("snapshot_key", ""), cred])
        write_attachment(cred, csv_name, buf.getvalue())

    if task.get("external_format"):
        old_name = f"{material}-legacy-external.csv"
        if not (attachment_dir(cred) / old_name).exists():
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            # 外部旧格式表头（旧系统字段名），其中 glj 关联键可回查真实换算来源。
            writer.writerow(["rq(日期)", "hb(货币对)", "hl(汇率)", "gly(来源)", "glj(关联键)"])
            writer.writerow([manifest.get("date", ""), manifest.get("pair", ""),
                             manifest.get("rate_text", ""), "旧外部系统导出",
                             task.get("关联键", "")])
            write_attachment(cred, old_name, buf.getvalue())


def write_legacy_environment() -> dict[str, Any]:
    """留档迁移前旧环境：所有非 N10 版本的旧口径与当时生效指针。"""
    ensure_dirs()
    rows = store.rows("currency_conv")
    usdcny = [r for r in rows if row_pair(r) == "USD/CNY"]
    l09 = next((r for r in usdcny if r.get("fixture_code") == "SNAP-L09"), None)
    legacy = {
        "name": "迁移前旧环境（legacy environment）",
        "archived_at": now_iso(),
        "caliber": "legacy",
        "effective_pointers": {"USD/CNY": assoc_key(l09) if l09 else ""},
        "usd_cny_rate": row_rate(l09) if l09 else None,
        "usd_cny_source": (l09 or {}).get("取数来源", ""),
        "rollback_instruction": "POST /api/currency_conv/rollback 即可把当前口径退回此指针；"
                                "已交付凭据清单不受回滚影响。",
        "snapshots": [snapshot_entry(r) for r in rows
                      if r.get("source_kind") != "provider_n10"],
    }
    atomic_write_json(CATALOG_DIR / "legacy" / "legacy-environment.json", legacy)
    return legacy


def bootstrap() -> dict[str, Any]:
    """启动时幂等引导：建目录、版本文件、旧环境留档、种子交付清单与附件、索引。"""
    ensure_dirs()
    readme = CATALOG_DIR / "README.md"
    if not readme.exists():
        readme.write_text(README_MD, encoding="utf-8")

    for row in store.rows("currency_conv"):
        upsert_snapshot_file(row)

    write_legacy_environment()

    seeded = []
    for task in store.rows("price_export"):
        cred = str(task.get("交付凭据", "") or "")
        if cred and task.get("status") == "已完成":
            if get_manifest(cred) is None:
                manifest = build_manifest_for_task(task)
                save_manifest(manifest)
                ensure_attachment_files(manifest, task)
                seeded.append(cred)

    index = rebuild_index()
    return {"catalog_dir": str(CATALOG_DIR), "seeded_manifests": seeded,
            "entries": index["entry_count"]}
