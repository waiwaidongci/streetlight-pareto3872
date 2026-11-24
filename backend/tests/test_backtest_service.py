"""回测服务层测试：用标准库内存桩验证租约、恢复与单套发布。

不依赖 SQLAlchemy/PostgreSQL，直接运行：python3 tests/test_backtest_service.py
"""

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from src.domain import backtest  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402

PARAMS = {"start_date": "2026-11-14", "end_date": "2026-11-20"}


# ---- 标准库桩：替换服务层依赖的第三方/基础设施模块 --------------------

class FakeStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class _Column:
    def __eq__(self, other):
        return ("eq", other)

    def __lt__(self, other):
        return ("lt", other)

    def __gt__(self, other):
        return ("gt", other)

    def in_(self, values):
        return ("in", tuple(values))


class FakeBacktest:
    _seq = 0

    # SQLAlchemy 模型类上的列描述符，仅用于服务层构造查询表达式。
    status = _Column()
    lease_until = _Column()
    id = _Column()
    dataset_id = _Column()
    params = _Column()

    def __init__(self, name, dataset_id, params, author="tester"):
        FakeBacktest._seq += 1
        self.id = FakeBacktest._seq
        self.name = name
        self.dataset_id = dataset_id
        self.status = FakeStatus.QUEUED
        self.params = params
        self.snapshot = None
        self.candidates = None
        self.evaluated = {}
        self.best = None
        self.next_index = 0
        self.candidate_total = 0
        self.result = None
        self.export_file = None
        self.error = None
        self.attempt = 0
        self.lease_until = None
        self.published_at = None
        self.author = author
        self.created_at = datetime(2026, 11, 21, 8, 0)
        self.finished_at = None


class FakeDataset:
    def __init__(self, rows):
        self.rows = rows


class FakeSession:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def scalar(self, _query):
        # 忽略 SQL 构造，按服务层排序语义返回最早的可领取任务。
        for job in sorted(self.store.backtests, key=lambda item: item.id):
            stale = job.status == FakeStatus.RUNNING and (
                job.lease_until is None or job.lease_until < self.store.clock)
            if job.status == FakeStatus.QUEUED or stale:
                return job
        return None

    def get(self, model, key, with_for_update=False):
        if model is FakeBacktest:
            return next((job for job in self.store.backtests if job.id == key), None)
        if model is FakeDataset:
            return self.store.dataset
        return None

    def add(self, item):
        if isinstance(item, FakeBacktest) and item not in self.store.backtests:
            self.store.backtests.append(item)


class Store:
    def __init__(self, rows):
        self.backtests = []
        self.dataset = FakeDataset(rows)
        self.clock = datetime(2026, 11, 21, 8, 0)
        self.audit = []
        self.exports = []


class FakeSessionMaker:
    def __init__(self, store):
        self.store = store

    def begin(self):
        return FakeSession(self.store)


def install_stubs(store):
    src_dir = str(ROOT / "src")
    src = types.ModuleType("src")
    src.__path__ = [src_dir]
    config = types.ModuleType("src.config")
    config.__path__ = [str(ROOT / "src" / "config")]
    database = types.ModuleType("src.config.database")
    database.Session = FakeSessionMaker(store)
    constants = types.ModuleType("src.constants")
    constants.__path__ = [str(ROOT / "src" / "constants")]
    enums = types.ModuleType("src.constants.enums")
    enums.Status = FakeStatus
    models = types.ModuleType("src.models")
    models.__path__ = [str(ROOT / "src" / "models")]
    jobs = types.ModuleType("src.models.jobs")
    jobs.Backtest = FakeBacktest
    source = types.ModuleType("src.models.source")
    source.Dataset = FakeDataset
    services = types.ModuleType("src.services")
    services.__path__ = [str(ROOT / "src" / "services")]
    audit_mod = types.ModuleType("src.services.audit")
    audit_mod.record = lambda db, actor, event, target, **info: store.audit.append((event, target, info))
    validation = types.ModuleType("src.services.validation")

    def cell(field, value):
        # 与真实 cell 的日期分支保持一致的最小实现。
        from datetime import date
        return date.fromisoformat(str(value)).isoformat()

    validation.cell = cell
    utils = types.ModuleType("src.utils")
    utils.__path__ = [str(ROOT / "src" / "utils")]
    exports_mod = types.ModuleType("src.utils.exports")

    def write_backtest_report(backtest_id, attempt, result):
        name = f"backtest-{backtest_id}-{attempt}.csv"
        store.exports.append((name, result))
        return name

    exports_mod.write_backtest_report = write_backtest_report
    time_mod = types.ModuleType("src.utils.time")
    time_mod.now = lambda: store.clock

    sqlalchemy = types.ModuleType("sqlalchemy")

    class FakeQuery:
        def where(self, *a):
            return self

        def order_by(self, *a):
            return self

        def limit(self, *a):
            return self

        def with_for_update(self, *a, **k):
            return self

    sqlalchemy.select = lambda *a: FakeQuery()
    sqlalchemy.and_ = lambda *a: ("and", a)
    sqlalchemy.or_ = lambda *a: ("or", a)

    sys.modules.update({
        "src": src, "src.config": config, "src.config.database": database,
        "src.constants": constants, "src.constants.enums": enums,
        "src.models": models, "src.models.jobs": jobs, "src.models.source": source,
        "src.services": services, "src.services.audit": audit_mod,
        "src.services.validation": validation,
        "src.utils": utils, "src.utils.exports": exports_mod, "src.utils.time": time_mod,
        "sqlalchemy": sqlalchemy,
    })


