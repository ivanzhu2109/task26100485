"""独立的全量重算参照实现。

只使用当前独立需求、原始期初库存与原始既定到货，从零计算全部
物料的计划。按层级顺序对每种物料做一次完整净算并整体展开子件
需求，不复用任何上一版建议结果，作为增量引擎的正确性对照。

返回稀疏结构：item_id -> {day: DailyPlan}，只包含有毛需求或既定
到货的事件日；日末库存在相邻事件日之间保持不变。
"""
from __future__ import annotations

from bisect import bisect_right

from .types import (
    BomVersion,
    DailyPlan,
    IndependentDemand,
    Item,
    ScheduledReceipt,
)

DAYS = 365


def full_recompute(
    items: list[Item],
    bom_versions: list[BomVersion],
    on_hand: dict[int, int],
    receipts: list[ScheduledReceipt],
    demands: list[IndependentDemand],
) -> dict[int, dict[int, DailyPlan]]:
    item_map = {it.item_id: it for it in items}

    versions: dict[int, list[BomVersion]] = {}
    for ver in bom_versions:
        versions.setdefault(ver.parent_id, []).append(ver)
    starts: dict[int, list[int]] = {}
    for pid, vers in versions.items():
        vers.sort(key=lambda v: v.start_day)
        starts[pid] = [v.start_day for v in vers]

    def version_at(item_id: int, day: int) -> BomVersion | None:
        vers = versions.get(item_id)
        if not vers:
            return None
        i = bisect_right(starts[item_id], day) - 1
        if i < 0:
            return None
        ver = vers[i]
        return ver if ver.start_day <= day < ver.end_day else None

    parents: dict[int, set[int]] = {it.item_id: set() for it in items}
    for ver in bom_versions:
        for line in ver.lines:
            parents[line.component_id].add(ver.parent_id)
    level: dict[int, int] = {}

    def visit(item_id: int) -> int:
        if item_id in level:
            return level[item_id]
        level[item_id] = 0
        level[item_id] = 1 + max((visit(p) for p in parents[item_id]), default=-1)
        return level[item_id]

    for it in items:
        visit(it.item_id)

    gross: dict[int, dict[int, int]] = {it.item_id: {} for it in items}
    for d in demands:
        g = gross[d.item_id]
        g[d.day] = g.get(d.day, 0) + d.qty

    rec: dict[int, dict[int, int]] = {it.item_id: {} for it in items}
    for r in receipts:
        rec[r.item_id][r.day] = rec[r.item_id].get(r.day, 0) + r.qty

    plans: dict[int, dict[int, DailyPlan]] = {}
    for item_id in sorted(item_map, key=lambda x: (level[x], x)):
        it = item_map[item_id]
        inv = on_hand.get(item_id, 0)
        g = gross[item_id]
        rc = rec[item_id]
        days = sorted(set(g) | set(rc))
        sparse: dict[int, DailyPlan] = {}
        arr_rel: dict[int, int] = {}
        eoh_by_day: dict[int, int] = {}
        for day in days:
            inv += rc.get(day, 0) - g.get(day, 0)
            arr = 0
            if inv < 0:
                arr = (-inv + it.lot_size - 1) // it.lot_size * it.lot_size
                rel_day = day - it.lead_time
                arr_rel[rel_day] = arr_rel.get(rel_day, 0) + arr
                inv += arr
            sparse[day] = DailyPlan(
                day=day,
                gross=g.get(day, 0),
                arrival=arr,
                release=0,
                eoh=inv,
            )
            eoh_by_day[day] = inv
        # 建议下达按下达日聚合后回填（下达日可能不是事件日）
        for day, qty in arr_rel.items():
            if day in sparse:
                p = sparse[day]
                sparse[day] = DailyPlan(day, p.gross, p.arrival, qty, p.eoh)
            else:
                prev = [d for d in days if d < day]
                inv_at = eoh_by_day[prev[-1]] if prev else on_hand.get(item_id, 0)
                sparse[day] = DailyPlan(day, 0, 0, qty, inv_at)
        plans[item_id] = sparse
        for day, qty in arr_rel.items():
            ver = version_at(item_id, day)
            if ver is None:
                continue
            for line in ver.lines:
                cg = gross[line.component_id]
                cg[day] = cg.get(day, 0) + qty * line.qty_per
    return plans
