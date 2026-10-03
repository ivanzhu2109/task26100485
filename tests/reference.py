"""独立的小规模全量 MRP 计算（参照实现）。

刻意写得简单直接：每次调用都基于当前需求、原始库存与既定到货
从头计算，逐日全扫描 0..364，用于核对增量引擎的结果。
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, Iterable, List, Set

from mrp.model import (
    DAY_MAX,
    DAY_MIN,
    BomVersion,
    Demand,
    DayPlan,
    Item,
    ItemKind,
    ItemPlan,
    ScheduledReceipt,
)


class ReferenceMRP:
    def __init__(
        self,
        items: Iterable[Item],
        bom_versions: Iterable[BomVersion],
        on_hand: Dict[str, int],
        receipts: Iterable[ScheduledReceipt],
    ) -> None:
        self._items = {it.item_id: it for it in items}
        self._bom: Dict[str, List[BomVersion]] = {}
        for bv in bom_versions:
            self._bom.setdefault(bv.item_id, []).append(bv)
        for versions in self._bom.values():
            versions.sort(key=lambda v: v.start_day)
        self._starts = {k: [v.start_day for v in vlist] for k, vlist in self._bom.items()}
        self._on_hand = dict(on_hand)
        self._receipts: Dict[str, Dict[int, int]] = {}
        for r in receipts:
            slot = self._receipts.setdefault(r.item_id, {})
            slot[r.day] = slot.get(r.day, 0) + r.qty
        self._demands: Dict[str, Demand] = {}
        self._plan: Dict[str, ItemPlan] = {}

    # ---------------- 需求维护（由测试驱动，与引擎调用保持同步） ----------------

    def replace_demand(self, demand: Demand) -> None:
        self._demands[demand.demand_id] = demand

    def cancel_demand(self, demand_id: str) -> None:
        self._demands.pop(demand_id, None)

    # ---------------- 全量重算 ----------------

    def recompute(self, scope: Set[str] | None = None) -> None:
        """从头计算。scope 限定只重算这些物料及其下游（它们与外界无依赖时使用）。"""
        gross: Dict[str, Dict[int, int]] = {}
        for d in self._demands.values():
            if scope is not None and d.item_id not in scope:
                continue
            slot = gross.setdefault(d.item_id, {})
            slot[d.day] = slot.get(d.day, 0) + d.qty

        # 拓扑：反复处理父件已全部就绪的物料
        pending = set(scope) if scope is not None else set(self._items)
        arrival: Dict[str, Dict[int, int]] = {}
        release: Dict[str, Dict[int, int]] = {}
        endinv: Dict[str, Dict[int, int]] = {}

        def parents_of(item_id: str) -> Set[str]:
            out = set()
            for pid, versions in self._bom.items():
                if scope is not None and pid not in scope:
                    continue
                for v in versions:
                    if any(line.child_id == item_id for line in v.lines):
                        out.add(pid)
            return out

        done: Set[str] = set()
        while pending:
            ready = [i for i in pending if not (parents_of(i) & pending)]
            assert ready, "依赖成环"
            for item_id in ready:
                item = self._items[item_id]
                receipts = self._receipts.get(item_id, {})
                g = gross.get(item_id, {})
                inv = self._on_hand.get(item_id, 0)
                arr: Dict[int, int] = {}
                rel: Dict[int, int] = {}
                einv: Dict[int, int] = {}
                for day in range(DAY_MIN, DAY_MAX + 1):
                    inv += receipts.get(day, 0)
                    need = g.get(day, 0)
                    if need:
                        if inv < need:
                            shortage = need - inv
                            qty = -(-shortage // item.lot_size) * item.lot_size
                            arr[day] = qty
                            rd = day - item.lead_time
                            rel[rd] = rel.get(rd, 0) + qty
                            inv += qty
                        inv -= need
                    einv[day] = inv
                arrival[item_id] = arr
                release[item_id] = rel
                endinv[item_id] = einv
                # 展开
                if item.kind is ItemKind.MANUFACTURED:
                    for day, qty in rel.items():
                        versions = self._bom[item_id]
                        idx = bisect_right(self._starts[item_id], day) - 1
                        for line in versions[idx].lines:
                            if scope is not None and line.child_id not in scope:
                                continue
                            slot = gross.setdefault(line.child_id, {})
                            slot[day] = slot.get(day, 0) + qty * line.qty_per
                pending.discard(item_id)
                done.add(item_id)

        # 生成逐日计划
        for item_id in done:
            g = gross.get(item_id, {})
            arr = arrival[item_id]
            rel = release[item_id]
            einv = endinv[item_id]
            self._plan[item_id] = [
                DayPlan(
                    day=day,
                    gross_req=g.get(day, 0),
                    suggested_arrival=arr.get(day, 0),
                    suggested_release=rel.get(day, 0),
                    end_inventory=einv[day],
                )
                for day in range(DAY_MIN, DAY_MAX + 1)
            ]

    def query(self, item_id: str) -> ItemPlan:
        return self._plan[item_id]
