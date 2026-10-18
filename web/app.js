import { createMockApi } from './js/mock-api.js';
import { decimal, duration, deltaDuration, receiveClock, connectionState, csvText, TARGET_MS, POLL_MS } from './js/format.js';
import { courseMapPoint, drawCourse, drawSpeedChart } from './js/charts.js';
import { initializeStrategy } from './js/strategy-ui.js';
import { ApiError, createTelemetryApi, readViewerToken, retryDelay, snapshot, toTokyoInput, fromTokyoInput } from './js/telemetry-api.js';

const $ = id => document.getElementById(id);
let api;
let course;
let response = null;
let requestError = null;
let timer;
let controller;
let requestVersion = 0;
let chartRange = 300;
let previousStatus = '';
let historySignature = '';
let strategyUi;
const demoMode = new URLSearchParams(location.search).get('demo') === '1';
let archiveMode = false;
let archiveSnapshots = [];
let failures = 0;
let historyLoading = false;

function setText(id, value) { if ($(id).textContent !== String(value)) $(id).textContent = String(value); }

function stateIsOld() { return !['live', 'finished'].includes(connectionState({ response, error: requestError })); }

function stopRequests() {
  clearTimeout(timer);
  controller?.abort();
  requestVersion++;
  if (historyLoading) {
    historyLoading = false;
    $('history-load').disabled = false;
    requestError = new ApiError('interrupted');
    setText('history-feedback', '取得を中断しました。「記録を表示」で再取得できます。');
  }
}

function modeLabels() {
  setText('mode-label', demoMode ? 'DEMO' : archiveMode ? '過去データ' : '走行データ');
  setText('mode-description', demoMode ? '模擬データで動作中' : archiveMode ? '指定した期間の受信記録' : 'delta-vegaの受信データ');
  setText('session-caption', demoMode ? 'テスト走行' : archiveMode ? '過去の受信記録' : '最新の受信記録');
  document.querySelector('.poll-note').textContent = archiveMode ? '過去の記録・自動更新なし' : '3秒ごとに自動更新';
  $('mode-label').dataset.source = demoMode ? 'demo' : 'live';
}

function renderConnection() {
  const state = archiveMode && !historyLoading && !response && !requestError ? 'select-period' : connectionState({ response, error: requestError });
  const age = response?.latest ? Math.max(0, Math.floor((Date.now() + (response.clock_offset_ms || 0) - response.received_at_ms) / 1000)) : null;
  const labels = { loading: '読み込み中', 'select-period': '期間を選択', live: 'LIVE · 通信正常', stale: '更新停止', error: '取得エラー', empty: 'データ待ち', finished: 'FINISHED', locked: '共有リンクが必要', unauthorized: 'リンクを確認', archived: '過去の記録' };
  $('connection-status').dataset.status = state;
  // Only announce state transitions, not the ticking data age.
  if (state !== previousStatus) { setText('connection-label', labels[state]); previousStatus = state; }
  setText('last-update', state === 'archived' ? `受信 ${new Date(response.received_at_ms).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })}` : age == null ? state === 'loading' ? 'データを取得しています' : '受信データなし' : `${age}秒前に受信`);
  let message = '';
  let kind = 'warning';
  if (state === 'stale') message = `更新が停止しています。表示している値は最後に受信したデータです。`;
  if (state === 'error') { message = archiveMode ? '記録を取得できませんでした。期間と接続を確認し、もう一度「記録を表示」を押してください。' : response?.latest ? 'データを取得できません。最後の受信値を表示し、自動で再試行しています。' : 'データを取得できません。接続を確認してください。自動で再試行します。'; kind = 'error'; }
  if (state === 'empty') { message = archiveMode ? 'この期間に走行データはありません。日時を変えて確認してください。' : 'まだ走行データがありません。データの受信を待っています。'; kind = 'info'; }
  if (state === 'locked') { message = '管理者から受け取った閲覧用の共有リンクを開いてください。'; kind = 'info'; }
  if (state === 'unauthorized') { message = 'この共有リンクは使用できません。管理者から新しいリンクを受け取って開いてください。'; kind = 'error'; }
  if (state === 'archived') { message = '過去の受信記録を表示しています。走行の区切りとラップタイムは推定です。完走の確定はできません。'; kind = 'info'; }
  if (state === 'finished') { message = '模擬走行が終了しました。タイムを確認できます。'; kind = 'info'; }
  if (response?.latest && !response.position && state === 'live') { message = response.latest.latitude == null ? 'GPS位置が取得できません。速度とタイムは引き続き更新しています。' : 'GPS位置がコースから離れています。速度とタイムは引き続き更新しています。'; kind = 'warning'; }
  if (!message && response?.warnings?.length) message = '履歴やラップの一部を取得できていません。取得済みの記録を表示しています。';
  $('notice').hidden = !message;
  $('notice').dataset.kind = kind;
  if (message) setText('notice', message);
  document.body.dataset.connection = state;
}

