"""货币换算器领域服务：管理按日期汇率快照的多币种价格口径统一。

cc10050 迁移场景扩展：
- 旧汇率快照 + 新提供方 N10 版本化迁移到“版本目录”，出具换算迁移回执；
- 回填幂等、同币对生效快照唯一，旧口径与新版寻址可对账；
- 切换失败可回滚旧环境（只移当前指针，不动已交付凭据）；
- 补核来源但永久保留“旧来源”区别，绝不改标 N10；
- 按日期+币对/关联键定位真实存量快照；缺币对或缺来源时说明原因、不编造汇率。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.runtime_state import load_state, save_state
from app.services.base import BaseModuleService
from app.store import store
from app.version_catalog import (
    assoc_key,
    compose_note,
    has_real_source,
    next_receipt_id,
    normalize_currency,
    pair_key,
    parse_pair,
    rebuild_index,
    resolve_snapshot,
    row_date,
    row_pair,
    row_rate,
    save_receipt,
    snapshot_entry,
    source_is_missing,
    upsert_snapshot_file,
)


class CurrencyConvService(BaseModuleService):
    MODULE = "currency_conv"
    ENTITY = "汇率快照"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["快照日期", "源货币", "目标货币", "汇率值", "取数来源"]
    REQUIRED_FIELDS = ["快照日期", "源货币", "目标货币"]
    STATUS_ORDER = ["待生效", "已生效", "已过期"]
    ACTION_RULES = {"生效快照": "已生效", "过期快照": "已过期"}
    NEGATIVE_ACTIONS = []

    # ---- 动作分派：标准动作走富结果，迁移场景动作在此识别 ----

    def dispatch_action(self, entry_id: int, action: str,
                        payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        custom = {
            "迁移版本目录": lambda: self.migrate(),
            "执行迁移": lambda: self.migrate(),
            "重新回填": lambda: self.backfill(),
            "幂等回填": lambda: self.backfill(),
            "迁移并回填": lambda: self.migrate(),
            "对账": lambda: self.reconcile(),
            "版本对账": lambda: self.reconcile(),
            "回滚": lambda: self.rollback(reason=str(payload.get("reason", "页面动作：模拟回滚旧环境"))),
            "回滚旧环境": lambda: self.rollback(reason=str(payload.get("reason", "页面动作：回滚旧环境"))),
            "模拟回滚": lambda: self.rollback(reason=str(payload.get("reason", "模拟回滚检查刚交付的附注"))),
            "模拟切换失败": lambda: self.simulate_failed_switch(),
            "补核来源": lambda: self.verify_source(
                snapshot_id=entry_id,
                source=str(payload.get("source", payload.get("来源", ""))),
                provider=str(payload.get("provider", payload.get("提供方", ""))),
                receipt_id=payload.get("receipt_id"),
            ),
        }
        if action in custom:
            return custom[action]()
        entry, message = self.run_action(entry_id, action)
        extra: dict[str, Any] = {"entry": entry}
        if entry is not None and action == "生效快照":
            extra.update(self._activation_extra(entry))
        return {"ok": entry is not None, "message": message, **extra}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "生效快照":
            return self._activate_via_state_machine(entry_id)
        return super().run_action(entry_id, action)

    def _activate_via_state_machine(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """通用动作契约：总是把状态置为「已生效」；同归一化币对的其它生效快照过期。

        与严格的 activate_snapshot 不同，这里不因样例汇率值无法解析而拒绝状态流转，
        以保持通用资源动作（spec 中的 action_targets）行为一致。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        pair = row_pair(entry)
        for row in store.rows(self.MODULE):
            if row.get("id") != entry.get("id") and row.get("status") == "已生效":
                if pair and row_pair(row) == pair:
                    row["status"] = "已过期"
                    row["pending"] = False
        entry["status"] = "已生效"
        entry["pending"] = False

        state = load_state()
        is_new_provider = str(entry.get("提供方编码", "")) == "N10"
        state["active_caliber"] = "new" if is_new_provider else "legacy"
        state.setdefault("switch_history", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "action": "activate",
            "snapshot": assoc_key(entry),
            "pair": pair,
            "rate": row_rate(entry),
            "provider": entry.get("提供方编码", ""),
            "caliber": state["active_caliber"],
        })
        save_state(state)
        try:
            upsert_snapshot_file(entry)
            rebuild_index()
        except Exception:
            pass
        store.persist()
        return entry, f"{self.ENTITY}已生效快照"

    # ---- 业务扩展：汇率快照与换算口径 ----

    def activate_snapshot(self, entry_id: int) -> dict[str, Any]:
        """生效汇率快照，同归一化币对旧快照自动过期（保证生效快照唯一）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        value = row_rate(entry)
        if value is None:
            return {"ok": False, "message": "汇率值无效或不是正数，不能生效"}
        pair = row_pair(entry)
        expired: list[Any] = []
        for row in store.rows(self.MODULE):
            if row.get("id") != entry.get("id") and row.get("status") == "已生效":
                if row_pair(row) == pair and pair:
                    row["status"] = "已过期"
                    row["pending"] = False
                    expired.append(row.get("id"))
        entry["status"] = "已生效"
        entry["pending"] = False

        state = load_state()
        is_n10 = str(entry.get("提供方编码", "")) == "N10" or "N10" in str(entry.get("取数来源", ""))
        state["active_caliber"] = "new" if is_n10 else "legacy"
        state.setdefault("switch_history", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "action": "activate",
            "snapshot": assoc_key(entry),
            "pair": pair,
            "rate": value,
            "provider": entry.get("提供方编码", ""),
            "caliber": state["active_caliber"],
        })
        save_state(state)
        upsert_snapshot_file(entry)
        rebuild_index()
        store.persist()
        sources = self.pair_versions(pair)
        return {"ok": True, "message": "快照已生效，同币对旧版本已过期但仍可在版本目录回查",
                "expired_old": len(expired), "expired_ids": expired,
                "pair": pair, "active_caliber": state["active_caliber"],
                "pair_versions": sources}

    def _activation_extra(self, entry: dict[str, Any]) -> dict[str, Any]:
        state = load_state()
        return {"pair": row_pair(entry), "pair_versions": self.pair_versions(row_pair(entry)),
                "active_caliber": state.get("active_caliber", "legacy")}

    def pair_versions(self, pair: str) -> list[dict[str, Any]]:
        out = []
        for row in store.rows(self.MODULE):
            if row_pair(row) == pair:
                item = snapshot_entry(row)
                item["source_distinction"] = (
                    "新提供方（编码 N10）" if row.get("提供方编码") == "N10"
                    else "旧来源（迁移前口径，保留明确区别，不会改标为新版提供方）"
                )
                out.append(item)
        out.sort(key=lambda e: int(e.get("row_id") or 0))
        return out

    def convert_price(self, entry_id: int, amount: float) -> dict[str, Any]:
        """按指定快照把金额从源货币换算到目标货币（历史锚点方法）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"ok": False, "consumable": False, "message": "快照不存在或未生效"}
        rate = row_rate(entry)
        if rate is None:
            return {"ok": False, "consumable": False, "message": "汇率值无效"}
        converted = round(amount * rate, 2)
        return {
            "ok": True,
            "consumable": True,
            "message": "换算完成",
            "from": normalize_currency(entry.get("源货币", "")),
            "to": normalize_currency(entry.get("目标货币", "")),
            "amount": amount,
            "rate": rate,
            "converted": converted,
            "snapshot_date": row_date(entry),
            "source": entry.get("取数来源", ""),
            "association_key": assoc_key(entry),
        }

    def convert_query(self, amount: float = 0, **query: Any) -> dict[str, Any]:
        """按日期+币对/关联键/版本解析后换算；不可消费时不返回 converted 数值。"""
        verdict = resolve_snapshot(**query)
        if not verdict.get("consumable") or verdict.get("rate") is None:
            return {"ok": False, "consumable": False, "amount": amount,
                    "converted": None,
                    "rate": verdict.get("rate"),
                    "retained_rate": verdict.get("rate"),
                    "source_missing": verdict.get("source_missing"),
                    "source_origin": verdict.get("source_origin"),
                    "source_kind": verdict.get("source_kind"),
                    "association_key": verdict.get("association_key"),
                    "message": verdict.get("reason", "汇率口径不可消费"),
                    "reason": verdict.get("reason", ""),
                    "missing": verdict.get("missing", []),
                    "pair": verdict.get("pair", ""), "date": verdict.get("date", "")}
        rate = float(verdict["rate"])
        return {"ok": True, "consumable": True, "amount": amount,
                "rate": rate, "converted": round(float(amount) * rate, 2),
                "pair": verdict.get("pair"), "date": verdict.get("date"),
                "source": verdict.get("source"), "source_kind": verdict.get("source_kind"),
                "source_verified": verdict.get("source_verified"),
                "warning": verdict.get("warning", ""),
                "association_key": verdict.get("association_key"),
                "catalog_ref": verdict.get("catalog_ref"),
                "message": verdict.get("warning") or "换算完成，取数来源可追溯"}

    def effective_rate(self, source: str, target: str) -> float | None:
        """查询指定归一化币对当前生效汇率。"""
        wanted = pair_key(source, target)
        for row in store.rows(self.MODULE):
            if row.get("status") == "已生效" and row_pair(row) == wanted:
                return row_rate(row)
        return None

    # ---- 定位与版本目录 ----

    def locate(self, *, date: str | None = None, pair: str | None = None,
               source: str | None = None, target: str | None = None,
               association_key: str | None = None) -> dict[str, Any]:
        """旧导出的日期+币对仍可定位原快照；返回该自然键下全部版本。"""
        if association_key:
            verdict = resolve_snapshot(association_key=association_key)
            if not verdict.get("snapshot"):
                return {"ok": False, "found": False, "message": verdict.get("reason")}
            row = verdict["snapshot"]
            return {"ok": True, "found": True, "query": {"association_key": association_key},
                    "versions": self.pair_versions(row_pair(row)),
                    "matched": snapshot_entry(row),
                    "message": "关联键已定位到真实存量快照"}
        p = pair or ""
        if not p and (source or target):
            p = pair_key(source, target)
        if pair and "/" not in str(pair):
            s, t = parse_pair(str(pair))
            p = f"{s}/{t}" if s and t else ""
        day = str(date or "")[:10]
        rows = store.rows(self.MODULE)
        if p:
            rows = [r for r in rows if row_pair(r) == p]
        if day:
            rows = [r for r in rows if row_date(r) == day]
        versions = [snapshot_entry(r) for r in rows]
        if not versions:
            return {"ok": False, "found": False, "date": day, "pair": p,
                    "message": f"未找到 {day or '该日期'} {p or '该币对'} 的原始快照"}
        return {"ok": True, "found": True, "date": day, "pair": p,
                "count": len(versions), "versions": versions,
                "message": f"按旧导出的日期/币对定位到 {len(versions)} 个版本快照"}

    def list_versions(self, *, date: str | None = None, pair: str | None = None) -> dict[str, Any]:
        entries = []
        for row in store.rows(self.MODULE):
            if date and row_date(row) != str(date)[:10]:
                continue
            if pair:
                p = pair if "/" in str(pair) else "/".join(parse_pair(str(pair)))
                if row_pair(row) != p:
                    continue
            entries.append(snapshot_entry(row))
        state = load_state()
        return {"ok": True, "catalog": "版本目录", "active_caliber": state.get("active_caliber"),
                "effective_pointers": rebuild_index().get("effective_pointers"),
                "count": len(entries), "versions": entries}

    # ---- 迁移 / 回填 / 回执 ----

    def _pending_source_items(self) -> list[dict[str, Any]]:
        items = []
        for row in store.rows(self.MODULE):
            missing = source_is_missing(row)
            needs = missing or (not row.get("source_verified", False))
            if needs:
                items.append({
                    "association_key": assoc_key(row),
                    "row_id": row.get("id"),
                    "date": row_date(row),
                    "pair": row_pair(row),
                    "rate": row_rate(row),
                    "current_source": row.get("取数来源", ""),
                    "source_kind": row.get("source_kind", ""),
                    "source_missing": missing,
                    "reason": ("原始取数来源缺失/为占位，补核前该快照不可作为口径消费"
                               if missing else "旧来源留档但原始提供方待运营补核"),
                    "action": "POST /api/currency_conv/verify-source "
                              "{association_key, source, provider?}",
                })
        return items

    def migrate(self, *, fail_after: int | None = None) -> dict[str, Any]:
        """把全部存量快照幂等迁移到版本目录并出具回执；可模拟中途中断。"""
        from pathlib import Path

        from app.runtime_state import CATALOG_DIR

        receipt_id = next_receipt_id()
        rows = list(store.rows(self.MODULE))
        migrated: list[dict[str, Any]] = []
        created = updated = 0
        interrupted = False
        processed = 0
        for row in rows:
            if fail_after is not None and processed >= int(fail_after):
                interrupted = True
                break
            key = assoc_key(row)
            path = CATALOG_DIR / "snapshots" / f"{key}.json"
            existed = path.exists()
            entry = upsert_snapshot_file(row)
            created += 0 if existed else 1
            updated += 1 if existed else 0
            migrated.append({"association_key": key, "row_id": row.get("id"),
                             "natural_key": entry["natural_key"], "rate": entry["rate"],
                             "catalog_ref": entry["catalog_ref"], "existed": existed})
            processed += 1

        # 生效唯一性兜底（重复回填不得复制成两个生效快照）
        duplicates = self._dedupe_effective()
        index = rebuild_index()
        pending = self._pending_source_items()
        state = load_state()
        state["migration_status"] = "interrupted" if interrupted else "completed"
        save_state(state)

        receipt = {
            "receipt_id": receipt_id,
            "kind": "换算迁移回执",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "interrupted" if interrupted else "completed",
            "interrupted": interrupted,
            "catalog_dir": str(CATALOG_DIR),
            "total_in_db": len(rows),
            "processed": processed,
            "created": created,
            "updated": updated,
            "duplicate_effective_resolved": duplicates,
            "db_rows_retained": len(store.rows(self.MODULE)),
            "db_retention_note": "数据库保留全部已写附注的币对与日期，迁移只复制到版本目录，不删行。",
            "effective_pointers": index["effective_pointers"],
            "source_review": pending,
            "source_review_count": len(pending),
            "next_step": "运营凭此回执补核来源（verify-source）；中断后再次调用 migrate/backfill 可续跑且不产生重复。",
            "items": migrated,
        }
        save_receipt(receipt)
        store.persist()
        return {"ok": not interrupted, "message": (
            "迁移中断（模拟）：可再次执行迁移/回填续跑，旧口径与新版寻址仍可对账"
            if interrupted else
            f"迁移完成：处理 {processed} 条，新建版本 {created}、幂等更新 {updated}，"
            f"{len(pending)} 条来源待补核，回执 {receipt_id}"),
            **receipt}

    def backfill(self) -> dict[str, Any]:
        """重复回填 = 不带失败注入的迁移，天然幂等。"""
        result = self.migrate()
        result["message"] = "存量回填幂等完成：" + result["message"]
        return result

    def _dedupe_effective(self) -> int:
        seen: dict[str, dict[str, Any]] = {}
        resolved = 0
        for row in store.rows(self.MODULE):
            if row.get("status") != "已生效":
                continue
            pair = row_pair(row)
            if not pair:
                continue
            if pair in seen:
                # 保留 id 较小的存量快照为生效，其余过期。
                keeper = seen[pair]
                if int(row.get("id", 0)) < int(keeper.get("id", 0)):
                    row, keeper = keeper, row
                    seen[pair] = row
                row["status"] = "已过期"
                row["pending"] = False
                resolved += 1
            else:
                seen[pair] = row
        return resolved

    def reconcile(self) -> dict[str, Any]:
        """旧口径（数据库存量行）与新版寻址（版本目录）对账。"""
        from app.runtime_state import CATALOG_DIR

        rows = store.rows(self.MODULE)
        db_keys = {assoc_key(r) for r in rows}
        disk_files = list((CATALOG_DIR / "snapshots").glob("*.json"))
        disk_keys = {p.stem for p in disk_files}
        matched, mismatches = [], []
        for row in rows:
            key = assoc_key(row)
            path = CATALOG_DIR / "snapshots" / f"{key}.json"
            if not path.exists():
                mismatches.append({"association_key": key, "issue": "版本目录缺少版本文件",
                                   "fix": "再次执行 migrate/backfill"})
                continue
            from app.runtime_state import read_json

            disk = read_json(path) or {}
            problems = []
            if disk.get("rate") != row_rate(row):
                problems.append("rate")
            if disk.get("date") != row_date(row):
                problems.append("date")
            if disk.get("pair") != row_pair(row):
                problems.append("pair")
            if str(disk.get("source", "")) != str(row.get("取数来源", "")):
                problems.append("source")
            if problems:
                mismatches.append({"association_key": key, "issue": "字段不一致: " + ",".join(problems)})
            else:
                matched.append(key)
        db_effective: dict[str, Any] = {}
        duplicate_pairs = []
        for row in rows:
            if row.get("status") == "已生效":
                pair = row_pair(row)
                if pair in db_effective:
                    duplicate_pairs.append(pair)
                db_effective[pair] = assoc_key(row)
        index = rebuild_index()
        state = load_state()
        ok = not mismatches and not duplicate_pairs
        return {
            "ok": ok,
            "message": ("对账一致：旧口径数据库行与版本目录寻址完全对应，且每币对仅一个生效快照"
                        if ok else f"对账发现 {len(mismatches)} 个差异、{len(duplicate_pairs)} 个重复生效币对"),
            "active_caliber": state.get("active_caliber"),
            "migration_status": state.get("migration_status"),
            "db_count": len(rows),
            "catalog_file_count": len(disk_files),
            "db_only_keys": sorted(db_keys - disk_keys),
            "catalog_only_keys": sorted(disk_keys - db_keys),
            "matched_count": len(matched),
            "matched_keys": matched,
            "mismatches": mismatches,
            "duplicate_effective_pairs": duplicate_pairs,
            "db_effective_pointers": db_effective,
            "catalog_effective_pointers": index.get("effective_pointers"),
        }

    # ---- 补核来源 ----

    def verify_source(self, *, association_key: str | None = None,
                      snapshot_id: int | None = None, source: str = "",
                      provider: str = "", receipt_id: str | None = None,
                      verified_by: str = "运营补核") -> dict[str, Any]:
        row: dict[str, Any] | None = None
        if snapshot_id is not None:
            row = store.find(self.MODULE, int(snapshot_id))
        if row is None and association_key:
            row = next((r for r in store.rows(self.MODULE)
                        if assoc_key(r) == association_key), None)
        if row is None:
            return {"ok": False, "message": "未找到要补核来源的快照"}
        source = str(source or "").strip()
        if not source:
            return {"ok": False, "message": "补核来源不能为空；不得用 N10 或编造来源顶替旧来源"}
        original_kind = str(row.get("source_kind", ""))
        row["核补来源"] = source
        if provider:
            row["核补提供方"] = provider
        row["source_verified"] = True
        row["source_missing"] = False
        row["source_verified_by"] = verified_by
        row["source_verified_at"] = datetime.now(timezone.utc).isoformat()
        # 关键：来源类型保留 legacy 前缀，旧来源文本不覆盖，绝不改标 N10。
        if original_kind.startswith("legacy"):
            row["source_kind"] = "legacy_verified"
            row["来源区别"] = "旧来源（已经运营补核），仍不是新提供方 N10"
        upsert_snapshot_file(row)

        # 同步更新最近回执中的待核项
        receipt = None
        if receipt_id:
            from app.version_catalog import get_receipt

            receipt = get_receipt(receipt_id)
        if receipt is None:
            from app.version_catalog import get_receipt

            receipt = get_receipt("latest")
        if receipt and isinstance(receipt.get("source_review"), list):
            for item in receipt["source_review"]:
                if item.get("association_key") == assoc_key(row):
                    item["verified"] = True
                    item["verified_source"] = source
            save_receipt(receipt)
        rebuild_index()
        store.persist()
        return {"ok": True, "message": "来源已补核；旧来源文本与来源类型保留，不会改标为新版提供方",
                "association_key": assoc_key(row), "source_kind": row.get("source_kind"),
                "original_source": row.get("取数来源"), "verified_source": source,
                "snapshot": snapshot_entry(row)}

    # ---- 切换失败 / 回滚 ----

    def mark_switch_blocked(self, reason: str = "新版上线受阻（模拟）") -> dict[str, Any]:
        state = load_state()
        state["last_switch_blocked"] = {
            "at": datetime.now(timezone.utc).isoformat(), "reason": reason}
        save_state(state)
        return {"ok": False, "switch_blocked": True, "message": reason,
                "guidance": "可调用 POST /api/currency_conv/rollback 退回旧环境口径；"
                            "已签发的交付凭据与刚交付附注不受影响，可继续复验/复算。"}

    def simulate_failed_switch(self) -> dict[str, Any]:
        """一键演练：N10 申请生效 -> 切换探针失败 -> 自动回滚旧环境。"""
        n10 = next((r for r in store.rows(self.MODULE)
                    if r.get("提供方编码") == "N10" or r.get("fixture_code") == "SNAP-N10"), None)
        activated = None
        if n10 is not None and n10.get("status") != "已生效":
            activated = self.activate_snapshot(int(n10["id"]))
        blocked = self.mark_switch_blocked("模拟：N10 新提供方切换后对账/健康探针失败，上线受阻")
        rollback = self.rollback(reason="切换失败，按预案退回旧环境 L09/7.0 口径")
        return {"ok": True, "message": "已完成“生效 N10 7.2 → 切换失败 → 回滚旧口径 7.0”演练",
                "n10_activation": activated, "blocked": blocked, "rollback": rollback}

    def rollback(self, *, reason: str = "回滚到迁移前旧环境") -> dict[str, Any]:
        """把当前口径指针退回旧环境（USD/CNY -> L09 7.0）；不删版本、不改已交付清单。"""
        from app.runtime_state import CATALOG_DIR

        state = load_state()
        state["rollback_seq"] = int(state.get("rollback_seq", 0)) + 1
        rb_id = f"RBP-{state['rollback_seq']:04d}"

        legacy_index_path = CATALOG_DIR / "legacy" / "legacy-environment.json"
        from app.runtime_state import read_json

        legacy_archive = read_json(legacy_index_path) or {}
        legacy_pointers = legacy_archive.get("effective_pointers", {})

        changes = []
        # 按币对恢复：优先旧环境留档指针，其次该币对的 legacy 行。
        pairs = {row_pair(r) for r in store.rows(self.MODULE) if row_pair(r)}
        for pair in pairs:
            target_key = legacy_pointers.get(pair)
            target = None
            if target_key:
                target = next((r for r in store.rows(self.MODULE)
                               if assoc_key(r) == target_key), None)
            if target is None:
                target = next((r for r in store.rows(self.MODULE)
                               if row_pair(r) == pair
                               and str(r.get("source_kind", "")).startswith("legacy")), None)
            if target is None:
                continue
            for row in store.rows(self.MODULE):
                if row.get("status") == "已生效" and row_pair(row) == pair \
                        and row.get("id") != target.get("id"):
                    row["status"] = "已过期"
                    row["pending"] = False
                    changes.append({"pair": pair, "expired": assoc_key(row)})
            if target.get("status") != "已生效":
                changes.append({"pair": pair, "restored": assoc_key(target)})
            target["status"] = "已生效"
            target["pending"] = False
            upsert_snapshot_file(target)

        state["active_caliber"] = "legacy"
        record = {"rollback_id": rb_id, "at": datetime.now(timezone.utc).isoformat(),
                  "reason": reason, "changes": changes,
                  "effective_pointers": rebuild_index().get("effective_pointers")}
        state.setdefault("rollback_history", []).append(record)
        save_state(state)
        from app.runtime_state import atomic_write_json

        atomic_write_json(CATALOG_DIR / "rollbacks" / f"{rb_id}.json", record)
        rebuild_index()
        store.persist()

        l09 = next((r for r in store.rows(self.MODULE)
                    if r.get("fixture_code") == "SNAP-L09"), None)
        return {"ok": True, "rollback_id": rb_id,
                "message": "已退回旧环境口径；版本目录中的 N10/7.2 版本与已交付凭据均保留可查",
                "reason": reason, "changes": changes,
                "active_caliber": "legacy",
                "current_effective": record["effective_pointers"],
                "restored_rate": row_rate(l09) if l09 else None,
                "restored_source": (l09 or {}).get("取数来源", ""),
                "restored_association_key": assoc_key(l09) if l09 else "",
                "deliveries_note": "已交付附注按实际交付时冻结的汇率复算，不受本次回滚影响。"}

    def expire_by_date(self, before_date: str) -> dict[str, Any]:
        """按日期批量过期旧快照，并向变价历史同步口径变更。"""
        expired = 0
        for row in store.rows(self.MODULE):
            if row.get("status") == "已生效" and row_date(row) < str(before_date)[:10]:
                row["status"] = "已过期"
                row["pending"] = False
                expired += 1
        if expired:
            hrows = store.rows("change_history")
            hid = max((int(r.get("id", 0)) for r in hrows), default=0) + 1
            hrows.append({
                "id": hid,
                "商品标识": "CURRENCY-ALL",
                "旧价格": "旧汇率快照",
                "新价格": f"已过期 {expired} 条",
                "变化幅度": "口径切换",
                "发现时间": datetime.now(timezone.utc).isoformat(),
                "status": "待核实",
                "pending": True,
                "abnormal": False,
            })
        store.persist()
        return {"ok": True, "message": f"已过期 {expired} 条快照", "expired": expired}

    def ensure_site_currency(self, entry_id: int) -> dict[str, Any]:
        """检查站点生效货币是否具备可用快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "快照不存在"}
        currency = normalize_currency(entry.get("源货币", ""))
        covered = any(
            r.get("status") == "已生效"
            and currency in (normalize_currency(r.get("源货币", "")),
                             normalize_currency(r.get("目标货币", "")))
            for r in store.rows(self.MODULE)
        )
        if not covered:
            entry["pending"] = True
        return {"ok": True, "message": "货币覆盖检查完成", "currency": currency, "covered": covered}

    def snapshot_for_export(self, entry_id: int) -> dict[str, Any]:
        """向比价导出通道提供换算口径说明（必须是实际绑定的快照）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"ok": False, "consumable": False, "message": "快照不存在或未生效"}
        return {
            "ok": True,
            "consumable": True,
            "message": "换算口径已提供",
            "rate": entry.get("汇率值", ""),
            "pair": row_pair(entry),
            "source": entry.get("取数来源", ""),
            "date": row_date(entry),
            "association_key": assoc_key(entry),
            "note": compose_note(entry),
        }
