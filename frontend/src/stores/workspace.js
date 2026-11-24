import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  backtests: [],
  async init() { this.spec = await request('/spec'); },
  async refresh() {
    [this.datasets, this.runs, this.backtests] =
      await Promise.all([call('/datasets'), call('/runs'), call('/backtests')]);
  },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  getBacktest: id => call(`/backtests/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  startBacktest: data => call('/backtests', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  download: (path, filename) => saveFile(path, session.token, filename),
};