function renderMetrics() {
  const data = response?.latest;
  setText('speed', decimal(data?.speed));
  setText('average-speed', decimal(data?.average_speed));
  setText('lap', data?.lap_number ?? '—');
  setText('elapsed', duration(data?.total_time_ms));
  setText('current-lap-time', duration(data?.lap_time_ms));
  setText('lap-note', data ? response.phase === 'finished' ? '全周回を完了' : archiveMode || stateIsOld() ? `${data.lap_number}周目の受信記録` : `${data.lap_number}周目を走行中` : '全7周の走行');
  setText('speed-note', data && data.speed == null ? '速度データが欠測しています' : response?.phase === 'finished' ? '走行終了' : '車輪センサーの計測値');
  const dots = Array.from({ length: 7 }, (_, index) => {
    const dot = document.createElement('i');
    dot.className = data && (index + 1 < data.lap_number || response.phase === 'finished') ? 'complete' : data && index + 1 === data.lap_number ? 'current' : '';
    return dot;
  });
  $('lap-dots').replaceChildren(...dots);
  const hasPosition = data && data.latitude != null && data.longitude != null && response.position;
  $('car-marker').toggleAttribute('hidden', !hasPosition);
  if (hasPosition) {
    const point = courseMapPoint(response.position);
    $('car-marker').setAttribute('transform', `translate(${point.x},${point.y})`);
    const bounds = $('course-map').viewBox.baseVal;
    const offsetX = Math.max(bounds.x + 5 - point.x, Math.min(-48, bounds.x + bounds.width - 101 - point.x));
    const below = point.y - 53 < bounds.y + 5;
    const callout = $('car-marker').querySelector('.car-callout');
    callout.setAttribute('transform', `translate(${offsetX},${below ? 20 : -53})`);
    const tip = Math.max(8, Math.min(88, -offsetX));
    callout.querySelector('path').setAttribute('d', below ? `M${tip - 6} 0L${tip} -6L${tip + 6} 0Z` : `M${tip - 6} 27L${tip} 33L${tip + 6} 27Z`);
  }
  setText('gps-status', hasPosition ? 'GPS 位置あり' : data ? data.latitude == null ? 'GPS 欠測' : 'コース外' : 'GPS 待機中');
  $('gps-status').dataset.missing = data && !hasPosition ? 'true' : 'false';
  $('map-overlay').hidden = !!hasPosition;
  setText('map-overlay', data ? data.latitude == null ? 'GPS位置が取得できません' : 'コース上の位置を特定できません' : '走行データを待っています');
  setText('history-status', response?.loading_history ? '履歴を読み込み中' : response?.warnings?.includes('history_truncated') ? '履歴は一部のみ' : response?.warnings?.length ? '履歴を再取得中' : '');
  $('export-csv').disabled = !response?.history?.length;
}

function renderLaps() {
  const completed = response?.laps ?? [];
  const data = response?.latest;
  const bestLap = completed.filter(lap => Number.isFinite(lap.time_ms) && lap.time_ms > 0).reduce((best, lap) => !best || lap.time_ms < best.time_ms ? lap : best, null)?.lap;
  const rows = [];
  for (let number = 1; number <= 7; number++) {
    const lap = completed.find(item => item.lap === number);
    const isCurrent = data && data.lap_number === number && response.phase !== 'finished';
    const row = document.createElement('tr');
    row.className = isCurrent ? 'current' : !lap ? 'upcoming' : bestLap === number ? 'best' : '';
    const title = document.createElement('td');
    title.textContent = String(number).padStart(2, '0');
    if (isCurrent) { const live = document.createElement('span'); live.className = 'lap-live'; live.textContent = archiveMode || stateIsOld() ? '受信時点' : '走行中'; title.append(live); }
    const time = document.createElement('td');
    time.textContent = duration(lap?.time_ms ?? (isCurrent ? data.lap_time_ms : null));
    if (lap && bestLap === number) { const best = document.createElement('span'); best.className = 'best-mark'; best.title = '最速ラップ'; best.setAttribute('aria-label', '最速ラップ'); time.append(best); }
    const diff = document.createElement('td');
    diff.textContent = Number.isFinite(lap?.time_ms) ? deltaDuration(lap.time_ms - lap.target_ms) : isCurrent ? archiveMode || stateIsOld() ? '記録時点' : '計測中' : '—';
    if (Number.isFinite(lap?.time_ms)) diff.className = lap.time_ms <= lap.target_ms ? 'delta-early' : 'delta-late';
    row.append(title, time, diff);
    rows.push(row);
  }
  $('lap-rows').replaceChildren(...rows);
  setText('laps-summary', demoMode ? `${completed.length} / 7 周完了` : `${completed.length} / 7 周（推定）`);
}

