"""规模测试：2万物料、8万BOM行、5万独立需求。

- 首次载入至可查询 <= 15 秒
- 连续 200 次单行改量/改期/撤销 + 查询受影响物料完整日计划 <= 8 秒
- 每次调整后与独立全量参照实现核对（核对耗时不计入上述 8 秒）

结构：2500 个相互独立的产品族，每族 4 成品 + 2 子装配件 + 2 采购件。
族间无共享，因此参照实现可按族 scoped 重算，核对开销可控。
"""
import random
import time
import unittest

from mrp import (
    BomLine,
    BomVersion,
    Demand,
    Item,
    ItemKind,
    MrpPlanner,
    ScheduledReceipt,
)
from tests.reference import ReferenceMRP

N_FAMILIES = 2500
N_OPS = 200
LOAD_BUDGET = 15.0
OPS_BUDGET = 8.0


def build_dataset(seed=20261004):
    rng = random.Random(seed)
    items, boms, receipts, demands = [], [], [], []
    on_hand = {}
    families = []  # [(family_item_ids, fg_ids)]
    for f in range(N_FAMILIES):
        fgs = [f"F{f}_FG{i}" for i in range(4)]
        subs = [f"F{f}_SUB{i}" for i in range(2)]
        comps = [f"F{f}_CMP{i}" for i in range(2)]
        for iid in fgs:
            items.append(Item(iid, ItemKind.MANUFACTURED, rng.randint(0, 7),
                              rng.choice([1, 2, 5])))
            # 3 个版本，每版 2 行
            cuts = sorted(rng.sample(range(60, 330), 2))
            for s, e in zip([0] + cuts, cuts + [365]):
                boms.append(BomVersion(iid, s, e, tuple(
                    BomLine(c, rng.randint(1, 3)) for c in subs)))
        for iid in subs:
            items.append(Item(iid, ItemKind.MANUFACTURED, rng.randint(0, 7),
                              rng.choice([1, 4, 10])))
            cuts = sorted(rng.sample(range(60, 330), 1))
            for s, e in zip([0] + cuts, cuts + [365]):
                boms.append(BomVersion(iid, s, e, tuple(
                    BomLine(c, rng.randint(1, 2)) for c in comps)))
        for iid in comps:
            items.append(Item(iid, ItemKind.PURCHASED, rng.randint(0, 7),
                              rng.choice([1, 10, 50])))
        for iid in fgs + subs + comps:
            on_hand[iid] = rng.randint(0, 30)
            if rng.random() < 0.3:
                receipts.append(ScheduledReceipt(
                    iid, rng.randint(0, 364), rng.randint(1, 40)))
        # 每个成品 5 条需求，落在 3 个不同交期上（控制毛需求组合总数）
        for i, fg in enumerate(fgs):
            base = rng.randint(100, 330)
            for k, off in enumerate([0, 0, 8, 16, 16]):
                demands.append(Demand(
                    f"F{f}_D{i}_{k}", fg, base + off, rng.randint(1, 20)))
        families.append((fgs + subs + comps, fgs))
    return items, boms, on_hand, receipts, demands, families


def plan_rows(plan):
    return [(r.gross_req, r.suggested_arrival, r.suggested_release,
             r.end_inventory) for r in plan]


class ScaleTest(unittest.TestCase):
    def test_scale_and_incremental_performance(self):
        items, boms, on_hand, receipts, demands, families = build_dataset()
        self.assertEqual(len(items), 20000)
        self.assertEqual(sum(len(v.lines) for v in boms), 80000)
        self.assertEqual(len(demands), 50000)

        planner = MrpPlanner()
        t0 = time.perf_counter()
        planner.load(items, boms, on_hand, receipts, demands)
        load_s = time.perf_counter() - t0
        print(f"\n[scale] 首次载入+完整计划: {load_s:.2f}s (预算 {LOAD_BUDGET}s)")
        self.assertLessEqual(load_s, LOAD_BUDGET)

        combos = sum(len(days) for days in planner._gross.values())
        print(f"[scale] 完整计划毛需求(物料,日期)组合数: {combos} (上限 200000)")
        self.assertLessEqual(combos, 200000)

        ref = ReferenceMRP(items, boms, on_hand, receipts)
        for d in demands:
            ref.replace_demand(d)

        # 载入后抽样核对（不计时）
        rng = random.Random(7)
        for fam_items, _ in rng.sample(families, 25):
            ref.recompute(scope=set(fam_items))
            for iid in fam_items:
                self.assertEqual(plan_rows(planner.query(iid)),
                                 plan_rows(ref.query(iid)), iid)

        live = [d.demand_id for d in demands]
        demand_by_id = {d.demand_id: d for d in demands}
        fam_of_item = {}
        for idx, (fam_items, _) in enumerate(families):
            for iid in fam_items:
                fam_of_item[iid] = idx

        ops_s = 0.0
        rng = random.Random(99)
        cancelled = 0
        for step in range(N_OPS):
            did = live[rng.randrange(len(live))]
            old = demand_by_id[did]
            roll = rng.random()
            if roll < 0.4:      # 改量
                new = Demand(did, old.item_id, old.day, rng.randint(1, 25))
            elif roll < 0.8:    # 改期
                new = Demand(did, old.item_id, rng.randint(100, 364), old.qty)
            else:               # 撤销
                new = None

            t0 = time.perf_counter()
            if new is None:
                planner.cancel_demand(did)
            else:
                planner.replace_demand(new)
            fam_items, _ = families[fam_of_item[old.item_id]]
            got = {iid: planner.query(iid) for iid in fam_items}
            ops_s += time.perf_counter() - t0

            # 正确性核对（不计入修改与查询耗时）
            if new is None:
                ref.cancel_demand(did)
                live.remove(did)
                cancelled += 1
            else:
                ref.replace_demand(new)
                demand_by_id[did] = new
            ref.recompute(scope=set(fam_items))
            for iid in fam_items:
                self.assertEqual(plan_rows(got[iid]),
                                 plan_rows(ref.query(iid)),
                                 f"step {step} 物料 {iid}")

        print(f"[scale] {N_OPS} 次调整+查询: {ops_s:.2f}s "
              f"(预算 {OPS_BUDGET}s, 含撤销 {cancelled} 次)")
        self.assertLessEqual(ops_s, OPS_BUDGET)

        # 收尾再抽样核对若干未触及的族
        for fam_items, _ in rng.sample(families, 25):
            ref.recompute(scope=set(fam_items))
            for iid in fam_items:
                self.assertEqual(plan_rows(planner.query(iid)),
                                 plan_rows(ref.query(iid)), iid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
