from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.config.settings import ARTIFACTS
from src.constants.enums import Role, Status
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.jobs import Backtest
from src.models.source import Dataset
from src.schemas.requests import BacktestRequest
from src.services.audit import record
from src.services.backtests import normalize_backtest_params
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/backtests')


def require_backtest(db, backtest_id):
    item = db.get(Backtest, backtest_id)
    if not item:
        raise HTTPException(404, '找不到该回测记录')
    return item


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(Backtest).order_by(Backtest.id.desc()).limit(100))
    return [views.backtest(item) for item in items]


@router.post('', status_code=202)
def create(body: BacktestRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    # 同一批次、同一回测参数只保留一个活跃回测，避免并发重复发起发布多套门槛。
    params = normalize_backtest_params(body.params)
    active_items = db.scalars(select(Backtest).where(
        Backtest.dataset_id == body.dataset_id,
        Backtest.status.in_([Status.QUEUED, Status.RUNNING]),
    ).order_by(Backtest.id.desc()))
    # JSON 列不在 SQL 内比较，取回后在 Python 中按规范化参数判定。
    if any(item.params == params for item in active_items):
        raise HTTPException(409, '同一批次与回测区间已有进行中的回测，完成后只发布一套门槛')
    item = Backtest(name=body.name, dataset_id=body.dataset_id, params=params,
                    evaluated={}, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'backtest.queued', item.id, dataset_id=item.dataset_id)
    db.commit()
    wake()
    return views.backtest(item, detail=True)


@router.get('/{backtest_id}')
def detail(backtest_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    return views.backtest(require_backtest(db, backtest_id), detail=True)


@router.get('/{backtest_id}/export')
def export(backtest_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = require_backtest(db, backtest_id)
    if not item.export_file:
        raise HTTPException(409, '此回测尚无可下载结果')
    path = ARTIFACTS / item.export_file
    if not path.is_file():
        raise HTTPException(410, '导出文件已不可用，请重新发起回测')
    return FileResponse(path, media_type='text/csv',
                        filename=f'{SPEC["short_name"]}-backtest-{item.id}.csv')
