import {datasetField, feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

function backtestRequest(data, spec) {
  return {
    dataset_id: Number(data.get('dataset_id')),
    name: data.get('name'),
    params: Object.fromEntries(spec.backtest_params.map(param => [param.key, data.get(param.key)])),
  };
}

function backtestForm(spec, datasets, route, refresh, message) {
  const submit = button('开始回测', {type:'submit', class:'primary', 'data-testid':'start-backtest'});
  return el('form', {class:'fields', 'data-testid':'backtest-form',
    onSubmit: handler(submit, message, async event => {
      const item = await workspace.startBacktest(backtestRequest(new FormData(event.target), spec));
      location.hash = `#backtests/${item.id}`;
    })},
    datasetField(datasets, route.id),
    field({key:'name', label:'回测名称', type:'string', max_length:120}, '派巡门槛回测'),
    el('div', {class:'form-grid'}, spec.backtest_params.map(param => field(param))),
    el('div', {class:'actions'}, submit));
}

function resultView(job, spec, message) {
  const result = job.result;
  const download = button('下载回测 CSV', {class:'quiet', 'data-testid':'backtest-export',
    onClick: event => handler(download, message, () =>
      workspace.download(`/backtests/${job.id}/export`, `${spec.short_name}-backtest-${job.id}.csv`))(event)});
  return el('section', {class:'analysis', 'data-testid':'backtest-complete'},
    el('div', {class:'result-head'}, p(job.name, 'eyeline'), h(2, result.headline), p(result.note, 'intro-copy'),
      el('div', {class:'actions'}, pill(STATUS[job.status], job.status),
        job.published_at && pill('已发布唯一门槛', 'complete'), download, message)),
    metrics(result.metrics),
    (result.sections || []).map(item => panel(item.title, p(item.text))),
    result.tables.map(item => panel(item.title, table(item.columns, item.rows,
      {caption: item.title, pageSize: 14}))));
}

export async function backtestPage(route, refresh) {
  const {spec, datasets, backtests} = workspace;
  const message = feedback();
  const selectedId = route.id || backtests[0]?.id;
  const methodPanel = panel('回测口径', el('ul', {class:'method-list'},
    spec.methodology.slice(-4).map(text => el('li', {}, text))));
  const formPanel = session.canWrite && panel(`发起${spec.backtest_run}`,
    datasets.length ? backtestForm(spec, datasets, route, refresh, message)
      : empty('请先导入含实测和名额的数据。', '#inputs', '前往导入'), message);

  if (!selectedId) {
    return el('div', {class:'two-col'},
      el('div', {class:'wide-panel'}, formPanel || p('只读角色可以查看已发布的回测门槛。', 'empty')),
      el('aside', {class:'sidebar-panel'}, methodPanel));
  }

  const job = await workspace.getBacktest(selectedId);
  const finished = item => item.status === 'complete' || item.status === 'failed';
  if (!finished(job)) {
    const original = location.hash;
    const status = el('p', {role:'status'},
      `后台正在评估候选门槛（${job.evaluated_total}/${job.candidate_total || '…'}），完成后自动展示。`);
    poll(() => workspace.getBacktest(selectedId), finished, () => location.hash === original)
      .then(result => {
        if (result && status.isConnected && location.hash === original) refresh();
        else if (status.isConnected) status.textContent = '仍在执行，请点击刷新检查。';
      })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
    return panel(job.name, pill(STATUS[job.status], job.status), status,
      p(`已评估 ${job.evaluated_total} / ${job.candidate_total} 套候选，进度 ${value(job.progress_pct)}%`, 'subtle'),
      button('刷新', {class:'quiet', onClick: refresh}));
  }

  const history = el('label', {class:'field run-picker', for:'backtest-picker'}, el('span', {}, '切换回测记录'),
    el('select', {id:'backtest-picker', onChange: event => { location.hash = `#backtests/${event.target.value}`; }},
      (backtests.some(item => item.id === job.id) ? backtests : [job, ...backtests]).map(item =>
        el('option', {value:item.id, selected:item.id === job.id}, item.name))));

  const detail = job.result
    ? resultView(job, spec, message)
    : panel(job.name, pill(STATUS[job.status], job.status), p(job.error, 'feedback'),
      link('返回调整回测区间', '#backtests', {class:'quiet'}));

  const inputs = spec.backtest_params.map(param =>
    el('div', {}, el('dt', {}, param.label), el('dd', {}, value(job.params[param.key]))));

  const historyList = panel('回测记录', table([
    {label:'回测', render:row => link(row.name, `#backtests/${row.id}`)},
    {label:'状态', render:row => pill(STATUS[row.status], row.status)},
    {label:'进度', render:row => row.status === 'complete'
      ? '已发布' : `${value(row.progress_pct)}%（${row.evaluated_total}/${row.candidate_total}）`},
  ], backtests, {caption:'回测记录', empty:'首次回测后将在此显示。'}));

  return el('div', {}, el('div', {class:'two-col'},
    el('div', {class:'wide-panel'}, detail, formPanel),
    el('aside', {class:'sidebar-panel'},
      panel('回测来源', history,
        link('查看数据批次', `#inputs/${job.dataset_id}`, {class:'quiet'}),
        p(date(job.finished_at), 'subtle'),
        el('dl', {class:'dictionary'}, inputs)),
      historyList,
      methodPanel)));
}
