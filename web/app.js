import { createMockApi, routeForLap } from './js/mock-api.js';
import { decimal, duration, deltaDuration, receiveClock, connectionState, estimates, csvText, TARGET_MS, POLL_MS } from './js/format.js';
import { courseMapPoint, drawCourse, drawSpeedChart } from './js/charts.js';
import { initializeStrategy } from './js/strategy-ui.js';

const $ = id => document.getElementById(id);
let api;
let course;
let response = null;
let requestError = null;
let timer;
let controller;
let requestVersion = 0;
let chartRange = 300;
let view = 'live';
let selectedLap = 1;
let previousStatus = '';
let analysisSignature = '';
let historySignature = '';
let strategyUi;

function setText(id, value) { if ($(id).textContent !== String(value)) $(id).textContent = String(value); }

function renderConnection() {
  const state = connectionState({ response, error: requestError });
  const age = response?.latest ? Math.max(0, Math.floor((Date.now() - response.received_at_ms) / 1000)) : null;
  const labels = { loading: '接続中', live: 'LIVE · 通信正常', stale: '更新停止', error: '取得エラー', empty: 'データ待ち', finished: 'FINISHED' };
  $('connection-status').dataset.status = state;
  // Only announce state transitions, not the ticking data age.
  if (state !== previousStatus) { setText('connection-label', labels[state]); previousStatus = state; }
  setText('last-update', age == null ? state === 'loading' ? 'データを取得しています' : '受信データなし' : `${age}秒前に受信`);
  let message = '';
  let kind = 'warning';
  if (state === 'stale') message = `更新が停止しています。表示している値は最後に受信したデータです。`;
  if (state === 'error') { message = response?.latest ? 'データの取得に失敗しました。最後の受信値を表示し、3秒ごとに再試行しています。' : 'データの取得に失敗しました。3秒ごとに再試行しています。'; kind = 'error'; }
  if (state === 'empty') { message = 'まだ走行データがありません。データの受信を待っています。'; kind = 'info'; }
  if (state === 'finished') { message = '模擬走行が終了しました。タイムとラップごとの走行を確認できます。'; kind = 'info'; }
  if (response?.latest && response.latest.latitude == null && state === 'live') { message = 'GPS位置が取得できません。速度とタイムは引き続き更新しています。'; kind = 'warning'; }
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
  const estimate = estimates(response, course);
  setText('distance', estimate ? `${decimal(estimate.distance / 1000, 2)} km` : '— km');
  setText('finish-estimate', duration(estimate?.finishMs));
  setText('target-delta', deltaDuration(estimate?.deltaMs));
  $('target-delta').classList.toggle('late', estimate?.deltaMs > 0);
  setText('progress-percent', estimate ? `${decimal(estimate.progress, 1)}%` : '—%');
  setText('remaining-distance', estimate ? `残り ${decimal(estimate.remaining / 1000, 2)} km` : '残り — km');
  $('progress-fill').style.width = `${estimate?.progress ?? 0}%`;
  setText('target-average', `${decimal(course.length_m / TARGET_MS * 3600)} km/h`);
  setText('received-time', receiveClock(data ? response.received_at_ms : null));
  setText('lap-note', data ? response.phase === 'finished' ? '全周回を完了' : `${data.lap_number}周目を走行中` : '全7周の走行');
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
  setText('gps-status', hasPosition ? 'GPS 位置あり' : data ? 'GPS 欠測' : 'GPS 待機中');
  $('gps-status').dataset.missing = data && !hasPosition ? 'true' : 'false';
  $('map-overlay').hidden = !!hasPosition;
  setText('map-overlay', data ? 'GPS位置が取得できません' : '走行データを待っています');
  $('export-csv').disabled = !response?.history?.length;
}

