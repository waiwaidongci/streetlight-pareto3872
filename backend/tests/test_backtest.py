"""派巡门槛回测域逻辑测试，仅依赖标准库：python3 -m tests.test_backtest"""

import sys
import traceback
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import backtest  # noqa: E402
from domain.demo import demo_rows  # noqa: E402

PARAMS = {"start_date": "2026-11-14", "end_date": "2026-11-20"}


def register(site_id, district, name, fixtures):
    return {"record_type": "灯组台账", "site_id": site_id, "district": district,
            "site_name": name, "fixture_count": fixtures,
            "incident_id": None, "report_date": None, "is_dark": None, "quota": None}


def ticket(site_id, incident_id, day):
    return {"record_type": "暗区工单", "site_id": site_id, "incident_id": incident_id,
            "report_date": f"2026-11-{day}", "district": None, "site_name": None,
            "fixture_count": None, "is_dark": None, "quota": None}


def measure(site_id, dark):
    return {"record_type": "现场实测", "site_id": site_id, "district": None,
            "site_name": None, "fixture_count": None, "incident_id": None,
            "report_date": None, "is_dark": "是" if dark else "否", "quota": None}


def quota(day, value):
    return {"record_type": "夜巡名额", "site_id": "全市", "district": None,
            "site_name": None, "fixture_count": None, "incident_id": None,
            "report_date": f"2026-11-{day}", "is_dark": None, "quota": value}


def base_rows():
    return [
        register("LH-01", "滨河片区", "柳汀大道北段", 32),
        register("LH-02", "老城片区", "东门巷", 5),
        register("LH-04", "滨河片区", "长堤辅道", 3),
        register("LH-06", "滨河片区", "渡口支路", 9),
        ticket("LH-01", "T-01", 14), ticket("LH-01", "T-02", 14),
        ticket("LH-02", "T-03", 14),
        ticket("LH-04", "T-04", 16), ticket("LH-04", "T-05", 20),
        measure("LH-01", False), measure("LH-02", True),
        measure("LH-04", True), measure("LH-06", True),
        quota(14, 1), quota(15, 1), quota(16, 1),
        quota(17, 1), quota(18, 1), quota(19, 1), quota(20, 1),
    ]


def test_prepare_includes_unequeued_sites():
    snapshot = backtest.prepare(demo_rows(), PARAMS)
    # 零工单、未入列的灯组同样保留在分母并要求有实测。
    assert len(snapshot["sites"]) == 10
    assert len(snapshot["measurements"]) == 10
    assert snapshot["dark_sites"] == {"LH-02", "LH-04", "LH-05", "LH-06"}
    assert snapshot["nights"][0] == "2026-11-14" and snapshot["nights"][-1] == "2026-11-20"
    assert all(snapshot["quotas"][night] == 2 for night in snapshot["nights"])


def test_missing_measurement_rejected():
    rows = base_rows()
    rows = [row for row in rows if not (row["record_type"] == "现场实测" and row["site_id"] == "LH-06")]
    try:
        backtest.prepare(rows, PARAMS)
    except ValueError as error:
        assert "LH-06" in str(error) and "现场实测" in str(error)
    else:
        raise AssertionError("缺少实测应报错")


def test_missing_quota_rejected():
    rows = [row for row in base_rows() if not (row["record_type"] == "夜巡名额" and row["report_date"] == "2026-11-16")]
    try:
        backtest.prepare(rows, PARAMS)
    except ValueError as error:
        assert "2026-11-16" in str(error) and "名额" in str(error)
    else:
        raise AssertionError("缺少名额应报错")


def test_zero_quota_rejected():
    rows = [quota(14, 0) if (row["record_type"] == "夜巡名额" and row["report_date"] == "2026-11-14") else row
            for row in base_rows()]
    try:
        backtest.prepare(rows, PARAMS)
    except ValueError as error:
        assert "名额" in str(error)
    else:
        raise AssertionError("零名额应报错")


def test_inverted_dates_rejected():
    try:
        backtest.prepare(base_rows(), {"start_date": "2026-11-20", "end_date": "2026-11-14"})
    except ValueError as error:
        assert "起日" in str(error)
    else:
        raise AssertionError("倒置日期应报错")


def test_district_without_dark_zone_has_zero_rate():
    rows = base_rows()
    # 老城片区唯一暗区 LH-02 改为非暗区，则老城无实测暗区。
    for row in rows:
        if row["record_type"] == "现场实测" and row["site_id"] == "LH-02":
            row["is_dark"] = "否"
    snapshot = backtest.prepare(rows, PARAMS)
    thresholds = {
        "滨河片区": {"min_incidents": 99, "min_rate": Fraction(9999)},
        "老城片区": {"min_incidents": 99, "min_rate": Fraction(9999)},
    }
    outcome = backtest.replay(snapshot, thresholds)
    assert outcome["district_rates"]["老城片区"] == 0.0
    # 滨河有两个暗区（LH-04、LH-06）且永不派巡，每组漏 7 个夜班，漏报率为 1。
    assert outcome["district_rates"]["滨河片区"] == 1.0


