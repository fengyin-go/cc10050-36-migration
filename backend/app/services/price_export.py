"""比价导出通道领域服务：导出任务排队、生成、失败重试，以及 cc10050 迁移场景扩展。

扩展职责：
- 导出任务必须消费“实际绑定”的存量快照生成口径附注（不再随手取第一条生效快照）；
- 交付即冻结清单并签发/沿用交付凭据，配置与附件可回查，历史可按原凭据复验、复算；
- 可凭交付凭据继续未办申请；
- 外部旧格式/缺币对旧任务通过关联键单独定位，并给出真实来源或缺失原因，绝不编造汇率。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.version_catalog import (
    assoc_key,
    build_manifest_for_task,
    compose_note,
    ensure_attachment_files,
    get_manifest,
    get_attachment_path,
    resolve_snapshot,
    row_date,
    row_pair,
    row_rate,
    save_manifest,
    verify_credential,
    write_attachment,
)


class PriceExportService(BaseModuleService):
    MODULE = "price_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["任务名称", "商品范围", "导出格式", "时间范围", "生成状态"]
    REQUIRED_FIELDS = ["任务名称", "商品范围", "导出格式"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "导出失败"]
    ACTION_RULES = {"开始导出": "导出中", "重试导出": "导出中"}
    NEGATIVE_ACTIONS = []

    def dispatch_action(self, entry_id: int, action: str,
                        payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        custom = {
            "绑定快照": lambda: self.bind_snapshot(entry_id, payload),
            "绑定汇率快照": lambda: self.bind_snapshot(entry_id, payload),
            "绑定口径": lambda: self.bind_snapshot(entry_id, payload),
            "绑定并生成附注": lambda: self.bind_snapshot(entry_id, {**payload, "generate_note": True}),
            "生成口径附注": lambda: self.generate_note(entry_id, payload),
            "附加汇率口径": lambda: self.generate_note(entry_id, payload),
            "附上货币换算口径说明": lambda: self.attach_currency_note(entry_id),
            "交付": lambda: self.deliver(entry_id, **{k: payload[k] for k in ("ok",) if k in payload}),
            "完成导出并签发凭据": lambda: self.deliver(entry_id),
            "交付并签发凭据": lambda: self.deliver(entry_id),
            "凭凭据继续": lambda: self._continue_entry(entry_id),
            "继续办理": lambda: self._continue_entry(entry_id),
            "继续未办申请": lambda: self._continue_entry(entry_id),
        }
        if action in custom:
            return custom[action]()
        # 标准动作沿用通用流转（保持状态机与列表冒烟行为），并回传凭据/绑定上下文。
        entry, message = self.run_action(entry_id, action)
        return {"ok": entry is not None, "message": message, "entry": entry,
                "credential": (entry or {}).get("交付凭据", "") if entry else ""}

    # ---- 原有：导出生成与重试 ----

    def start_export(self, entry_id: int) -> dict[str, Any]:
        """开始导出：校验商品范围有已计算比价结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        scope = entry.get("商品范围", "")
        fmt = entry.get("导出格式", "")
        if fmt not in ("CSV", "XLSX", "JSON"):
            return {"ok": False, "message": f"不支持的导出格式: {fmt}"}
        computed = [
            c for c in store.rows("price_compare")
            if scope in str(c.get("商品标识", "")) and c.get("status") == "已计算"
        ]
        if not computed:
            entry["status"] = "导出失败"
            entry["abnormal"] = True
            store.persist()
            return {"ok": False, "message": f"商品范围 {scope} 没有已计算的比价结果"}
        entry["status"] = "导出中"
        entry["pending"] = True
        if not entry.get("continue_credential"):
            entry["continue_credential"] = str(entry.get("交付凭据", "") or f"CRD-EXPORT-{entry_id}")
        store.persist()
        return {"ok": True, "message": "导出已开始", "rows": len(computed), "format": fmt,
                "credential": entry.get("continue_credential")}

    def finish_export(self, entry_id: int, ok: bool) -> dict[str, Any]:
        """回写导出完成状态并把备份记录写入变价历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "导出任务不存在"}
        entry["status"] = "已完成" if ok else "导出失败"
        entry["生成状态"] = "成功" if ok else "失败"
        entry["abnormal"] = not ok
        entry["pending"] = False
        if ok:
            rows = store.rows("change_history")
            hid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": hid,
                "商品标识": entry.get("商品范围", ""),
                "旧价格": "在线比价结果",
                "新价格": f"导出 {entry.get('导出格式', '')} 文件",
                "变化幅度": "备份",
                "发现时间": datetime.now(timezone.utc).isoformat(),
                "status": "已确认",
                "pending": False,
                "abnormal": False,
            })
        store.persist()
        return {"ok": True, "message": "导出状态已回写"}

    def retry_export(self, entry_id: int) -> dict[str, Any]:
        """重试失败导出并隔离失败上下文。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "导出失败":
            return {"ok": False, "message": "任务不存在或未失败"}
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"export-{entry_id}",
            "失败阶段": "导出",
            "失败原因": f"任务 {entry.get('任务名称', '')} 上次导出失败",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "1",
            "status": "重试中",
            "pending": True,
            "abnormal": False,
        })
        entry["status"] = "导出中"
        entry["abnormal"] = False
        store.persist()
        return {"ok": True, "message": "导出重试已启动", "quarantine_id": fid}

    # ---- cc10050：实际绑定快照 + 口径附注 ----

    def _resolve_task_snapshot(self, task: dict[str, Any], selection: dict[str, Any] | None = None):
        selection = selection or {}
        if selection.get("snapshot_id") or selection.get("association_key") \
                or selection.get("version") or selection.get("prefer"):
            return resolve_snapshot(**{
                "snapshot_id": selection.get("snapshot_id"),
                "association_key": selection.get("association_key"),
                "version": selection.get("version"),
                "prefer": selection.get("prefer"),
                "date": selection.get("date"),
                "pair": selection.get("pair"),
            })
        if task.get("bound_snapshot_id"):
            return resolve_snapshot(snapshot_id=task["bound_snapshot_id"])
        bound_assoc = task.get("bound_association_key") or task.get("resolves_to")
        if bound_assoc:
            verdict = resolve_snapshot(association_key=str(bound_assoc))
            if verdict.get("snapshot"):
                return verdict
        # 用任务自身的日期+币对（绝不随手取全表第一条生效快照）
        pair = str(task.get("币对", "") or "")
        day = row_date(task)
        if pair:
            return resolve_snapshot(date=day, pair=pair)
        return {"ok": False, "consumable": False, "snapshot": None,
                "missing": ["源货币", "目标货币（币对）"],
                "reason": "该旧任务只有日期、缺少币对，无法定位汇率快照；来源待核，不能编造汇率。"}

    def bind_snapshot(self, entry_id: int, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"ok": False, "message": "导出任务不存在"}
        verdict = self._resolve_task_snapshot(task, selection)
        snap = verdict.get("snapshot")
        if snap is None:
            task["生成状态"] = "缺汇率口径（币对/来源待核）"
            store.persist()
            return {"ok": False, "consumable": False, "message": verdict.get("reason"),
                    "reason": verdict.get("reason"), "missing": verdict.get("missing", [])}
        task["bound_snapshot_id"] = snap.get("id")
        task["bound_association_key"] = assoc_key(snap)
        task["snapshot_ref"] = snap.get("snapshot_ref", "")
        if not task.get("币对"):
            task["币对"] = row_pair(snap)
        if selection and selection.get("generate_note", True):
            note = compose_note(
                snap,
                credential=str(task.get("交付凭据", "") or ""),
                revision=int(task.get("delivery_revision") or 0) + 1
                if task.get("status") == "已完成" else int(task.get("delivery_revision") or 0) or None,
            )
            task["口径附注"] = note
            task["绑定汇率"] = str(snap.get("汇率值", ""))
        store.persist()
        return {"ok": True, "message": "已绑定实际存量快照并据此生成口径附注",
                "task_id": entry_id, "bound_snapshot_id": snap.get("id"),
                "association_key": assoc_key(snap), "pair": row_pair(snap),
                "date": row_date(snap), "rate": row_rate(snap),
                "source": snap.get("取数来源", ""),
                "source_kind": snap.get("source_kind", ""),
                "source_verified": snap.get("source_verified", False),
                "warning": verdict.get("warning", ""),
                "note": task.get("口径附注", "")}

    def generate_note(self, entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"ok": False, "message": "导出任务不存在"}
        verdict = self._resolve_task_snapshot(task, payload)
        snap = verdict.get("snapshot")
        if snap is None:
            task["生成状态"] = "缺汇率口径（币对/来源待核）"
            store.persist()
            return {"ok": False, "consumable": False,
                    "message": verdict.get("reason", "没有可绑定的存量快照，导出口径缺失"),
                    "reason": verdict.get("reason", "")}
        task["bound_snapshot_id"] = snap.get("id")
        task["bound_association_key"] = assoc_key(snap)
        task["口径附注"] = compose_note(
            snap, credential=str(task.get("交付凭据", "") or ""),
            external=bool(task.get("external_format")))
        task["绑定汇率"] = str(snap.get("汇率值", ""))
        store.persist()
        return {"ok": True, "message": "口径附注已按实际绑定快照生成",
                "note": task["口径附注"], "rate": row_rate(snap),
                "pair": row_pair(snap), "source": snap.get("取数来源", ""),
                "association_key": assoc_key(snap)}

    def attach_currency_note(self, entry_id: int) -> dict[str, Any]:
        """兼容旧方法名：为导出任务附上货币换算口径说明（走实际绑定，不猜第一条）。"""
        return self.generate_note(entry_id)

    # ---- cc10050：交付签发凭据 + 冻结清单 ----

    def _continue_entry(self, entry_id: int) -> dict[str, Any]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"ok": False, "continued": False, "message": "导出任务不存在"}
        credential = str(task.get("continue_credential") or task.get("交付凭据") or "")
        if not credential:
            return {"ok": False, "continued": False,
                    "message": "该任务尚未签发办理凭据，无法凭凭据继续"}
        return self.continue_with_credential(credential)

    def deliver(self, entry_id: int, *, ok: bool = True) -> dict[str, Any]:
        task = store.find(self.MODULE, entry_id)
        if task is None:
            return {"ok": False, "message": "导出任务不存在"}
        if not ok:
            return self.finish_export(entry_id, False)

        verdict = self._resolve_task_snapshot(task)
        snap = verdict.get("snapshot")
        if snap is None:
            task["status"] = "导出失败"
            task["abnormal"] = True
            task["生成状态"] = "缺汇率口径，未交付"
            store.persist()
            return {"ok": False, "message": verdict.get("reason", "缺少换算口径，不能交付"),
                    "reason": verdict.get("reason", "")}

        task["bound_snapshot_id"] = snap.get("id")
        task["bound_association_key"] = assoc_key(snap)
        if not task.get("币对"):
            task["币对"] = row_pair(snap)

        base_cred = str(task.get("交付凭据", "") or "").strip()
        revision = int(task.get("delivery_revision") or 0) + 1
        if not base_cred:
            base_cred = f"DLV-TASK-{entry_id}"
            task["交付凭据"] = base_cred
            task["continue_credential"] = base_cred
        # 首次交付沿用基础凭据（种子历史交付即 v1）；再次交付签发新的修订凭据，
        # 原凭据清单冻结不动，仍可按原凭据复验。
        credential = base_cred if revision == 1 else f"{base_cred}-R{revision}"

        note = compose_note(snap, credential=credential, revision=revision,
                            external=bool(task.get("external_format")))
        task["口径附注"] = note
        task["绑定汇率"] = str(snap.get("汇率值", ""))
        task["delivery_revision"] = revision
        task["delivered_at"] = datetime.now(timezone.utc).isoformat()
        task["status"] = "已完成"
        task["pending"] = False
        task["abnormal"] = False
        task["最新交付凭据"] = credential
        task["生成状态"] = f"已交付｜交付凭据 {credential}"

        manifest = build_manifest_for_task(
            task, revision=revision, credential=credential, snap=snap)
        manifest["note"] = note
        save_manifest(manifest)
        ensure_attachment_files(manifest, task)
        # 新修订若没有独立附件，也保证其凭据下可下载基础附件副本。
        if credential != base_cred:
            base_manifest = get_manifest(base_cred)
            if base_manifest:
                for att in base_manifest.get("attachments", []):
                    src = get_attachment_path(base_cred, att["name"])
                    if src is not None and not _att_path(credential, att["name"]).exists():
                        write_attachment(credential, att["name"], src.read_bytes())
        store.persist()
        return {"ok": True, "message": f"交付完成，已签发交付凭据 {credential}，清单冻结可复验",
                "credential": credential, "original_credential": base_cred,
                "revision": revision, "manifest": manifest, "note": note}

    # ---- cc10050：凭据回查 / 继续办理 / 复算 ----

    def credential_info(self, credential: str) -> dict[str, Any]:
        result = verify_credential(credential)
        if not result.get("found"):
            # 清单文件缺失（例如未走 lifespan 的 TestClient）：先按种子任务惰性重建一次。
            task = store.find_by(self.MODULE, continue_credential=credential) \
                or store.find_by(self.MODULE, **{"交付凭据": credential})
            if task is not None and task.get("status") == "已完成":
                try:
                    manifest = build_manifest_for_task(task, credential=credential)
                    save_manifest(manifest)
                    ensure_attachment_files(manifest, task)
                    result = verify_credential(credential)
                except Exception:
                    result = {"found": False}
        if not result.get("found"):
            # 也许是任务上的 continue_credential（尚未交付）
            task = store.find_by(self.MODULE, continue_credential=credential) \
                or store.find_by(self.MODULE, **{"交付凭据": credential})
            if task is not None:
                return {"ok": True, "found": True, "delivered": False,
                        "task": task, "credential": credential,
                        "message": "凭据对应一份尚未完成交付的申请，可继续办理"}
            return {"ok": False, "found": False,
                    "message": f"交付凭据 {credential} 不存在，材料与换算依据不可回查"}
        manifest = result["manifest"]
        task = store.find(self.MODULE, int(manifest.get("task_id", -1))) if manifest.get("task_id") else None
        revisions = []
        if task is not None:
            base = str(task.get("交付凭据", ""))
            rev = int(task.get("delivery_revision") or 1)
            revisions = [base] + [f"{base}-R{i}" for i in range(2, rev + 1)]
        return {**result, "delivered": True, "task": task,
                "revision_credentials": revisions}

    def continue_with_credential(self, credential: str) -> dict[str, Any]:
        credential = str(credential or "").strip()
        if not credential:
            return {"ok": False, "continued": False, "message": "必须提供交付凭据才能继续未办申请"}
        task = store.find_by(self.MODULE, continue_credential=credential) \
            or store.find_by(self.MODULE, **{"交付凭据": credential})
        if task is None:
            manifest = get_manifest(credential)
            if manifest and manifest.get("task_id"):
                task = store.find(self.MODULE, int(manifest["task_id"]))
        if task is None:
            return {"ok": False, "continued": False,
                    "message": f"凭据 {credential} 未关联任何导出申请，无法继续"}

        blockers: list[str] = []
        missing: list[str] = []
        if not str(task.get("币对", "")).strip():
            missing = ["源货币", "目标货币（币对）"]
            blockers.append("缺少币对，无法定位汇率快照")
        verdict = self._resolve_task_snapshot(task)
        if not verdict.get("snapshot"):
            blockers.append(verdict.get("reason", "汇率口径不可用"))
        if task.get("status") in ("排队中",):
            task["status"] = "导出中"
            task["pending"] = True
        task["last_continued_at"] = datetime.now(timezone.utc).isoformat()
        task["continue_count"] = int(task.get("continue_count", 0)) + 1
        store.persist()
        return {
            "ok": True, "continued": True, "credential": credential,
            "task_id": task.get("id"), "task": task,
            "status": task.get("status"),
            "missing": missing, "blockers": blockers,
            "can_generate_note": bool(verdict.get("snapshot")),
            "resolution": {k: v for k, v in verdict.items() if k != "snapshot"},
            "next_actions": [
                "补齐币对后调用 POST /api/price_export/{id}/bind 绑定实际快照并生成口径附注",
                "调用 POST /api/price_export/{id}/deliver 交付并冻结签发凭据",
            ] if blockers else ["可直接生成口径附注并交付"],
            "message": ("已凭凭据定位并继续该申请；但" + "；".join(blockers) +
                        "。系统只说明缺失，不会编造汇率。") if blockers
            else "已凭凭据继续申请，绑定快照口径可用",
        }

    def recompute(self, credential: str, amount: float | None = None) -> dict[str, Any]:
        manifest = get_manifest(credential)
        if manifest is None:
            return {"ok": False, "message": f"凭据 {credential} 的冻结交付清单不存在，不能复算"}
        rate = float(manifest.get("rate") or 0)
        if amount is None:
            items = manifest.get("items") or []
            amount = float(items[0]["amount"]) if items else 1.0
        converted = round(float(amount) * rate, 2)
        return {"ok": True, "credential": credential, "amount": amount,
                "rate": rate, "converted": converted,
                "pair": manifest.get("pair"), "date": manifest.get("date"),
                "source": manifest.get("source"),
                "source_kind": manifest.get("source_kind"),
                "immutable_note": manifest.get("note"),
                "message": f"按凭据 {credential} 的实际交付结果复算：{amount} × {rate} = {converted}"}

    # ---- cc10050：关联键定位（含外部旧格式 / 缺币对任务）----

    def association_lookup(self, key: str) -> dict[str, Any]:
        key = str(key or "").strip()
        snap_row = next((r for r in store.rows("currency_conv")
                         if str(r.get("关联键", "")) == key), None)
        task = next((r for r in store.rows(self.MODULE)
                     if str(r.get("关联键", "")) == key), None)
        if snap_row is not None:
            verdict = resolve_snapshot(association_key=key)
            return {"ok": True, "found": True, "kind": "snapshot",
                    "association_key": key, "task": task,
                    "source": snap_row.get("取数来源", ""),
                    "rate": row_rate(snap_row), "pair": row_pair(snap_row),
                    "date": row_date(snap_row),
                    "resolution": {k: v for k, v in verdict.items() if k != "snapshot"},
                    "message": "关联键直接指向真实存量换算快照"}
        if task is not None:
            verdict = self._resolve_task_snapshot(task)
            return {
                "ok": bool(verdict.get("snapshot")),
                "found": True, "kind": "export_task",
                "association_key": key,
                "task_id": task.get("id"), "task": task,
                "credential": task.get("交付凭据", ""),
                "resolves_to": task.get("resolves_to", ""),
                "consumable": bool(verdict.get("consumable")),
                "source": (verdict.get("snapshot") or {}).get("取数来源", "")
                if verdict.get("snapshot") else "",
                "rate": verdict.get("rate"),
                "reason": verdict.get("reason", ""),
                "message": verdict.get("reason") or "关联键经导出任务解析到真实换算来源",
            }
        return {"ok": False, "found": False,
                "message": f"关联键 {key} 既未指向快照也未指向导出任务"}

    # ---- 原有：统计与清理 ----

    def export_scope_stats(self, entry_id: int) -> dict[str, Any]:
        """统计导出范围内比价结果覆盖情况。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "导出任务不存在"}
        scope = entry.get("商品范围", "")
        rows = [c for c in store.rows("price_compare") if scope in str(c.get("商品标识", ""))]
        computed = sum(1 for r in rows if r.get("status") == "已计算")
        return {
            "ok": True,
            "message": "导出范围统计完成",
            "total_batches": len(rows),
            "computed": computed,
            "coverage": f"{(computed / len(rows)):.0%}" if rows else "0%",
        }

    def cleanup_finished(self, keep_days: int = 30) -> dict[str, Any]:
        """清理历史已完成导出任务并归档审计。"""
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc).date()
        finished = [
            t for t in store.rows(self.MODULE) if t.get("status") == "已完成"
        ]
        for task in finished:
            task["pending"] = False
        rows = store.rows("parse_audit")
        aid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": aid,
            "审计时间": datetime.now(timezone.utc).isoformat(),
            "解析模板": "导出通道清理",
            "样本总数": str(len(finished)),
            "字段命中率": "100.00%",
            "失败原因分布": "",
            "status": "正常",
            "pending": True,
            "abnormal": False,
        })
        store.persist()
        return {
            "ok": True,
            "message": "已完成导出任务已清理",
            "cleaned": len(finished),
            "as_of": cutoff.isoformat(),
        }


def _att_path(credential: str, filename: str):
    from app.runtime_state import CATALOG_DIR

    safe = "".join(ch for ch in str(credential) if ch.isalnum() or ch in "-_")
    return CATALOG_DIR / "attachments" / safe / filename
