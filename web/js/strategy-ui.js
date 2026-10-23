import { parseStrategy, projectOnRoute, strategyCue, MAX_STRATEGY_BYTES } from './strategy.js';
import { pointAtDistance } from './mock-api.js';
import { courseMapPoint, svgNode } from './charts.js';
import { decimal } from './format.js';

const STORAGE_KEY = 'delta-deneb.strategy.v1';
const ENGINE = '#cb8542';
const COAST = '#6b9bb7';
const $ = id => document.getElementById(id);

export function initializeStrategy({ course, getResponse, onChange }) {
  let plan = null;
  let filename = '';
  let previewLap = null;
  let loadVersion = 0;
  let mapSignature = '';
  let detailsSignature = '';
  const layer = svgNode('g', { id: 'course-strategy' });
  $('course-map').insertBefore(layer, $('course-landmarks'));
  const mapInfo = document.createElement('div');
  mapInfo.className = 'course-strategy-info';
  mapInfo.hidden = true;
  const legend = document.createElement('span');
  legend.className = 'strategy-legend';
  const engineMark = document.createElement('i'); engineMark.className = 'engine';
  const coastMark = document.createElement('i'); coastMark.className = 'coast';
  legend.append(engineMark, '使用予定', coastMark, '惰性予定');
  const cueLabel = document.createElement('span');
  cueLabel.className = 'strategy-cue';
  mapInfo.append(legend, cueLabel);
  document.querySelector('.course-wrap').after(mapInfo);
  document.querySelectorAll('.chart-legend').forEach(container => {
    const item = document.createElement('span'); item.className = 'strategy-chart-legend'; item.hidden = true;
    const mark = document.createElement('i'); mark.className = 'legend-band';
    item.append(mark, '使用予定区間（GPS照合）');
    container.insertBefore(item, container.lastElementChild);
  });

  function feedback(message, error = false) {
    $('strategy-feedback').hidden = !message;
    $('strategy-feedback').textContent = message;
    $('strategy-feedback').dataset.error = String(error);
  }
  function remember(text) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ text, filename })); return true; }
    catch { return false; }
  }
  function adopt(text, name, persist = true) {
    const candidate = parseStrategy(text, course); // Do not modify the current plan until validation succeeds.
    plan = candidate;
    filename = name;
    previewLap = null;
    mapSignature = '';
    detailsSignature = '';
    const saved = persist ? remember(text) : true;
    feedback(saved ? '読み込みました。このブラウザだけに適用されます。' : '読み込みました。ブラウザ保存が利用できないため、再読み込み時には解除されます。');
    $('strategy-lap').replaceChildren(...plan.laps.map(lap => { const option = document.createElement('option'); option.value = lap.lap; option.textContent = `LAP ${String(lap.lap).padStart(2, '0')}`; return option; }));
    render(getResponse());
  }

  function drawMap(lap) {
    const signature = `${plan.plan_id}-${lap.lap}-${filename}`;
    if (mapSignature === signature) return;
    mapSignature = signature;
    layer.replaceChildren();
    const route = course.routes[lap.route_id];
    const path = points => points.map((value, index) => { const point = courseMapPoint(value); return `${index ? 'L' : 'M'}${point.x},${point.y}`; }).join(' ');
    layer.append(svgNode('path', { d: path(route.points), fill: 'none', stroke: COAST, 'stroke-width': 5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    lap.runs.forEach(run => {
      const points = [pointAtDistance(route, run.on_s_m), ...route.points.filter(point => point.s > run.on_s_m && point.s < run.off_s_m), pointAtDistance(route, run.off_s_m)];
      layer.append(svgNode('path', { d: path(points), fill: 'none', stroke: ENGINE, 'stroke-width': 5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
      for (const [label, s, color] of [['ON', run.on_s_m, ENGINE], ['OFF', run.off_s_m, COAST]]) {
        const point = courseMapPoint(pointAtDistance(route, s));
        const marker = svgNode('circle', { cx: point.x, cy: point.y, r: 4.5, fill: color, stroke: '#fff', 'stroke-width': 1.5 });
        marker.append(svgNode('title', {}, `${label} ${decimal(s, 0)} m`));
        layer.append(marker, svgNode('text', { x: point.x, y: point.y - 11, 'text-anchor': 'middle', 'font-size': 10, 'font-family': 'monospace', fill: color, 'paint-order': 'stroke', stroke: '#f8faf5', 'stroke-width': 3 }, label));
      }
    });
  }

  function drawDetails(lap, response) {
    const route = course.routes[lap.route_id];
    const svg = $('strategy-timeline');
    const width = Math.max(300, svg.getBoundingClientRect().width || 820);
    const data = response?.latest;
    const current = data?.lap_number === lap.lap ? projectOnRoute(route, data.latitude, data.longitude) : null;
    const signature = `${plan.plan_id}-${filename}-${lap.lap}-${width}-${Math.round(current?.s ?? -1)}`;
    if (detailsSignature === signature) return;
    detailsSignature = signature;
    svg.replaceChildren();
    svg.setAttribute('viewBox', `0 0 ${width} 105`);
    const x = s => 26 + s / route.length_m * (width - 52);
    svg.append(svgNode('rect', { x: 26, y: 36, width: width - 52, height: 15, rx: 4, fill: '#dae8ee' }));
    lap.runs.forEach((run, index) => {
      svg.append(svgNode('rect', { x: x(run.on_s_m), y: 36, width: x(run.off_s_m) - x(run.on_s_m), height: 15, rx: 2, fill: '#e7b88d' }));
      svg.append(svgNode('text', { x: (x(run.on_s_m) + x(run.off_s_m)) / 2, y: 27, 'text-anchor': 'middle', fill: ENGINE, 'font-family': 'monospace', 'font-size': 10 }, `ON ${index + 1}`));
    });
    for (let i = 0; i <= 4; i++) {
      const s = route.length_m * i / 4;
      svg.append(svgNode('line', { x1: x(s), x2: x(s), y1: 56, y2: 60, stroke: '#bdc8b5' }));
      svg.append(svgNode('text', { x: x(s), y: 77, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle', class: 'chart-axis' }, `${decimal(s, 0)} m`));
    }
    if (current) {
      svg.append(svgNode('line', { x1: x(current.s), x2: x(current.s), y1: 30, y2: 58, stroke: '#236b4f', 'stroke-width': 2 }));
      svg.append(svgNode('circle', { cx: x(current.s), cy: 43, r: 4, fill: '#236b4f', stroke: '#fff', 'stroke-width': 2 }));
      svg.append(svgNode('text', { x: x(current.s), y: 97, 'text-anchor': 'middle', 'font-size': 10, fill: '#236b4f' }, '現在位置'));
    }
    $('strategy-route').textContent = `${lap.route_id} / ${decimal(route.length_m / 1000, 2)} km / ${lap.runs.length}区間`;
    $('strategy-runs').replaceChildren(...lap.runs.map((run, index) => {
      const row = document.createElement('tr');
      for (const value of [String(index + 1).padStart(2, '0'), `${decimal(run.on_s_m, 1)} m`, `${decimal(run.off_s_m, 1)} m`, `${decimal(run.off_s_m - run.on_s_m, 1)} m`]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      return row;
    }));
  }

  function render(response) {
    $('strategy-type').hidden = !plan;
    $('strategy-details-toggle').hidden = !plan;
    $('strategy-clear').hidden = !plan;
    $('strategy-demo').hidden = !!plan;
    mapInfo.hidden = !plan;
    document.querySelectorAll('.strategy-chart-legend').forEach(item => { item.hidden = !plan; });
    if (!plan) {
      $('strategy-label').textContent = '戦略JSONを読み込んで、ON/OFF地点を重ねて表示';
      $('strategy-title').textContent = '走行戦略';
      $('strategy-details').hidden = true;
      $('strategy-details-toggle').setAttribute('aria-expanded', 'false');
      layer.replaceChildren();
      return;
    }
    $('strategy-title').textContent = plan.plan_id;
    $('strategy-type').textContent = plan.plan_type === 'demo' ? 'PLAN DEMO' : 'PLAN RACE';
    $('strategy-type').dataset.type = plan.plan_type;
    $('strategy-label').textContent = `${filename} · ${plan.laps.length}周 / ${plan.laps.reduce((sum, lap) => sum + lap.runs.length, 0)}区間 · ブラウザ内に保存`;
    const number = response?.latest?.lap_number ?? 1;
    const lap = plan.laps.find(item => item.lap === number);
    if (lap) drawMap(lap);
    else { layer.replaceChildren(); mapSignature = ''; }
    legend.hidden = !lap;
    const route = lap ? course.routes[lap.route_id] : null;
    const data = response?.latest;
    const position = data && route ? projectOnRoute(route, data.latitude, data.longitude) : null;
    const cue = strategyCue(lap, position?.s, route?.length_m);
    cueLabel.textContent = !lap ? 'この周回の走行戦略はありません' : `LAP ${String(lap.lap).padStart(2, '0')} · ` + (response?.phase === 'finished' ? '走行終了' : cue ? `${cue.mode === 'engine' ? '使用予定区間' : '惰性予定区間'} / 次の${cue.next === 'LAP END' ? '周回終点' : cue.next}まで ${decimal(cue.distance_m, 0)} m（GPS推定）` : '位置未取得');
    const detailLap = plan.laps.find(item => item.lap === (previewLap ?? number)) ?? plan.laps[0];
    $('strategy-lap').value = detailLap.lap;
    if (!$('strategy-details').hidden) drawDetails(detailLap, response);
  }

  $('strategy-import').addEventListener('click', () => $('strategy-file').click());
  $('strategy-file').addEventListener('change', async () => {
    const file = $('strategy-file').files[0];
    if (!file) return;
    const version = ++loadVersion;
    try {
      if (!file.size || file.size > MAX_STRATEGY_BYTES) throw new Error('戦略ファイルは1〜4096バイトにしてください。');
      const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer());
      if (version !== loadVersion) return;
      adopt(text, file.name);
      onChange();
    } catch (error) { if (version === loadVersion) feedback(`読み込めませんでした: ${error.message}${plan ? ' 現在の戦略は保持しています。' : ''}`, true); }
    finally { if (version === loadVersion) $('strategy-file').value = ''; }
  });
  $('strategy-demo').addEventListener('click', async () => {
    const version = ++loadVersion;
    try {
      const result = await fetch('./assets/motegi_demo.json');
      if (!result.ok) throw new Error('サンプルファイルを取得できませんでした。');
      const text = await result.text();
      if (version !== loadVersion) return;
      adopt(text, 'motegi_demo.json');
      onChange();
    } catch (error) { if (version === loadVersion) feedback(error.message, true); }
  });
  $('strategy-clear').addEventListener('click', () => {
    loadVersion++;
    plan = null;
    filename = '';
    mapSignature = '';
    detailsSignature = '';
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* Session-only mode remains usable. */ }
    feedback('走行戦略の表示を解除しました。');
    render(getResponse());
    onChange();
  });
  $('strategy-details-toggle').addEventListener('click', () => {
    $('strategy-details').hidden = !$('strategy-details').hidden;
    $('strategy-details-toggle').setAttribute('aria-expanded', String(!$('strategy-details').hidden));
    render(getResponse());
  });
  $('strategy-lap').addEventListener('change', () => { previewLap = Number($('strategy-lap').value); render(getResponse()); });
  $('strategy-follow').addEventListener('click', () => { previewLap = null; render(getResponse()); });
  const resizeObserver = new ResizeObserver(() => { if (plan && !$('strategy-details').hidden) { detailsSignature = ''; render(getResponse()); } });
  resizeObserver.observe($('strategy-timeline'));
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) { const saved = JSON.parse(stored); adopt(saved.text, saved.filename, false); feedback('保存済みの戦略を復元しました。このブラウザだけに適用されます。'); }
  } catch { feedback('保存済みの戦略を復元できませんでした。ファイルを再度読み込んでください。', true); }
  render(getResponse());
  return { render, getPlan: () => plan };
}