def test_replay_tracks_missed_dark_zones_nightly():
    snapshot = backtest.prepare(base_rows(), PARAMS)
    thresholds = {
        "滨河片区": {"min_incidents": 1, "min_rate": Fraction(0)},
        "老城片区": {"min_incidents": 1, "min_rate": Fraction(0)},
    }
    outcome = backtest.replay(snapshot, thresholds)
    by_night = {row["night"]: row for row in outcome["night_rows"]}
    # 名额每晚仅 1：14 日 LH-02（rate 20）与 LH-01（rate 6.25）达标，LH-02 优先入列。
    assert by_night["2026-11-14"]["queued_site_ids"] == ["LH-02"]
    assert by_night["2026-11-14"]["missed_dark_site_ids"] == ["LH-04", "LH-06"]
    # LH-06 零工单永远无法达标，七个夜班都漏掉。
    assert all("LH-06" in row["missed_dark_site_ids"] for row in outcome["night_rows"])
    # LH-04 在 16 日累计 1 单、rate 33.33，当晚名额内入列后不再计入漏掉。
    assert by_night["2026-11-16"]["queued_site_ids"] == ["LH-04"]
    assert all("LH-04" not in row["missed_dark_site_ids"]
               for row in outcome["night_rows"] if row["night"] >= "2026-11-16")


def test_optimization_minimizes_max_district_rate_then_total():
    result = backtest.run_backtest(base_rows(), PARAMS)
    objective = result["objective"]
    # 最优解必须让最高片区漏报率尽可能低，再让总漏掉组夜最少。
    snapshot = backtest.prepare(base_rows(), PARAMS)
    candidates = backtest.all_candidates(snapshot)
    for thresholds in candidates:
        outcome = backtest.replay(snapshot, thresholds)
        assert (outcome["max_miss_rate"], outcome["missed_night_total"]) >= (
            objective["max_miss_rate"], objective["missed_night_total"])


def test_demo_backtest_publishes_single_threshold_pair_per_district():
    result = backtest.run_backtest(demo_rows(), PARAMS)
    threshold_table = result["tables"][0]
    assert [row["district"] for row in threshold_table["rows"]] == ["滨河片区", "老城片区"]
    assert all(row["min_incidents"] >= 0 for row in threshold_table["rows"])
    # 回测页面必须包含门槛表和逐夜漏掉暗区表。
    assert result["tables"][1]["title"] == "逐夜派巡与漏掉的暗区"
    night_table = result["tables"][1]
    assert [row["night"] for row in night_table["rows"]] == [
        f"2026-11-{day}" for day in range(14, 21)]
    assert all("missed_dark_count" in row for row in night_table["rows"])
    # 最优解在名额约束下尽快派出暗区：零工单暗区 LH-06 于 16 日入列，此前两晚漏掉。
    site_rows = {row["site_id"]: row for row in result["tables"][2]["rows"]}
    assert site_rows["LH-06"]["dispatch_night"] == "2026-11-16"
    early_missed = [row for row in night_table["rows"] if row["night"] < "2026-11-16"]
    assert all("LH-06" in row["missed_dark_site_ids"].split("、") for row in early_missed)
    later_missed = [row for row in night_table["rows"] if row["night"] >= "2026-11-16"]
    assert all("LH-06" not in row["missed_dark_site_ids"].split("、") for row in later_missed)


def test_resume_reuses_evaluated_cache_and_publishes_once():
    # 模拟服务层分批：切分候选后逐批评估，缓存命中的候选不重算。
    snapshot = backtest.prepare(base_rows(), PARAMS)
    candidates = backtest.all_candidates(snapshot)
    evaluated = {}
    best = None
    batch = 7
    for start in range(0, len(candidates), batch):
        for thresholds in candidates[start:start + batch]:
            signature = backtest.candidate_signature(snapshot, thresholds)
            assert signature not in evaluated  # 每个候选只评估一次
            outcome = backtest.replay(snapshot, thresholds)
            # 与服务层一致，缓存仅保存目标比较所需的紧凑指标。
            evaluated[signature] = {
                "district_rates": outcome["district_rates"],
                "missed_night_total": outcome["missed_night_total"],
                "max_miss_rate": outcome["max_miss_rate"],
            }
            candidate = {"thresholds": thresholds, "outcome": outcome, "signature": signature}
            if best is None or backtest.better(snapshot, best, candidate):
                best = candidate
    one_shot = backtest.run_backtest(base_rows(), PARAMS)
    assert len(evaluated) == one_shot["objective"]["candidate_total"]
    assert best["outcome"]["max_miss_rate"] == one_shot["objective"]["max_miss_rate"]
    assert best["outcome"]["missed_night_total"] == one_shot["objective"]["missed_night_total"]


def test_json_roundtrip_preserves_fractions():
    snapshot = backtest.prepare(base_rows(), PARAMS)
    candidates = backtest.all_candidates(snapshot)
    restored_snapshot = backtest.from_jsonable(backtest.to_jsonable(snapshot))
    restored_candidates = backtest.from_jsonable(backtest.to_jsonable(candidates))
    assert restored_snapshot["districts"] == snapshot["districts"]
    before = backtest.replay(snapshot, candidates[10])
    after = backtest.replay(restored_snapshot, restored_candidates[10])
    assert before == after


def test_candidate_grid_has_stop_threshold():
    snapshot = backtest.prepare(base_rows(), PARAMS)
    grids = backtest.candidate_grids(snapshot)
    totals = {}
    for ticket_night in snapshot["tickets_by_night"].values():
        for ticket in ticket_night:
            district = snapshot["sites"][ticket["site_id"]]["district"]
            totals[district] = totals.get(district, 0) + 1
    for district, options in grids.items():
        max_count = max(option["min_incidents"] for option in options)
        # 停派档高于该片区任何灯组的累计单数；用单灯组最高累计值作下界校验。
        site_max = max(
            sum(1 for ticket_night in snapshot["tickets_by_night"].values()
                for ticket in ticket_night if ticket["site_id"] == site_id)
            for site_id, site in snapshot["sites"].items() if site["district"] == district
        )
        assert max_count > site_max


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
