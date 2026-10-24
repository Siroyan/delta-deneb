import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createMockApi, pointAtDistance, routeForLap } from '../web/js/mock-api.js';
import { validLapState } from '../web/js/lap-state.js';
import { connectionState, csvText } from '../web/js/format.js';

const course = JSON.parse(await readFile(new URL('../web/assets/course.json', import.meta.url), 'utf8'));
function setup() {
  let time = 1791770400000;
  const api = createMockApi({ course, now: () => time, latencyMs: 0 });
  return { api, now: () => time, tick: ms => { time += ms; } };
}

test('normal polling advances time, position and history using the MQTT field names', async () => {
  const { api, now, tick } = setup();
  const initial = await api.getLatest();
  assert.equal(initial.latest.lap_number, 4);
  assert.equal(initial.laps.length, 3);
  assert.equal(connectionState({ response: initial, now: now() }), 'live');
  assert.ok(initial.latest.timestamp_ms < initial.received_at_ms); // device clock and receive epoch are distinct
  tick(3000);
  const next = await api.getLatest();
  assert.equal(next.latest.total_time_ms - initial.latest.total_time_ms, 3000);
  assert.notDeepEqual(next.position, initial.position);
  assert.ok(next.history.length > initial.history.length);
});

test('stale scenario freezes data and receipt time while data age grows', async () => {
  const { api, now, tick } = setup();
  const initial = await api.getLatest();
  api.setScenario('stale');
  const stale = await api.getLatest();
  assert.deepEqual(stale.latest, initial.latest);
  assert.equal(connectionState({ response: stale, now: now() }), 'stale');
  tick(12000);
  const later = await api.getLatest();
  assert.equal(later.received_at_ms, stale.received_at_ms);
  assert.deepEqual(later.latest, stale.latest);
  api.setScenario('live');
  const recovered = await api.getLatest();
  assert.ok(recovered.latest.total_time_ms > initial.latest.total_time_ms);
  assert.equal(connectionState({ response: recovered, now: now() }), 'live');
});

test('GPS loss clears coordinates and marker position while telemetry continues', async () => {
  const { api, tick } = setup();
  api.setScenario('gps-missing');
  const initial = await api.getLatest();
  assert.equal(initial.position, null);
  assert.equal(initial.latest.latitude, null);
  assert.equal(initial.latest.longitude, null);
  assert.ok(initial.latest.speed > 0);
  tick(3000);
  assert.ok((await api.getLatest()).latest.total_time_ms > initial.latest.total_time_ms);
  api.setScenario('live');
  assert.ok((await api.getLatest()).position);
});

test('API errors are rejected; empty data contains no fabricated readings', async () => {
  const { api, now } = setup();
  api.setScenario('error');
  await assert.rejects(api.getLatest(), /取得エラー/);
  assert.equal(connectionState({ response: null, error: new Error(), now: now() }), 'error');
  api.setScenario('empty');
  const empty = await api.getLatest();
  assert.equal(empty.latest, null);
  assert.deepEqual(empty.history, []);
  assert.deepEqual(empty.laps, []);
  assert.equal(connectionState({ response: empty, now: now() }), 'empty');
});

test('finished state has seven completed laps and frozen total time, never future receipt dates', async () => {
  const { api, now, tick } = setup();
  api.setScenario('finished');
  const finished = await api.getLatest();
  assert.equal(finished.latest.speed, 0);
  assert.equal(finished.laps.length, 7);
  assert.equal(finished.latest.total_time_ms, finished.laps.reduce((sum, lap) => sum + lap.time_ms, 0));
  assert.equal(connectionState({ response: finished, now: now() }), 'finished');
  assert.ok(finished.history.every(sample => sample.received_at_ms <= now()));
  tick(6000);
  assert.equal((await api.getLatest()).latest.total_time_ms, finished.latest.total_time_ms);
});

test('long gaps cross lap boundaries without overrunning the course', async () => {
  const { api, tick } = setup();
  tick(40 * 60 * 1000);
  const finished = await api.getLatest();
  assert.equal(finished.phase, 'finished');
  assert.equal(finished.laps.length, 7);
  assert.equal(finished.latest.lap_number, 7);
});

test('aborted in-flight requests cannot deliver an obsolete result', async () => {
  const api = createMockApi({ course, latencyMs: 50 });
  const controller = new AbortController();
  const pending = api.getLatest({ signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  await assert.rejects(api.getLatest({ signal: controller.signal }), { name: 'AbortError' });
});

test('route interpolation clamps endpoints and CSV preserves missing values', async () => {
  const route = routeForLap(course, 4);
  assert.deepEqual(pointAtDistance(route, -1), route.points[0]);
  const end = pointAtDistance(route, 1e9);
  assert.ok(Math.abs(end.s - route.points.at(-1).s) < .01); // asset positions are rounded to centimeters
  const { api } = setup();
  api.setScenario('gps-missing');
  const snapshot = await api.getLatest();
  const csv = csvText(snapshot.history);
  assert.ok(csv.startsWith('\uFEFFtimestamp_ms,received_at_ms,'));
  assert.ok(csv.endsWith(',,,"pi"'));
});

test('demo emits the same completed array contract across natural transitions and finish', async () => {
  const { api, tick } = setup();
  for (let index = 0; index < 180; index++) {
    tick(7141);
    const value = await api.getLatest();
    assert.equal(validLapState(value.latest), true);
    assert.ok(value.history.every(validLapState));
    assert.deepEqual(value.laps.map(item => item.time_ms), value.latest.completed_lap_times_ms);
  }
});

test('CSV encodes completed arrays as one JSON field and keeps legacy distinct', () => {
  const value = csvText([{ completed_lap_times_ms: [362000, 0] }, { completed_lap_times_ms: [] }, {}]);
  assert.ok(value.includes('completed_lap_times_ms'));
  assert.ok(value.includes('"[362000,0]"'));
  assert.ok(value.includes('"[]"'));
});
