import { TARGET_MS } from './format.js';

// This module is the only data transport boundary. Replace getLatest() with fetch()
// after the Lambda response contract is defined. No AWS request is sent in this demo.
const COMPLETED_MS = [300000, 335000, 333000];
const BASE_ELAPSED_MS = 1172000;
const FINISH_LAP_MS = [300000, 335000, 333000, 339000, 336000, 332000, 316000];
export const SCENARIOS = ['live', 'stale', 'error', 'gps-missing', 'empty', 'finished'];

export function routeForLap(course, lap) {
  return course.routes[lap === 1 ? 'first_lap' : lap === course.lap_count ? 'final_lap' : 'regular_lap'];
}

export function pointAtDistance(route, distance) {
  const clamped = Math.max(0, Math.min(route.length_m, distance));
  let index = route.points.findIndex(point => point.s >= clamped);
  if (index < 0) index = route.points.length - 1;
  if (index === 0) return { ...route.points[0] };
  const a = route.points[index - 1];
  const b = route.points[index];
  const t = (clamped - a.s) / (b.s - a.s || 1);
  return Object.fromEntries(['s', 'x', 'y', 'latitude', 'longitude'].map(key => [key, a[key] + (b[key] - a[key]) * t]));
}

function mockSpeed(lap, fraction) {
  return Math.max(12, 26 + 11.5 * Math.sin(fraction * Math.PI * 6 + lap * .6) + 3.8 * Math.cos(fraction * Math.PI * 12));
}

