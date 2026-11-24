import logging
import time
from pathlib import Path

from src.config.database import Session
from src.constants.enums import Status
from src.models.jobs import Backtest
from src.services.backtests import claim_backtest, process_backtest
from src.services.execution import claim, execute, materialize_due
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def is_active(job_id):
    with Session() as db:
        job = db.get(Backtest, job_id)
        return bool(job and job.status == Status.RUNNING)


def drain_backtests():
    """连续处理回测批次，直到没有进行中的回测任务。

    首次领取建立租约；同一 worker 持有租约期间直接按租约续算后续批次，
    进程中断后租约到期，再由任意 worker 通过 claim_backtest 接手。
    """
    lease = claim_backtest()
    while lease:
        job_id, attempt = lease
        process_backtest(lease)
        heartbeat.touch()
        # 任务仍在运行说明还有候选，沿用同一租约继续下一批；否则尝试领取其他任务。
        lease = (job_id, attempt) if is_active(job_id) else claim_backtest()


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            heartbeat.touch()
            if lease:
                execute(lease)
                heartbeat.touch()
                continue
            # 回测按候选分批执行：同一轮内连续领批，中断（进程退出或租约过期）
            # 后由新循环或并发 worker 沿用冻结快照续算，已评候选不重算。
            drain_backtests()
            wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
