"""Worker 回测排干调度测试：只验证领取/续算/接手序列，不依赖数据库。"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main():
    events = []
    active = {1: True}

    def package(name, path):
        module = types.ModuleType(name)
        module.__path__ = [path]
        return module

    src = package("src", str(ROOT / "src"))
    config = package("src.config", str(ROOT / "src" / "config"))
    database = types.ModuleType("src.config.database")

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, job_id):
            class Job:
                status = "running" if active.get(job_id) else "complete"

            return Job()

    database.Session = FakeSession
    constants = package("src.constants", str(ROOT / "src" / "constants"))
    enums = types.ModuleType("src.constants.enums")

    class FakeStatus:
        RUNNING = "running"

    enums.Status = FakeStatus
    models = package("src.models", str(ROOT / "src" / "models"))
    jobs = types.ModuleType("src.models.jobs")

    class Backtest:
        pass

    jobs.Backtest = Backtest
    services = package("src.services", str(ROOT / "src" / "services"))
    backtests_svc = types.ModuleType("src.services.backtests")

    def fake_claim():
        events.append("claim")
        return (1, 1) if active.get(1) else None

    def fake_process(lease):
        events.append(f"process:{lease[0]}:{lease[1]}")
        # 前两批任务仍在运行，第三批后任务完成。
        if events.count("process:1:1") == 3:
            active[1] = False

    backtests_svc.claim_backtest = fake_claim
    backtests_svc.process_backtest = fake_process
    execution = types.ModuleType("src.services.execution")
    execution.claim = lambda: None
    execution.execute = lambda lease: None
    execution.materialize_due = lambda: None
    utils = package("src.utils", str(ROOT / "src" / "utils"))
    wakeup = types.ModuleType("src.utils.wakeup")
    wakeup.wait = lambda: None

    sys.modules.update({
        "src": src, "src.config": config, "src.config.database": database,
        "src.constants": constants, "src.constants.enums": enums,
        "src.models": models, "src.models.jobs": jobs,
        "src.services": services, "src.services.backtests": backtests_svc,
        "src.services.execution": execution,
        "src.utils": utils, "src.utils.wakeup": wakeup,
    })

    worker = __import__("src.worker", fromlist=["drain_backtests"])
    worker.drain_backtests()

    failures = []
    expected = ["claim", "process:1:1", "process:1:1", "process:1:1", "claim"]
    print("调度序列:", events)
    if events != expected:
        failures.append(f"序列不符：{events} != {expected}")
    # 同一租约连续处理三批，只在开始和结束各领取一次，中断后才会重新领取。
    if events.count("claim") != 2:
        failures.append("应只在首尾各领取一次")

    if failures:
        for failure in failures:
            print("FAIL", failure)
        print("0/1 passed")
        return 1
    print("1/1 passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
