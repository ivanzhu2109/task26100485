"""增量 MRP 计划引擎。

核心思路：
- 每个物料的毛需求 = 独立需求 + 所有父件展开需求，按天合并存储。
- 单件净算（_simulate）：逐事件日扫描，先到货、再抵扣毛需求，
  缺口按批量向上取整产生建议到货，建议下达日 = 到货日 - 提前期。
- 自制件按下达日选 BOM 版本展开，仅展开建议到货数量。
- 调整时只重算毛需求发生变化的物料，按依赖层级（父件层级 < 子件层级）
  依次传播，结果与全量重算完全一致。
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, Iterable, List, Set

from .model import (
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


class MrpPlanner:
    """进程内 MRP 计划库，由上层直接调用，串行执行。"""

    def __init__(self) -> None:
        self._items: Dict[str, Item] = {}
        # 自制件 -> 按 start_day 排序的版本列表
        self._bom: Dict[str, List[BomVersion]] = {}
        # 自制件 -> 版本起始日数组（用于二分查找）
        self._bom_starts: Dict[str, List[int]] = {}
        self._on_hand: Dict[str, int] = {}
        self._receipts: Dict[str, Dict[int, int]] = {}
        self._demands: Dict[str, Demand] = {}
        # 物料 -> 天 -> 独立需求合计
        self._indep: Dict[str, Dict[int, int]] = {}
        # 物料 -> 天 -> 合并后的毛需求
        self._gross: Dict[str, Dict[int, int]] = {}
        # 计划结果
        self._arrival: Dict[str, Dict[int, int]] = {}
        self._release: Dict[str, Dict[int, int]] = {}
        self._endinv: Dict[str, Dict[int, int]] = {}
        # 自制件 -> 子件 -> 天 -> 该父件当前展开出的需求
        self._explosion: Dict[str, Dict[str, Dict[int, int]]] = {}
        # 依赖层级：父件层级严格小于子件层级
        self._level: Dict[str, int] = {}

    # ------------------------------------------------------------------ 载入

    def load(
        self,
        items: Iterable[Item],
        bom_versions: Iterable[BomVersion],
        on_hand: Dict[str, int],
        receipts: Iterable[ScheduledReceipt],
        demands: Iterable[Demand],
    ) -> None:
        """载入全部基础数据并完成首次完整计划。"""
        self.__init__()
        for it in items:
            self._items[it.item_id] = it
        for bv in bom_versions:
            self._bom.setdefault(bv.item_id, []).append(bv)
        for item_id, versions in self._bom.items():
            versions.sort(key=lambda v: v.start_day)
            self._bom_starts[item_id] = [v.start_day for v in versions]
        self._on_hand = dict(on_hand)
        for r in receipts:
            slot = self._receipts.setdefault(r.item_id, {})
            slot[r.day] = slot.get(r.day, 0) + r.qty
        for d in demands:
            self._demands[d.demand_id] = d
            slot = self._indep.setdefault(d.item_id, {})
            slot[d.day] = slot.get(d.day, 0) + d.qty
        for item_id, days in self._indep.items():
            self._gross[item_id] = dict(days)
        self._compute_levels()
        # 首次完整计划：重算所有有毛需求的物料（按层级传播），
        # 再补齐只有库存/既定到货的物料的库存曲线。
        self._propagate(set(self._gross))
        for item_id in self._items:
            if item_id not in self._endinv:
                self._simulate(item_id)

    def _compute_levels(self) -> None:
        children: Dict[str, Set[str]] = {}
        for item_id, versions in self._bom.items():
            for v in versions:
                for line in v.lines:
                    children.setdefault(item_id, set()).add(line.child_id)
        memo: Dict[str, int] = {}

        def level_of(item_id: str) -> int:
            if item_id in memo:
                return memo[item_id]
            memo[item_id] = 0  # 输入保证无环，占位防重入
            best = 0
            for child in children.get(item_id, ()):
                best = max(best, level_of(child) + 1)
            memo[item_id] = best
            return best

        # level 语义：到叶子的距离，父件 > 子件；传播时从大到小处理。
        for item_id in self._items:
            level_of(item_id)
        self._level = memo

    # -------------------------------------------------------------- 单件净算

    def _simulate(self, item_id: str) -> None:
        """按当前毛需求重算单件的建议到货/下达/日末库存。"""
        item = self._items[item_id]
        receipts = self._receipts.get(item_id, {})
        gross = self._gross.get(item_id, {})
        days = sorted(set(receipts) | set(gross))
        inv = self._on_hand.get(item_id, 0)
        lot = item.lot_size
        lead = item.lead_time
        arrival: Dict[int, int] = {}
        release: Dict[int, int] = {}
        endinv: Dict[int, int] = {}
        for d in days:
            inv += receipts.get(d, 0)
            g = gross.get(d, 0)
            if g:
                if inv < g:
                    shortage = g - inv
                    qty = -(-shortage // lot) * lot
                    arrival[d] = qty
                    rd = d - lead
                    release[rd] = release.get(rd, 0) + qty
                    inv += qty
                inv -= g
            endinv[d] = inv
        self._arrival[item_id] = arrival
        self._release[item_id] = release
        self._endinv[item_id] = endinv

    # -------------------------------------------------------------- BOM 展开

    def _version_at(self, item_id: str, day: int) -> BomVersion:
        versions = self._bom[item_id]
        idx = bisect_right(self._bom_starts[item_id], day) - 1
        return versions[idx]

    def _explode(self, item_id: str) -> Dict[str, Dict[int, int]]:
        """按当前建议下达展开自制件，返回 子件 -> 天 -> 需求。"""
        out: Dict[str, Dict[int, int]] = {}
        release = self._release.get(item_id, {})
        if not release:
            return out
        for day, qty in release.items():
            version = self._version_at(item_id, day)
            for line in version.lines:
                slot = out.setdefault(line.child_id, {})
                slot[day] = slot.get(day, 0) + qty * line.qty_per
        return out

    # -------------------------------------------------------------- 增量传播

    def _recompute_item(self, item_id: str) -> List[str]:
        """重算单件，返回毛需求发生变化的子件列表。"""
        self._simulate(item_id)
        item = self._items[item_id]
        if item.kind is not ItemKind.MANUFACTURED:
            return ()
        old_expl = self._explosion.get(item_id, {})
        new_expl = self._explode(item_id)
        self._explosion[item_id] = new_expl
        dirtied: List[str] = []
        for child in set(old_expl) | set(new_expl):
            old_days = old_expl.get(child, {})
            new_days = new_expl.get(child, {})
            if old_days == new_days:
                continue
            cg = self._gross.setdefault(child, {})
            changed = False
            for day in set(old_days) | set(new_days):
                delta = new_days.get(day, 0) - old_days.get(day, 0)
                if delta:
                    val = cg.get(day, 0) + delta
                    if val:
                        cg[day] = val
                    else:
                        cg.pop(day, None)
                    changed = True
            if changed:
                dirtied.append(child)
        return dirtied

    def _propagate(self, dirty: Set[str]) -> None:
        """按层级分桶从大到小（父件先于子件）重算，每件最多一次。

        层级 = 到叶子的距离，父件严格大于子件；处理某层级时只会把
        更低层级的物料加入桶中，因此每个物料每次调整最多重算一次。
        """
        level = self._level
        buckets: Dict[int, List[str]] = {}
        for i in dirty:
            buckets.setdefault(level.get(i, 0), []).append(i)
        done: Set[str] = set()
        lvl = max(buckets, default=-1)
        while lvl >= 0:
            stack = buckets.get(lvl)
            if stack:
                item_id = stack.pop()
                if item_id in done:
                    continue
                done.add(item_id)
                for child in self._recompute_item(item_id):
                    buckets.setdefault(level.get(child, 0), []).append(child)
            else:
                lvl -= 1

    # -------------------------------------------------------------- 需求调整

    def _apply_indep_delta(self, item_id: str, day: int, delta: int, dirty: Set[str]) -> None:
        if not delta:
            return
        indep = self._indep.setdefault(item_id, {})
        val = indep.get(day, 0) + delta
        if val:
            indep[day] = val
        else:
            indep.pop(day, None)
        gross = self._gross.setdefault(item_id, {})
        gval = gross.get(day, 0) + delta
        if gval:
            gross[day] = gval
        else:
            gross.pop(day, None)
        dirty.add(item_id)

    def replace_demand(self, demand: Demand) -> None:
        """新增或替换一条带稳定编号的独立需求。"""
        dirty: Set[str] = set()
        old = self._demands.get(demand.demand_id)
        if old is not None:
            self._apply_indep_delta(old.item_id, old.day, -old.qty, dirty)
        self._demands[demand.demand_id] = demand
        self._apply_indep_delta(demand.item_id, demand.day, demand.qty, dirty)
        self._propagate(dirty)

    def cancel_demand(self, demand_id: str) -> None:
        """撤销一条独立需求。"""
        old = self._demands.pop(demand_id, None)
        if old is None:
            return
        dirty: Set[str] = set()
        self._apply_indep_delta(old.item_id, old.day, -old.qty, dirty)
        self._propagate(dirty)

    # ------------------------------------------------------------------ 查询

    def query(self, item_id: str) -> ItemPlan:
        """查询指定物料 0..364 日的逐日计划。"""
        gross = self._gross.get(item_id, {})
        arrival = self._arrival.get(item_id, {})
        release = self._release.get(item_id, {})
        endinv = self._endinv.get(item_id, {})
        inv = self._on_hand.get(item_id, 0)
        plan: ItemPlan = []
        for d in range(DAY_MIN, DAY_MAX + 1):
            inv = endinv.get(d, inv)
            plan.append(
                DayPlan(
                    day=d,
                    gross_req=gross.get(d, 0),
                    suggested_arrival=arrival.get(d, 0),
                    suggested_release=release.get(d, 0),
                    end_inventory=inv,
                )
            )
        return plan
