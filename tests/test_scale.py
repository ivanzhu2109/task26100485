"""规模测试：2 万种物料、8 万条 BOM 行、5 万条独立需求。

可复现：固定随机种子。运行方式：
    python3 -m tests.test_scale          # 直接运行并打印实测耗时
    python3 -m unittest tests.test_scale # 作为单元测试运行

规模约束（生成后断言）：
- 物料 20000 种，BOM 行 >= 80000，独立需求 50000 条
- 完整计划中有毛需求的 (物料, 日期) 组合 <= 200000

性能要求：
- 首次载入至可查询 <= 15 秒
- 连续 200 次单行改量/改期/撤销 + 查询变化物料的完整日计划 <= 8 秒
- 每次调整后变化物料 <= 50 种
- 每次调整后都与独立全量重算核对全部物料（核对耗时不计入上述 8 秒）
"""
from __future__ import annotations

import random
import time
import unittest

from mrp import (
    BomLine,
    BomVersion,
    IndependentDemand,
    Item,
    MRPPlanner,
    ScheduledReceipt,
)
from mrp.reference import full_recompute

SEED = 26100485
N_ITEMS = 20000
N_DEMANDS = 50000
N_OPS = 200
LOAD_LIMIT_S = 15.0
OPS_LIMIT_S = 8.0
MAX_CHANGED = 50
MAX_GROSS_COMBOS = 200_000

LEVEL_SIZES = [1000, 1400, 1800, 2000, 2000, 1800, 1800, 1700, 1700, 1600, 1500, 1700]
DAY_POOL = [100 + 50 * k for k in range(4)]  # 需求日池：100/150/200/250


def generate(seed: int = SEED):
    """确定性生成规模数据。返回 (items, boms, on_hand, receipts, demands, level_of)。"""
    rng = random.Random(seed)
    assert sum(LEVEL_SIZES) == N_ITEMS
    items: list[Item] = []
    level_of: dict[int, int] = {}
    idx = 0
    for lv, cnt in enumerate(LEVEL_SIZES):
        for _ in range(cnt):
            purchased = rng.random() < (0.15 + 0.07 * lv)
            items.append(
                Item(idx, purchased, rng.randint(0, 3), rng.choice([1, 2, 5, 10, 20, 50]))
            )
            level_of[idx] = lv
            idx += 1
    by_level: dict[int, list[int]] = {}
    for it in items:
        by_level.setdefault(level_of[it.item_id], []).append(it.item_id)

    boms: list[BomVersion] = []
    for it in items:
        if it.is_purchased:
            continue
        lv = level_of[it.item_id]
        higher = [c for h in range(lv + 1, 12) for c in by_level[h]]
        if not higher:  # 最深层自制件退化为采购件
            items[it.item_id] = Item(it.item_id, True, it.lead_time, it.lot_size)
            continue
        bounds = [0] + sorted(rng.sample(range(90, 280), 1)) + [365]
        for v in range(2):
            lines = tuple(
                BomLine(c, rng.randint(1, 5)) for c in rng.sample(higher, rng.randint(3, 6))
            )
            boms.append(BomVersion(it.item_id, bounds[v], bounds[v + 1], lines))

    top_ids = [i for lv in range(2) for i in by_level[lv]]
    demands = [
        IndependentDemand(did, rng.choice(top_ids), rng.choice(DAY_POOL), rng.randint(1, 60))
        for did in range(N_DEMANDS)
    ]
    on_hand = {it.item_id: rng.randint(0, 200) for it in items if rng.random() < 0.3}
    receipts = [
        ScheduledReceipt(rng.randrange(N_ITEMS), rng.randint(0, 364), rng.randint(1, 200))
        for _ in range(20000)
    ]
    return items, boms, on_hand, receipts, demands, level_of


def build_children(boms: list[BomVersion]) -> dict[int, set[int]]:
    children: dict[int, set[int]] = {}
    for v in boms:
        for ln in v.lines:
            children.setdefault(v.parent_id, set()).add(ln.component_id)
    return children