export function createMockApi({ course, now = () => Date.now(), latencyMs = 260 } = {}) {
  if (!course) throw new Error('Course is required');
  let scenario = 'live';
  let lastNow = now();
  const startEpoch = lastNow - BASE_ELAPSED_MS;
  let elapsedMs = BASE_ELAPSED_MS;
  let lap = 4;
  let lapMs = BASE_ELAPSED_MS - COMPLETED_MS.reduce((sum, n) => sum + n, 0);
  let lapDistance = routeForLap(course, lap).length_m * lapMs / 336000;
  let completed = COMPLETED_MS.map((time_ms, index) => ({ lap: index + 1, time_ms, distance_m: routeForLap(course, index + 1).length_m }));
  let totalDistance = completed.reduce((sum, item) => sum + item.distance_m, 0) + lapDistance;
  const history = seedHistory(COMPLETED_MS, lapMs);
  let phase = 'running';
  let lastResponse = null;
  let frozenResponse = null;

  function seedHistory(times, partialMs = 0) {
    let elapsed = 0;
    let distanceBefore = 0;
    const samples = [];
    const allTimes = partialMs > 0 ? [...times, partialMs] : times;
    allTimes.forEach((time, index) => {
      const route = routeForLap(course, index + 1);
      const fullTime = index < times.length ? time : 336000;
      for (let t = 0; t <= time; t += 3000) {
        const fraction = Math.min(1, t / fullTime);
        const position = pointAtDistance(route, route.length_m * fraction);
        const totalTime = elapsed + t;
        samples.push({ timestamp_ms: totalTime + 120000, received_at_ms: startEpoch + totalTime, speed: mockSpeed(index + 1, fraction), average_speed: totalTime > 0 ? (distanceBefore + route.length_m * fraction) / totalTime * 3600 : null, lap_number: index + 1, total_time_ms: totalTime, lap_time_ms: t, latitude: position.latitude, longitude: position.longitude, machine_id: 'pi', memo: 'delta-deneb UI demo' });
      }
      elapsed += time;
      distanceBefore += route.length_m;
    });
    return samples;
  }

  function advance() {
    const time = now();
    let remainingMs = Math.max(0, time - lastNow);
    lastNow = time;
    // Integrate in small steps so background tabs still advance coherently.
    while (remainingMs > 0 && phase !== 'finished') {
      const stepMs = Math.min(1000, remainingMs);
      const route = routeForLap(course, lap);
      const speed = mockSpeed(lap, lapDistance / route.length_m);
      const travelled = speed * stepMs / 3600;
      if (lapDistance + travelled >= route.length_m) {
        const usedMs = (route.length_m - lapDistance) / speed * 3600;
        elapsedMs += usedMs;
        lapMs += usedMs;
        totalDistance += route.length_m - lapDistance;
        completed.push({ lap, time_ms: lapMs, distance_m: route.length_m });
        if (lap >= course.lap_count) phase = 'finished';
        else { lap += 1; lapMs = 0; lapDistance = 0; }
        remainingMs -= usedMs;
      } else {
        elapsedMs += stepMs;
        lapMs += stepMs;
        lapDistance += travelled;
        totalDistance += travelled;
        remainingMs -= stepMs;
      }
    }
    if (phase === 'finished') { totalDistance = course.length_m; lapDistance = routeForLap(course, course.lap_count).length_m; }
  }

  function buildResponse() {
    advance();
    const route = routeForLap(course, lap);
    const point = pointAtDistance(route, lapDistance);
    const latest = { speed: phase === 'finished' ? 0 : mockSpeed(lap, lapDistance / route.length_m), average_speed: totalDistance / elapsedMs * 3600, lap_number: lap, total_time_ms: Math.round(elapsedMs), lap_time_ms: Math.round(lapMs), latitude: point.latitude, longitude: point.longitude, timestamp_ms: Math.round(elapsedMs + 120000), machine_id: 'pi', memo: 'delta-deneb UI demo' };
    const current = { ...latest, received_at_ms: now() };
    if (history.at(-1)?.total_time_ms !== current.total_time_ms) history.push(current);
    return { latest, received_at_ms: now(), history: history.slice(-1600), laps: completed.map(item => ({ ...item, target_ms: item.distance_m / course.length_m * TARGET_MS })), phase, position: { ...point } };
  }

  function finishResponse() {
    const totalMs = FINISH_LAP_MS.reduce((sum, item) => sum + item, 0);
    const fullHistory = seedHistory(FINISH_LAP_MS).map(sample => ({ ...sample, received_at_ms: now() - totalMs + sample.total_time_ms }));
    const finalRoute = routeForLap(course, course.lap_count);
    const position = pointAtDistance(finalRoute, finalRoute.length_m);
    const latest = { speed: 0, average_speed: course.length_m / totalMs * 3600, lap_number: 7, total_time_ms: totalMs, lap_time_ms: FINISH_LAP_MS.at(-1), latitude: position.latitude, longitude: position.longitude, timestamp_ms: totalMs + 120000, machine_id: 'pi', memo: 'delta-deneb UI demo' };
    fullHistory.push({ ...latest, received_at_ms: now() });
    return { latest, received_at_ms: now(), phase: 'finished', history: fullHistory, position, laps: FINISH_LAP_MS.map((time_ms, index) => ({ lap: index + 1, time_ms, distance_m: routeForLap(course, index + 1).length_m, target_ms: routeForLap(course, index + 1).length_m / course.length_m * TARGET_MS })) };
  }

  return {
    setScenario(value) {
      if (!SCENARIOS.includes(value)) throw new Error('Unknown demo scenario');
      if (value === 'stale' && scenario !== 'stale') {
        frozenResponse = structuredClone(lastResponse?.latest ? lastResponse : buildResponse());
        frozenResponse.received_at_ms = now() - 15000;
      }
      scenario = value;
    },
    async getLatest({ signal } = {}) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
        const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, latencyMs);
        signal?.addEventListener('abort', abort, { once: true });
      });
      if (scenario === 'error') throw new Error('模擬APIで取得エラーが発生しました');
      if (scenario === 'empty') return { latest: null, received_at_ms: now(), history: [], laps: [], phase: 'waiting', position: null };
      if (scenario === 'stale') return structuredClone(frozenResponse);
      if (scenario === 'finished') { lastResponse = finishResponse(); return lastResponse; }
      const response = buildResponse();
      if (scenario === 'gps-missing') {
        response.latest.latitude = null;
        response.latest.longitude = null;
        response.position = null;
        response.history[response.history.length - 1] = { ...response.history.at(-1), latitude: null, longitude: null };
      }
      lastResponse = response;
      return response;
    }
  };
}