function renderHistory() {
  const samples = response?.history ?? [];
  const end = response?.latest?.total_time_ms ?? 300000;
  const start = chartRange === 'all' ? 0 : Math.max(0, end - chartRange * 1000);
  const signature = `${samples.length}-${end}-${chartRange}-${samples.at(-1)?.speed}-${samples[0]?.sample_id}`;
  if (historySignature === signature) return;
  historySignature = signature;
  const bounds = $('speed-chart').getBoundingClientRect();
  const width = Math.max(220, bounds.width);
  const height = Math.max(110, bounds.height || 125);
  drawSpeedChart($('speed-chart'), samples, { targetSpeed: course.length_m / TARGET_MS * 3600, fromMs: start, toMs: end, width, height, strategy: strategyUi?.getPlan(), course });
  $('chart-empty').hidden = samples.length > 0;
}

function render() {
  renderConnection();
  renderMetrics();
  renderLaps();
  renderHistory();
  strategyUi?.render(response);
}

async function poll() {
  if (archiveMode || document.hidden || requestError?.status === 401) return;
  clearTimeout(timer);
  controller?.abort();
  controller = new AbortController();
  const version = ++requestVersion;
  try {
    const next = await api.getLatest({ signal: controller.signal });
    if (version !== requestVersion) return;
    response = next;
    requestError = null;
    failures = 0;
  } catch (error) {
    if (error.name === 'AbortError' || version !== requestVersion) return;
    requestError = error;
    failures++;
    if (error.status === 401) response = null;
  }
  render();
  if (requestError?.status !== 401 && !archiveMode && !document.hidden) timer = setTimeout(poll, requestError ? retryDelay(requestError, failures) : POLL_MS);
}

async function loadArchive(event) {
  event.preventDefault();
  if (historyLoading) return;
  const from = fromTokyoInput($('history-start').value);
  const to = fromTokyoInput($('history-end').value);
  if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to || to - from > 7200000 || to > Date.now() + (response?.clock_offset_ms || 0)) {
    setText('history-feedback', '開始と終了を、現在以前の2時間以内の期間で指定してください。');
    return;
  }
  stopRequests();
  controller = new AbortController();
  const version = requestVersion;
  historyLoading = true;
  $('history-load').disabled = true;
  $('archive-run-label').hidden = true;
  archiveSnapshots = [];
  response = null;
  requestError = null;
  historySignature = '';
  setText('history-feedback', '記録を読み込んでいます…');
  render();
  try {
    const runs = await api.getArchive({ from, to, signal: controller.signal });
    if (version !== requestVersion) return;
    archiveSnapshots = runs;
    response = runs.at(-1) ?? snapshot([], course, { archived: true });
    $('archive-run').replaceChildren(...runs.map((run, index) => {
      const option = document.createElement('option');
      option.value = index;
      option.textContent = `候補 ${index + 1} / ${receiveClock(run.history[0]?.received_at_ms)}〜${receiveClock(run.received_at_ms)}`;
      return option;
    }));
    $('archive-run').value = runs.length - 1;
    $('archive-run-label').hidden = runs.length < 2;
    setText('history-feedback', runs.length ? `${runs.length}件の走行候補（区切りは推定）` : 'この期間の記録はありません。');
  } catch (error) {
    if (error.name === 'AbortError' || version !== requestVersion) return;
    requestError = error;
    setText('history-feedback', error.status === 401 ? '共有リンクを確認してください。' : '記録を取得できませんでした。再度お試しください。');
  } finally {
    if (version === requestVersion) { historyLoading = false; $('history-load').disabled = false; render(); }
  }
}

