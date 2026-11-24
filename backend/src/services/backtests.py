"""派巡门槛回测的参数规范化、租约执行与单套门槛发布。"""

import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select

from src.config.database import Session
from src.constants.enums import Status
from src.domain import backtest
from src.domain.spec import SPEC
from src.models.jobs import Backtest
from src.models.source import Dataset
from src.services.audit import record
from src.services.validation import cell
from src.utils.exports import write_backtest_report
from src.utils.time import now

log = logging.getLogger(__name__)


def normalize_backtest_params(params):
    if not isinstance(params, dict):
        raise ValueError("回测参数格式有误。")
    fields = SPEC["backtest_params"]
    allowed = {field["key"] for field in fields}
    if set(params) - allowed:
        raise ValueError("存在未知回测参数，请重新打开回测面板。")
    normalized = {}
    for field in fields:
        value = params.get(field["key"], field["default"])
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"「{field['label']}」不能为空，请填写有效日期。")
        normalized[field["key"]] = cell(field, value)
    return normalized


def claim_backtest():
    """领取一个排队中或租约过期的回测，冻结快照并枚举候选。"""
    stamp = now()
    with Session.begin() as db:
        query = select(Backtest).where(
            or_(Backtest.status == Status.QUEUED,
                and_(Backtest.status == Status.RUNNING, Backtest.lease_until < stamp))
        )
        job = db.scalar(query.order_by(Backtest.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = "后台进程多次中断，请发起新的回测"
            job.finished_at = stamp
            record(db, "worker", "backtest.failed", job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        # 新租约覆盖整批处理窗口；正常完成一批后会在 process_backtest 中续租。
        job.lease_until = stamp + timedelta(minutes=3)

        if job.snapshot is None:
            # 开算时冻结工单、实测和名额；之后中断恢复全部沿用这份快照。
            rows = db.get(Dataset, job.dataset_id).rows
            try:
                snapshot = backtest.prepare(rows, job.params)
                candidates = backtest.all_candidates(snapshot)
            except ValueError as error:
                job.lease_until = None
                job.finished_at = now()
                job.status = Status.FAILED
                job.error = str(error)[:500]
                record(db, "worker", "backtest.failed", job.id, reason=job.error)
                return None
            job.snapshot = backtest.to_jsonable(snapshot)
            job.candidates = backtest.to_jsonable(candidates)
            job.candidate_total = len(candidates)
            job.evaluated = {}
            job.next_index = 0
            job.best = None
            record(db, "worker", "backtest.started", job.id, candidates=job.candidate_total)
        return job.id, job.attempt


def process_backtest(lease):
    """在单个租约内评估一批候选，保持已评缓存并在全部完成时发布。"""
    job_id, attempt = lease
    failure = None
    try:
        with Session.begin() as db:
            job = db.get(Backtest, job_id, with_for_update=True)
            if job.attempt != attempt or job.status != Status.RUNNING:
                return
            snapshot = backtest.from_jsonable(job.snapshot)
            candidates = backtest.from_jsonable(job.candidates)
            evaluated = backtest.from_jsonable(job.evaluated)
            best = backtest.from_jsonable(job.best)
            start_index = job.next_index

            done = 0
            index = start_index
            while index < len(candidates) and done < backtest.BATCH_SIZE:
                thresholds = candidates[index]
                signature = backtest.candidate_signature(snapshot, thresholds)
                if signature not in evaluated:
                    outcome = backtest.replay(snapshot, thresholds)
                    # 缓存只保留目标比较所需的紧凑指标，逐夜轨迹仅随最优候选保留，
                    # 既不重算已评候选，也避免 JSON 列随候选数膨胀。
                    compact = {
                        "district_rates": outcome["district_rates"],
                        "missed_night_total": outcome["missed_night_total"],
                        "max_miss_rate": outcome["max_miss_rate"],
                    }
                    evaluated[signature] = compact
                    candidate = {"thresholds": thresholds, "outcome": outcome, "signature": signature}
                    if best is None or backtest.better(snapshot, best, candidate):
                        best = candidate
                    done += 1
                index += 1

            job.evaluated = backtest.to_jsonable(evaluated)
            job.best = backtest.to_jsonable(best)
            job.next_index = index

            if index < len(candidates):
                # 本批完成但仍有候选：续租保持 RUNNING，worker 下一轮可直接领取；
                # 若进程中断，租约到期后由并发接手沿用同一份冻结快照续算。
                job.lease_until = now() + timedelta(minutes=3)
                record(db, "worker", "backtest.progress", job.id,
                       evaluated=len(evaluated), total=len(candidates))
                return

            result = backtest.build_result(snapshot, best, len(candidates), len(evaluated))
            export = write_backtest_report(job.id, job.attempt, result)
            job.result = result
            job.export_file = export
            job.status = Status.COMPLETE
            job.finished_at = now()
            job.lease_until = None
            # 并发接手也只发布一套门槛：完成时刻一次性写入发布时间。
            job.published_at = job.finished_at
            record(db, "worker", "backtest.complete", job.id,
                   candidates=len(candidates), missed=result["objective"]["missed_night_total"])
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception("Backtest %s failed", job_id)
        failure = "回测失败，请检查实测、名额和日期口径后重试"

    if failure:
        with Session.begin() as db:
            job = db.get(Backtest, job_id, with_for_update=True)
            if job.attempt != attempt or job.status != Status.RUNNING:
                return
            job.lease_until = None
            job.finished_at = now()
            job.status = Status.FAILED
            job.error = failure
            record(db, "worker", "backtest.failed", job_id, reason=failure)