function renderLaps() {
  const focusedLabel = $('lap-rows').contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
  const completed = response?.laps ?? [];
  const data = response?.latest;
  const bestLap = completed.reduce((best, lap) => !best || lap.time_ms < best.time_ms ? lap : best, null)?.lap;
  const rows = [];
  for (let number = 1; number <= 7; number++) {
    const lap = completed.find(item => item.lap === number);
    const isCurrent = data && data.lap_number === number && response.phase !== 'finished';
    const row = document.createElement('tr');
    row.className = isCurrent ? 'current' : !lap ? 'upcoming' : bestLap === number ? 'best' : '';
    const title = document.createElement('td');
    title.textContent = String(number).padStart(2, '0');
    if (isCurrent) { const live = document.createElement('span'); live.className = 'lap-live'; live.textContent = '走行中'; title.append(live); }
    const time = document.createElement('td');
    time.textContent = duration(lap?.time_ms ?? (isCurrent ? data.lap_time_ms : null));
    if (lap && bestLap === number) { const best = document.createElement('span'); best.className = 'best-mark'; best.title = '最速ラップ'; best.setAttribute('aria-label', '最速ラップ'); time.append(best); }
    const diff = document.createElement('td');
    diff.textContent = lap ? deltaDuration(lap.time_ms - lap.target_ms) : isCurrent ? '計測中' : '—';
    if (lap) diff.className = lap.time_ms <= lap.target_ms ? 'delta-early' : 'delta-late';
    const action = document.createElement('td');
    if (lap || isCurrent) {
      const button = document.createElement('button');
      button.className = 'lap-arrow';
      button.textContent = '↗';
      button.setAttribute('aria-label', `ラップ${number}を分析`);
      button.addEventListener('click', () => { selectedLap = number; setView('analysis'); });
      action.append(button);
    }
    row.append(title, time, diff, action);
    rows.push(row);
  }
  $('lap-rows').replaceChildren(...rows);
  if (focusedLabel) [...$('lap-rows').querySelectorAll('button')].find(button => button.getAttribute('aria-label') === focusedLabel)?.focus({ preventScroll: true });
  setText('laps-summary', `${completed.length} / 7 周完了`);
}

function renderHistory() {
  const samples = response?.history ?? [];
  const end = response?.latest?.total_time_ms ?? 300000;
  const start = chartRange === 'all' ? 0 : Math.max(0, end - chartRange * 1000);
  const signature = `${samples.length}-${end}-${chartRange}-${samples.at(-1)?.speed}`;
  if (historySignature === signature) return;
  historySignature = signature;
  const bounds = $('speed-chart').getBoundingClientRect();
  const width = Math.max(220, bounds.width);
  const height = Math.max(110, bounds.height || 125);
  drawSpeedChart($('speed-chart'), samples, { targetSpeed: course.length_m / TARGET_MS * 3600, fromMs: start, toMs: end, width, height, strategy: strategyUi?.getPlan(), course });
  $('chart-empty').hidden = samples.length > 0;
}

function renderAnalysis() {
  if (view !== 'analysis') return;
  const available = [...new Set((response?.history ?? []).map(sample => sample.lap_number))];
  if (!available.includes(selectedLap)) selectedLap = available.at(-1) ?? 1;
  const select = $('analysis-lap');
  const signature = available.join(',');
  if (analysisSignature !== signature) {
    analysisSignature = signature;
    select.replaceChildren(...available.map(number => { const option = document.createElement('option'); option.value = number; option.textContent = `LAP ${String(number).padStart(2, '0')}`; return option; }));
  }
  select.value = selectedLap;
  select.disabled = available.length === 0;
  const completeLap = response?.laps?.find(item => item.lap === selectedLap);
  const current = response?.latest?.lap_number === selectedLap && response.phase !== 'finished';
  const ms = completeLap?.time_ms ?? (current ? response.latest.lap_time_ms : null);
  const route = routeForLap(course, selectedLap);
  setText('analysis-time', duration(ms));
  setText('analysis-average', completeLap ? `${decimal(route.length_m / ms * 3600)} km/h` : '— km/h');
  setText('analysis-delta', completeLap ? deltaDuration(ms - completeLap.target_ms) : '—');
  const samples = (response?.history ?? []).filter(item => item.lap_number === selectedLap);
  const width = Math.max(280, $('lap-chart').getBoundingClientRect().width);
  drawSpeedChart($('lap-chart'), samples, { width, height: width < 560 ? 220 : 280, fromMs: samples[0]?.total_time_ms ?? 0, toMs: samples.at(-1)?.total_time_ms ?? 300000, relative: true, strategy: strategyUi?.getPlan(), course });
  setText('analysis-note', !samples.length ? 'まだ分析できる走行データがありません。' : current ? '現在計測中のラップです。タイムとグラフは自動更新されます。' : `LAP ${String(selectedLap).padStart(2, '0')} / ${decimal(route.length_m / 1000, 2)} km。目標値はこのデモで設定した走行目標です。`);
}