function changeDataView() {
  stopRequests();
  archiveMode = $('data-view').value === 'archive';
  $('history-form').hidden = !archiveMode;
  $('history-load').disabled = false;
  historyLoading = false;
  response = null;
  requestError = null;
  historySignature = '';
  modeLabels();
  if (archiveMode) {
    setText('history-feedback', '期間を指定して「記録を表示」を押してください。');
    $('archive-run-label').hidden = true;
  } else {
    api = createTelemetryApi({ course, token: readViewerToken(location.hash) });
    poll();
  }
  render();
}

async function start() {
  try {
    const result = await fetch('./assets/course.json');
    if (!result.ok) throw new Error('Course asset is unavailable');
    course = await result.json();
    api = demoMode ? createMockApi({ course }) : createTelemetryApi({ course, token: readViewerToken(location.hash) });
    $('demo-controls').hidden = !demoMode;
    $('source-controls').hidden = demoMode;
    modeLabels();
    // Keep the share fragment in the address bar for reload and copying. Never
    // write the token to localStorage, sessionStorage, logs, or query strings.
    document.querySelector('.brand').href = location.pathname + location.search + location.hash;
    drawCourse(course);
    strategyUi = initializeStrategy({ course, getResponse: () => response, onChange: () => { historySignature = ''; render(); } });
    render();
    $('scenario').addEventListener('change', () => { api.setScenario($('scenario').value); poll(); });
    $('data-view').addEventListener('change', changeDataView);
    $('history-form').addEventListener('submit', loadArchive);
    $('archive-run').addEventListener('change', () => {
      response = archiveSnapshots[Number($('archive-run').value)];
      historySignature = ''; render();
    });
    const end = Math.floor(Date.now() / 60000) * 60000;
    $('history-start').value = toTokyoInput(end - 3600000);
    $('history-end').value = toTokyoInput(end);
    window.addEventListener('hashchange', () => {
      if (demoMode || location.hash === '#main') return;
      stopRequests();
      response = null; requestError = null; failures = 0; historySignature = '';
      archiveMode = false; $('data-view').value = 'live'; $('history-form').hidden = true;
      api = createTelemetryApi({ course, token: readViewerToken(location.hash) });
      document.querySelector('.brand').href = location.pathname + location.search + location.hash;
      modeLabels(); render(); poll();
    });
    document.querySelector('.skip-link').addEventListener('click', event => { event.preventDefault(); $('main').tabIndex = -1; $('main').focus(); });
    document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
      chartRange = button.dataset.range === 'all' ? 'all' : Number(button.dataset.range);
      document.querySelectorAll('[data-range]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button)); });
      renderHistory();
    }));
    $('export-csv').addEventListener('click', () => {
      if (!response?.history?.length) return;
      const url = URL.createObjectURL(new Blob([csvText(response.history)], { type: 'text/csv;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `delta-deneb-${demoMode ? 'demo' : archiveMode ? 'archive' : 'telemetry'}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    if (!document.fullscreenEnabled) $('fullscreen').hidden = true;
    $('fullscreen').addEventListener('click', async () => {
      try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
      catch { $('fullscreen').hidden = true; }
    });
    document.addEventListener('fullscreenchange', () => $('fullscreen').setAttribute('aria-label', document.fullscreenElement ? '全画面表示を終了' : '全画面表示'));
    const resizeObserver = new ResizeObserver(() => { historySignature = ''; renderHistory(); });
    resizeObserver.observe($('speed-chart'));
    setInterval(renderConnection, 1000);
    window.addEventListener('pagehide', stopRequests);
    window.addEventListener('pageshow', event => { if (event.persisted) poll(); });
    document.addEventListener('visibilitychange', () => { if (document.hidden) stopRequests(); else if (!archiveMode) poll(); else render(); });
    window.addEventListener('online', () => { if (!archiveMode) poll(); });
    window.addEventListener('offline', () => { stopRequests(); requestError = new ApiError('network'); render(); });
    await poll();
  } catch (error) {
    $('main').replaceChildren(Object.assign(document.createElement('p'), { className: 'initial-error', textContent: '画面の読み込みに失敗しました。HTTPサーバー経由で開き、再読み込みしてください。' }));
    // Do not log URL fragments or fetch errors that could contain share data.
  }
}

start();
