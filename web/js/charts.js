import { duration } from './format.js';
import { projectOnRoute, strategyCue } from './strategy.js';

const NS = 'http://www.w3.org/2000/svg';
// Undo the asset's -45° schematic rotation: east is right, north is up.
// Only display coordinates change; GPS and route distances remain unchanged.
export function courseMapPoint(point) {
  const x = point.x - 410;
  const y = point.y - 175;
  return { x: (x - y) / Math.SQRT2, y: (x + y) / Math.SQRT2 };
}
export function svgNode(name, attributes = {}, text) {
  const element = document.createElementNS(NS, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  if (text !== undefined) element.textContent = text;
  return element;
}

export function drawSpeedChart(svg, samples, { targetSpeed = null, fromMs = null, toMs = null, width = 850, height = 225, relative = false, strategy = null, course = null } = {}) {
  svg.replaceChildren();
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const padding = { left: 42, top: 21, right: 22, bottom: 32 };
  const w = width - padding.left - padding.right;
  const h = height - padding.top - padding.bottom;
  const first = fromMs ?? samples[0]?.total_time_ms ?? 0;
  const last = Math.max(first + 1000, toMs ?? samples.at(-1)?.total_time_ms ?? 300000);
  const maxSpeed = Math.max(50, ...samples.filter(s => Number.isFinite(s.speed)).map(s => Math.ceil(s.speed / 10) * 10));
  const x = time => padding.left + (time - first) / (last - first) * w;
  const y = speed => padding.top + h - speed / maxSpeed * h;
  const visible = samples.filter(sample => sample.total_time_ms >= first && sample.total_time_ms <= last);
  if (strategy && course) {
    for (let i = 0; i < visible.length - 1; i++) {
      const sample = visible[i];
      const next = visible[i + 1];
      if (sample.lap_number !== next.lap_number || next.total_time_ms - sample.total_time_ms > 10000) continue;
      const lap = strategy.laps.find(item => item.lap === sample.lap_number);
      if (!lap) continue;
      const route = course.routes[lap.route_id];
      const position = projectOnRoute(route, sample.latitude, sample.longitude);
      if (strategyCue(lap, position?.s, route.length_m)?.mode !== 'engine') continue;
      svg.append(svgNode('rect', { x: x(sample.total_time_ms), y: padding.top, width: Math.max(0, x(next.total_time_ms) - x(sample.total_time_ms)), height: h, fill: '#f5e7d8', class: 'strategy-band' }));
    }
  }
  for (let speed = 0; speed <= maxSpeed; speed += 10) {
    svg.append(svgNode('line', { x1: padding.left, y1: y(speed), x2: width - padding.right, y2: y(speed), class: 'chart-grid' }));
    svg.append(svgNode('text', { x: padding.left - 11, y: y(speed) + 3, 'text-anchor': 'end', class: 'chart-axis' }, speed));
  }
  for (let i = 0; i <= 5; i++) {
    const time = first + (last - first) * i / 5;
    svg.append(svgNode('text', { x: x(time), y: height - 11, 'text-anchor': i === 0 ? 'start' : i === 5 ? 'end' : 'middle', class: 'chart-axis' }, duration(relative ? time - first : time)));
  }
  if (targetSpeed != null) svg.append(svgNode('line', { x1: padding.left, y1: y(targetSpeed), x2: width - padding.right, y2: y(targetSpeed), class: 'chart-target' }));
  // Null sensor readings break the path; they never become a zero speed sample.
  let started = false;
  const path = visible.map(sample => {
    if (typeof sample.speed !== 'number' || !Number.isFinite(sample.speed)) { started = false; return ''; }
    const command = started ? 'L' : 'M';
    started = true;
    return `${command}${x(sample.total_time_ms).toFixed(2)},${y(sample.speed).toFixed(2)}`;
  }).join(' ');
  if (path) {
    const trace = svgNode('path', { d: path, class: 'chart-trace' });
    trace.append(svgNode('title', {}, '模擬走行の速度推移'));
    svg.append(trace);
    const latest = visible.at(-1);
    if (latest && typeof latest.speed === 'number' && Number.isFinite(latest.speed)) svg.append(svgNode('circle', { cx: x(latest.total_time_ms), cy: y(latest.speed), r: 3.2, fill: '#236b4f', stroke: '#fff', 'stroke-width': 1.4 }));
  }
}

export function drawCourse(course) {
  const allPoints = [...Object.values(course.segments).flat(), ...Object.values(course.landmarks)].map(courseMapPoint);
  const left = Math.min(...allPoints.map(p => p.x));
  const right = Math.max(...allPoints.map(p => p.x));
  const top = Math.min(...allPoints.map(p => p.y));
  const bottom = Math.max(...allPoints.map(p => p.y));
  const padding = 22;
  const width = right - left + padding * 2;
  const height = bottom - top + padding * 2;
  document.getElementById('course-map').setAttribute('viewBox', `${left - padding} ${top - padding} ${width} ${height}`);
  const background = document.querySelector('#course-map > rect');
  for (const [key, value] of Object.entries({ x: left - padding, y: top - padding, width, height })) background.setAttribute(key, value);
  const road = document.getElementById('course-road');
  road.replaceChildren();
  for (const [name, points] of Object.entries(course.segments)) {
    const path = points.map((point, i) => { const p = courseMapPoint(point); return `${i ? 'L' : 'M'}${p.x},${p.y}`; }).join(' ');
    road.append(svgNode('path', { d: path, fill: 'none', stroke: '#e2e8dc', 'stroke-width': name === 'oval' ? 23 : 15, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    road.append(svgNode('path', { d: path, fill: 'none', stroke: name === 'oval' ? '#b2c3a5' : '#c7d2bd', 'stroke-width': name === 'oval' ? 2 : 1.5, 'stroke-linecap': 'round', 'stroke-dasharray': name === 'oval' ? '' : '3 4' }));
  }
  const landmarkRoot = document.getElementById('course-landmarks');
  landmarkRoot.replaceChildren();
  const landmarkConfig = { start: ['START', 10, 21], finish: ['FINISH', 12, -14], lap_update_on_route: ['LAP LINE', 12, 22] };
  for (const [name, [label, dx, dy]] of Object.entries(landmarkConfig)) {
    const p = courseMapPoint(course.landmarks[name]);
    landmarkRoot.append(svgNode('circle', { cx: p.x, cy: p.y, r: 4, fill: '#fff', stroke: '#839b73', 'stroke-width': 1.5 }));
    landmarkRoot.append(svgNode('text', { x: p.x + dx, y: p.y + dy, fill: '#8b9c7e', 'font-size': 9, 'font-family': 'monospace', 'letter-spacing': '.7', 'text-anchor': 'start' }, label));
  }
  landmarkRoot.append(svgNode('text', { x: (left + right) / 2, y: (top + bottom) / 2, 'text-anchor': 'middle', fill: '#c0cbb6', 'font-size': 16, 'font-family': 'monospace', 'letter-spacing': 3 }, 'MOTEGI'));
}