function setView(next) {
  view = next;
  document.body.dataset.view = next;
  $('live-view').classList.toggle('active', next === 'live');
  $('analysis-view').classList.toggle('active', next === 'analysis');
  $('live-view').setAttribute('aria-pressed', String(next === 'live'));
  $('analysis-view').setAttribute('aria-pressed', String(next === 'analysis'));
  $('lap-analysis').hidden = next !== 'analysis';
  renderAnalysis();
  strategyUi?.render(response);
}

function render() {
  renderConnection();
  renderMetrics();
  renderLaps();
  renderHistory();
  renderAnalysis();
  strategyUi?.render(response);
}

async function poll() {
  clearTimeout(timer);
  controller?.abort();
  controller = new AbortController();
  const version = ++requestVersion;
  try {
    const next = await api.getLatest({ signal: controller.signal });
    if (version !== requestVersion) return;
    response = next;
    requestError = null;
  } catch (error) {
    if (error.name === 'AbortError' || version !== requestVersion) return;
    requestError = error;
  }
  render();
  timer = setTimeout(poll, POLL_MS);
}

async function start() {
  try {
    const result = await fetch('./assets/course.json');
    if (!result.ok) throw new Error('Course asset is unavailable');
    course = await result.json();
    api = createMockApi({ course });
    drawCourse(course);
    strategyUi = initializeStrategy({ course, getResponse: () => response, onChange: () => { historySignature = ''; render(); } });
    render();
    $('scenario').addEventListener('change', () => { api.setScenario($('scenario').value); poll(); });
    $('live-view').addEventListener('click', () => setView('live'));
    $('analysis-view').addEventListener('click', () => setView('analysis'));
    $('analysis-lap').addEventListener('change', () => { selectedLap = Number($('analysis-lap').value); renderAnalysis(); });
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
      link.download = 'delta-deneb-demo.csv';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    if (!document.fullscreenEnabled) $('fullscreen').hidden = true;
    $('fullscreen').addEventListener('click', async () => {
      try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
      catch { $('fullscreen').hidden = true; }
    });
    document.addEventListener('fullscreenchange', () => $('fullscreen').setAttribute('aria-label', document.fullscreenElement ? '全画面表示を終了' : '全画面表示'));
    const resizeObserver = new ResizeObserver(() => { historySignature = ''; renderHistory(); renderAnalysis(); });
    resizeObserver.observe($('speed-chart'));
    resizeObserver.observe($('lap-chart'));
    setInterval(renderConnection, 1000);
    window.addEventListener('pagehide', () => { controller?.abort(); clearTimeout(timer); });
    window.addEventListener('pageshow', event => { if (event.persisted) poll(); });
    await poll();
  } catch (error) {
    $('main').replaceChildren(Object.assign(document.createElement('p'), { className: 'initial-error', textContent: '画面の読み込みに失敗しました。HTTPサーバー経由で開き、再読み込みしてください。' }));
    console.error(error);
  }
}

start();