def main():
    store = Store(demo_rows())
    install_stubs(store)

    # 延迟导入，确保桩先生效。
    from src.services import backtests as service  # noqa: E402

    failures = []

    def check(name, condition):
        print(("PASS " if condition else "FAIL ") + name)
        if not condition:
            failures.append(name)

    # 1. 领取新任务时冻结快照与候选，并记录 started 审计。
    job = FakeBacktest("回测演示", 1, dict(PARAMS))
    store.backtests.append(job)
    lease = service.claim_backtest()
    check("claim 返回租约", lease == (job.id, 1))
    check("领取后状态为 RUNNING", job.status == "running")
    check("开算即冻结快照", job.snapshot is not None and len(job.snapshot["sites"]) == 10)
    check("开算即枚举候选", job.candidate_total == len(backtest.all_candidates(
        backtest.prepare(demo_rows(), PARAMS))))
    check("started 审计已记录", any(event == "backtest.started" for event, _, _ in store.audit))

    # 2. 统计 replay 调用，验证分批恢复中已评候选不重算。
    original_replay = backtest.replay
    replay_calls = []

    def counting_replay(snapshot, thresholds):
        replay_calls.append(backtest.candidate_signature(snapshot, thresholds))
        return original_replay(snapshot, thresholds)

    backtest.replay = counting_replay
    try:
        batches = 0
        while job.status == "running":
            lease = (job.id, job.attempt)
            service.process_backtest(lease)
            batches += 1
            if batches > 200:
                break
    finally:
        backtest.replay = original_replay

    check("分多批执行", batches > 1)
    check("每个候选只 replay 一次", len(replay_calls) == len(set(replay_calls)) == job.candidate_total)
    check("最终状态 COMPLETE", job.status == "complete")
    check("只发布一次（published_at 唯一写入）", job.published_at == store.clock and job.finished_at is not None)
    check("结果包含门槛与逐夜漏掉暗区表",
          job.result["tables"][0]["title"] == "选中的固定门槛（按片区）"
          and job.result["tables"][1]["title"] == "逐夜派巡与漏掉的暗区")
    check("导出文件已生成", len(store.exports) == 1 and job.export_file == store.exports[0][0])
    check("complete 审计已记录", any(event == "backtest.complete" for event, _, _ in store.audit))

    # 3. 中断恢复：处理到一半租约过期，新的领取沿用同一快照，不重新冻结。
    store2 = Store(demo_rows())
    install_stubs(store2)
    import importlib
    service2 = importlib.reload(service)
    job2 = FakeBacktest("中断恢复", 1, dict(PARAMS))
    store2.backtests.append(job2)
    service2.claim_backtest()
    frozen_snapshot = job2.snapshot
    frozen_candidates = job2.candidates
    service2.process_backtest((job2.id, 1))
    progress_index = job2.next_index
    check("首批处理后仍有剩余候选", 0 < progress_index < job2.candidate_total)
    # 模拟租约过期后被另一进程接手。
    store2.clock += timedelta(minutes=10)
    job2.lease_until = store2.clock - timedelta(minutes=5)
    reclaimed = service2.claim_backtest()
    check("过期租约可被接手", reclaimed == (job2.id, 2))
    check("恢复沿用开算时快照", job2.snapshot is frozen_snapshot and job2.candidates is frozen_candidates)
    while job2.status == "running":
        service2.process_backtest((job2.id, job2.attempt))
    check("恢复后完成且结果一致",
          job2.status == "complete"
          and job2.result["objective"] == job.result["objective"])
    check("恢复未重算已评候选（缓存沿用）",
          len(job2.evaluated) == job2.candidate_total)
    check("缓存只存紧凑指标，完整轨迹仅随最优候选",
          all(set(item) == {"district_rates", "missed_night_total", "max_miss_rate"}
              for item in job2.evaluated.values())
          and "night_rows" in job2.best["outcome"])

    # 4. 坏数据在冻结快照阶段失败，任务标记 FAILED，不发布门槛。
    store3 = Store(demo_rows())
    install_stubs(store3)
    service3 = importlib.reload(service)
    bad_job = FakeBacktest("坏区间", 1, {"start_date": "2026-11-20", "end_date": "2026-11-14"})
    store3.backtests.append(bad_job)
    bad_lease = service3.claim_backtest()
    check("坏区间领取即失败且无租约", bad_lease is None)
    check("坏区间标记 FAILED", bad_job.status == "failed" and bad_job.error and "起日" in bad_job.error)
    check("失败不发布门槛", bad_job.published_at is None and bad_job.result is None)

    print(f"\n{len(replay_calls)} 次 replay / {job.candidate_total} 个候选，共 {batches} 批")
    total_checks = 21
    print(f"{total_checks - len(failures)}/{total_checks} passed" if not failures
          else f"{total_checks - len(failures)}/{total_checks} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