def downstream_closure(children: dict[int, set[int]], item_id: int) -> set[int]:
    seen: set[int] = set()
    stack = [item_id]
    while stack:
        for c in children.get(stack.pop(), ()):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def make_ops(demands: list[IndependentDemand], children, level_of, seed: int = 31337):
    """构造 200 次单行调整（改量/改期/撤销），目标物料下游闭包 1..45。

    返回 (kind, demand, coverage) 列表，coverage 为可能受影响的物料集合。
    """
    dem_by_item: dict[int, list[IndependentDemand]] = {}
    for d in demands:
        dem_by_item.setdefault(d.item_id, []).append(d)
    candidates = []
    for i, ds in dem_by_item.items():
        cov = downstream_closure(children, i)
        if 1 <= len(cov) <= 45:
            cov = cov | {i}
            for d in ds:
                candidates.append((d, cov))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    ops = []
    for k, (d, cov) in enumerate(candidates[:N_OPS]):
        kind = ("remove", "qty", "date")[k % 3]
        ops.append((kind, d, cov))
    return ops


class ScaleTest(unittest.TestCase):
    def test_scale(self) -> None:
        items, boms, on_hand, receipts, demands, level_of = generate()
        n_bom_lines = sum(len(v.lines) for v in boms)
        self.assertEqual(len(items), N_ITEMS)
        self.assertGreaterEqual(n_bom_lines, 80000)
        self.assertEqual(len(demands), N_DEMANDS)

        # ---- 首次载入 ----
        planner = MRPPlanner()
        t0 = time.perf_counter()
        planner.load(items, boms, on_hand, receipts, demands)
        load_s = time.perf_counter() - t0

        # ---- 规模约束：有毛需求的 (物料, 日期) 组合 <= 20 万 ----
        ref = full_recompute(items, boms, on_hand, receipts, demands)
        combos = sum(1 for s in ref.values() for p in s.values() if p.gross > 0)
        self.assertLessEqual(combos, MAX_GROSS_COMBOS)

        # ---- 初始计划正确性（不计时）----
        self._verify_all(planner, items, ref)

        # ---- 200 次调整 ----
        children = build_children(boms)
        ops = make_ops(demands, children, level_of)
        live = {d.demand_id: d for d in demands}
        timed_s = 0.0
        changed_log: list[int] = []
        for kind, d, cov in ops:
            before = {i: planner.sparse_plan(i) for i in cov}  # 核对用，不计时
            t0 = time.perf_counter()
            if kind == "remove":
                planner.remove_demand(d.demand_id)
                del live[d.demand_id]
            elif kind == "qty":
                nd = IndependentDemand(d.demand_id, d.item_id, d.day, d.qty + 7)
                planner.replace_demand(nd)
                live[d.demand_id] = nd
            else:
                nd = IndependentDemand(
                    d.demand_id, d.item_id, 100 + (d.day + 40) % 200, d.qty
                )
                planner.replace_demand(nd)
                live[d.demand_id] = nd
            t_op = time.perf_counter() - t0
            changed = [i for i in cov if planner.sparse_plan(i) != before[i]]
            t0 = time.perf_counter()
            for i in changed:  # 查询变化物料的完整日计划
                planner.daily_plan(i)
            timed_s += t_op + (time.perf_counter() - t0)
            changed_log.append(len(changed))
            self.assertLessEqual(len(changed), MAX_CHANGED)
            # 每次调整后核对全部物料（不计入修改与查询耗时）
            ref = full_recompute(items, boms, on_hand, receipts, list(live.values()))
            self._verify_all(planner, items, ref)

        self.assertLessEqual(load_s, LOAD_LIMIT_S)
        self.assertLessEqual(timed_s, OPS_LIMIT_S)
        print(
            f"\n[scale] items={len(items)} bom_lines={n_bom_lines} "
            f"demands={len(demands)} gross_combos={combos}\n"
            f"[scale] load={load_s:.2f}s (限 {LOAD_LIMIT_S:.0f}s)\n"
            f"[scale] 200次修改+查询={timed_s:.2f}s (限 {OPS_LIMIT_S:.0f}s), "
            f"单次变化物料 max={max(changed_log)} avg={sum(changed_log)/len(changed_log):.1f}"
        )

    def _verify_all(self, planner: MRPPlanner, items, ref) -> None:
        for it in items:
            expected = ref.get(it.item_id, {})
            actual = planner.sparse_plan(it.item_id)
            self.assertEqual(
                {d: (p.gross, p.arrival, p.release, p.eoh) for d, p in actual.items()},
                {d: (p.gross, p.arrival, p.release, p.eoh) for d, p in expected.items()},
                msg=f"item {it.item_id} 与全量重算不一致",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
