import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ApiError, createTelemetryApi, readViewerToken, retryDelay, splitRuns, inferLaps, snapshot, fromTokyoInput, toTokyoInput } from '../web/js/telemetry-api.js';
import { connectionState } from '../web/js/format.js';

const course = JSON.parse(await readFile(new URL('../web/assets/course.json', import.meta.url)));
const NOW = 1800000000000;
const TOKEN = 'example-viewer-token';
function sample(id, { total = 1000, lap = 1, lapTime = total, received = NOW, uptime = total + 120000, speed = 27 } = {}) {
  const point = course.routes[lap === 1 ? 'first_lap' : lap === 7 ? 'final_lap' : 'regular_lap'].points[10];
  return { sample_id: id, machine_id: 'delta_machine_alpha', received_at_ms: received, timestamp_ms: uptime,
    total_time_ms: total, lap_time_ms: lapTime, lap_number: lap, speed, average_speed: 26, latitude: point.latitude, longitude: point.longitude };
}
function json(body, status = 200, headers = {}) { return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } }); }

function harness(initial, { pageSize = 1000, intercept = null, timeoutMs = 10000 } = {}) {
  const state = { items: initial, now: NOW, historyError: false, lapError: false, lapPayload: null, latestStatus: 200, calls: [] };
  const fetchImpl = async (path, options) => {
    state.calls.push({ path, options });
    const intercepted = intercept?.(path, options, state);
    if (intercepted !== undefined) return intercepted;
    const url = new URL(path, 'https://example.test');
    const values = state.items;
    const latest = values.at(-1) ?? null;
    if (url.pathname.endsWith('/latest')) return json({ latest, received_at_ms: latest?.received_at_ms ?? null, fetched_at_ms: state.now, invalid_items: 0 }, state.latestStatus);
    if (url.pathname.endsWith('/laps')) {
      if (state.lapError) return json({}, 503);
      const run = splitRuns(values).at(-1) ?? [];
      return json({ latest, laps: state.lapPayload ?? inferLaps(run), truncated: state.lapPayload !== null, invalid_items: 0 });
    }
    if (state.historyError) return json({ error: { code: 'private-raw-error' } }, 503, { 'retry-after': '6' });
    const cursor = url.searchParams.get('cursor');
    const context = cursor ? JSON.parse(Buffer.from(cursor, 'base64url')) : { from: Number(url.searchParams.get('from_ms')), to: Number(url.searchParams.get('to_ms')), offset: 0 };
    const filtered = values.filter(item => item.received_at_ms >= context.from && item.received_at_ms <= context.to);
    const size = Math.min(pageSize, Number(url.searchParams.get('limit')));
    const end = context.offset + size;
    const next = end < filtered.length ? Buffer.from(JSON.stringify({ ...context, offset: end })).toString('base64url') : null;
    return json({ history: filtered.slice(context.offset, end), next_cursor: next, invalid_items: 0 });
  };
  return { state, api: createTelemetryApi({ course, token: TOKEN, fetchImpl, now: () => state.now, timeoutMs }) };
}

async function readWithDetails(api) {
  await api.getLatest();
  return api.refreshDetails();
}

function deferred() {
  let resolve;
  const promise = new Promise(deliver => { resolve = deliver; });
  return { promise, resolve };
}

test('tokens are accepted only from a single valid fragment parameter', () => {
  assert.equal(readViewerToken(`#token=${TOKEN}`), TOKEN);
  for (const hash of ['', '#main', '#token=', '#token=x&token=y', '#token=%0D%0Avalue']) assert.equal(readViewerToken(hash), null);
});

test('missing share key prevents all network access', async () => {
  const api = createTelemetryApi({ course, token: null, fetchImpl: () => assert.fail('must not fetch') });
  await assert.rejects(api.getLatest(), error => error.code === 'missing_token' && error.status === 401);
});

test('real requests carry a header and never put the key in URLs or follow redirects', async () => {
  const { api, state } = harness([sample('one')]);
  const value = await readWithDetails(api);
  assert.equal(value.latest.sample_id, 'one');
  assert.ok(value.position);
  for (const { path, options } of state.calls) {
    assert.ok(path.startsWith('/api/telemetry/'));
    assert.ok(!path.includes(TOKEN));
    assert.equal(options.headers['X-Deneb-Token'], TOKEN);
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.referrerPolicy, 'no-referrer');
  }
});

test('empty real API stays empty and never starts the mock vehicle', async () => {
  const { api, state } = harness([]);
  const value = await readWithDetails(api);
  assert.equal(value.latest, null);
  assert.equal(value.received_at_ms, null);
  assert.deepEqual(value.history, []);
  assert.equal(state.calls.length, 1);
});

test('latest values are published without waiting for details', async () => {
  const { api, state } = harness([sample('fresh')]);
  const value = await api.getLatest();
  assert.equal(value.latest.sample_id, 'fresh');
  assert.deepEqual(state.calls.map(call => new URL(call.path, 'https://example.test').pathname), ['/api/telemetry/latest']);
});

for (const channel of ['history', 'laps']) test(`slow ${channel} cannot delay new latest polls or overwrite a newer sample`, async () => {
  const started = deferred(), release = deferred();
  const first = sample('first');
  const { api, state } = harness([first], { intercept: (path) => {
    if (!path.includes(`/${channel}`)) return;
    started.resolve();
    return release.promise;
  } });
  assert.equal((await api.getLatest()).latest.sample_id, 'first');
  const details = api.refreshDetails();
  await started.promise;
  assert.equal(api.refreshDetails(), details, 'overlapping refreshes share a bounded backfill');
  for (const [index, id] of ['second', 'third'].entries()) {
    state.now += 3000;
    state.items.push(sample(id, { total: 4000 + index * 3000, received: state.now }));
    assert.equal((await api.getLatest()).latest.sample_id, id);
  }
  assert.equal(state.calls.filter(call => call.path.includes(`/${channel}`)).length, 1);
  release.resolve(json(channel === 'history' ? { history: [first], next_cursor: null } : { latest: first, laps: [], truncated: false }));
  const finished = await details;
  assert.equal(finished.latest.sample_id, 'third');
  assert.equal(finished.received_at_ms, state.now);
  assert.ok(finished.history.some(item => item.sample_id === 'second'));
});

test('cancelling backfill aborts its request and allows a later refresh', async () => {
  const started = deferred();
  let blocked = false;
  const { api, state } = harness([sample('first')], { intercept: (path, { signal }) => {
    if (!path.includes('/history') || blocked) return;
    blocked = true;
    started.resolve(signal);
    return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
  } });
  await api.getLatest();
  const controller = new AbortController();
  const details = api.refreshDetails({ signal: controller.signal });
  const signal = await started.promise;
  controller.abort();
  await assert.rejects(details, error => error.name === 'AbortError');
  assert.equal(signal.aborted, true);
  state.now += 3000;
  state.items.push(sample('resumed', { total: 4000, received: state.now }));
  assert.equal((await readWithDetails(api)).latest.sample_id, 'resumed');
});

test('a new run discards even an old backfill that ignores cancellation', async () => {
  const started = deferred(), release = deferred();
  const old = sample('old-lap2', { total: 301000, lap: 2, lapTime: 1000 });
  const { api, state } = harness([old], { intercept: path => {
    if (!path.includes('/history')) return;
    started.resolve();
    return release.promise;
  } });
  await api.getLatest();
  const details = api.refreshDetails();
  const discarded = assert.rejects(details, error => error.name === 'AbortError');
  await started.promise;
  state.now += 3000;
  state.items = [sample('new-run', { total: 1000, uptime: 10000, received: state.now })];
  const latest = await api.getLatest();
  assert.deepEqual(latest.history.map(item => item.sample_id), ['new-run']);
  release.resolve(json({ history: [old], next_cursor: null }));
  await discarded;
  const after = await api.getLatest();
  assert.deepEqual(after.history.map(item => item.sample_id), ['new-run']);
  assert.deepEqual(after.laps, []);
});

test('latest observations preserve lap transitions while history is stalled', async () => {
  const started = deferred(), release = deferred();
  const first = sample('lap1', { total: 299000 });
  const { api, state } = harness([first], { intercept: path => {
    if (!path.includes('/history')) return;
    started.resolve();
    return release.promise;
  } });
  await api.getLatest();
  const details = api.refreshDetails();
  await started.promise;
  for (const [lap, total] of [[2, 301000], [3, 631000]]) {
    state.now += 3000;
    state.items.push(sample(`lap${lap}`, { lap, total, lapTime: 1000, received: state.now }));
    await api.getLatest();
  }
  release.resolve(json({ history: [first], next_cursor: null }));
  assert.deepEqual((await details).laps.map(({ lap, time_ms }) => [lap, time_ms]), [[1, 300000], [2, 330000]]);
});

test('detail timeouts and authorization failures are handled independently of latest', async () => {
  const { api, state } = harness([sample('first')], { timeoutMs: 5, intercept: (path, { signal }) => {
    if (!path.includes('/history')) return;
    return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
  } });
  assert.equal((await api.getLatest()).latest.sample_id, 'first');
  assert.ok((await api.refreshDetails()).warnings.includes('history_unavailable'));
  state.now += 3000;
  state.items.push(sample('second', { total: 4000, received: state.now }));
  assert.equal((await api.getLatest()).latest.sample_id, 'second');
  const unauthorized = harness([sample('first')], { intercept: path => path.includes('/laps') ? json({}, 401) : undefined });
  await unauthorized.api.getLatest();
  await assert.rejects(unauthorized.api.refreshDetails(), error => error.status === 401);
});

test('history overlap deduplicates sample IDs and laps are not queried on every poll', async () => {
  const { api, state } = harness([sample('a', { received: NOW - 1000 }), sample('b', { total: 2000 })]);
  await readWithDetails(api);
  state.now += 3000;
  state.items.push(sample('c', { total: 5000, received: state.now }));
  const value = await readWithDetails(api);
  assert.deepEqual(value.history.map(item => item.sample_id), ['a', 'b', 'c']);
  assert.equal(state.calls.filter(call => call.path.startsWith('/api/telemetry/laps')).length, 1);
  const histories = state.calls.filter(call => call.path.startsWith('/api/telemetry/history'));
  assert.ok(Number(new URL(histories[1].path, 'https://example.test').searchParams.get('from_ms')) <= NOW - 5000);
});

test('backfill is bounded to three pages per poll and continues with cursors', async () => {
  const items = Array.from({ length: 10 }, (_, index) => sample(String(index), { received: NOW - 9000 + index * 1000, total: index * 1000 + 1000 }));
  const { api, state } = harness(items, { pageSize: 2 });
  const first = await readWithDetails(api);
  assert.equal(first.latest.sample_id, '9');
  assert.equal(first.loading_history, true);
  assert.equal(state.calls.filter(call => call.path.includes('/history')).length, 3);
  const next = await readWithDetails(api);
  assert.equal(next.loading_history, false);
  assert.equal(next.history.length, 10);
});

test('history failure retains fresh metrics and retries with channel backoff', async () => {
  const { api, state } = harness([sample('one')]);
  state.historyError = true;
  const first = await readWithDetails(api);
  assert.equal(first.latest.speed, 27);
  assert.ok(first.warnings.includes('history_unavailable'));
  state.now += 3000;
  await readWithDetails(api);
  assert.equal(state.calls.filter(call => call.path.includes('/history')).length, 1);
  state.historyError = false;
  state.now += 3000;
  const recovered = await readWithDetails(api);
  assert.ok(!recovered.warnings.includes('history_unavailable'));
});

test('a reset discards the prior run before presenting the new vehicle state', async () => {
  const { api, state } = harness([sample('old', { total: 600000, lap: 3, lapTime: 1000 })]);
  await readWithDetails(api);
  state.now += 3000;
  state.items.push(sample('new', { total: 1000, received: state.now, uptime: 10000 }));
  const value = await readWithDetails(api);
  assert.deepEqual(value.history.map(item => item.sample_id), ['new']);
  assert.deepEqual(value.laps, []);
});

test('eventually consistent latest reads do not roll the dashboard backwards', async () => {
  const newest = sample('new', { total: 2000 });
  const { api, state } = harness([newest]);
  await readWithDetails(api);
  state.items = [sample('old', { total: 1000, received: NOW - 1000 })];
  const value = await readWithDetails(api);
  assert.equal(value.latest.sample_id, 'new');
});

test('HTTP authorization errors are not silently replaced with sample data', async () => {
  const { api, state } = harness([]);
  state.latestStatus = 401;
  await assert.rejects(api.getLatest(), error => error.status === 401 && error.code === 'unauthorized');
});

test('request timeout is bounded and aborts the fetch', async () => {
  const api = createTelemetryApi({ course, token: TOKEN, timeoutMs: 5, fetchImpl: (_, { signal }) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))) });
  await assert.rejects(api.getLatest(), error => error.code === 'timeout');
});

test('an aborted late response cannot become a cached sample', async () => {
  let deliver;
  let first = true;
  const api = createTelemetryApi({ course, token: TOKEN, fetchImpl: async () => {
    if (first) { first = false; return new Promise(resolve => { deliver = resolve; }); }
    return json({ latest: null, received_at_ms: null, fetched_at_ms: NOW, invalid_items: 0 });
  } });
  const controller = new AbortController();
  const pending = api.getLatest({ signal: controller.signal });
  controller.abort();
  deliver(json({ latest: sample('late'), fetched_at_ms: NOW }));
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.equal((await api.getLatest()).latest, null);
});

test('archival records split into separate inferred runs without polling live endpoints', async () => {
  const items = [sample('old1', { received: NOW - 600000, total: 300000 }), sample('old2', { received: NOW - 300000, total: 600000, lap: 2, lapTime: 300000 }), sample('new', { total: 1000 })];
  const { api, state } = harness(items, { pageSize: 1 });
  const runs = await api.getArchive({ from: NOW - 700000, to: NOW });
  assert.equal(runs.length, 2);
  assert.equal(runs[0].latest.sample_id, 'old2');
  assert.equal(runs[1].latest.sample_id, 'new');
  assert.equal(runs[0].archived, true);
  assert.ok(state.calls.every(call => call.path.startsWith('/api/telemetry/history')));
});

test('the first observed lap carries its own start boundary, even after lap 1', () => {
  const items = [sample('a', { lap: 3, total: 900000, lapTime: 1000 }), sample('b', { lap: 4, total: 1200000, lapTime: 1000 })];
  assert.equal(inferLaps(items)[0].time_ms, 300000);
  const value = snapshot(items, course);
  assert.equal(value.phase, 'running');
  assert.equal(value.laps.length, 1);
});

test('lap 2 to 3 keeps past times when the lap API returns a partial summary', async () => {
  const { api, state } = harness([
    sample('lap1', { total: 299000, received: NOW - 2000 }),
    sample('lap2', { total: 301000, lap: 2, lapTime: 1000 }),
  ]);
  assert.equal((await readWithDetails(api)).laps[0].time_ms, 300000);
  state.now += 330000;
  state.items.push(sample('lap3', { total: 631000, lap: 3, lapTime: 1000, received: state.now }));
  state.lapPayload = [{ lap: 2, time_ms: null, source: 'inferred_transition' }];
  const result = await readWithDetails(api);
  assert.deepEqual(result.laps.map(({ lap, time_ms }) => [lap, time_ms]), [[1, 300000], [2, 330000]]);
  assert.ok(result.warnings.includes('history_truncated'));
});

test('lap 2 to 3 keeps past times through history and lap API errors', async () => {
  const { api, state } = harness([
    sample('lap1', { total: 299000, received: NOW - 2000 }),
    sample('lap2', { total: 301000, lap: 2, lapTime: 1000 }),
  ]);
  await readWithDetails(api);
  state.now += 330000;
  state.items = [sample('lap3', { total: 631000, lap: 3, lapTime: 1000, received: state.now })];
  state.historyError = true;
  state.lapError = true;
  const result = await readWithDetails(api);
  assert.deepEqual(result.laps.map(({ lap, time_ms }) => [lap, time_ms]), [[1, 300000], [2, 330000]]);
});

test('known lap times survive cache eviction but clear on an actual new run', async () => {
  const { api, state } = harness([
    sample('lap1', { total: 299000, received: NOW - 2000 }),
    sample('lap2', { total: 301000, lap: 2, lapTime: 1000 }),
  ]);
  await readWithDetails(api);
  state.now += 330000;
  state.items = Array.from({ length: 10002 }, (_, index) => sample(`lap3-${index}`, {
    total: 631000 + index, lap: 3, lapTime: 1000 + index, received: state.now + index,
  }));
  state.now += 10001;
  state.lapPayload = [];
  for (let poll = 0; poll < 4; poll++) await readWithDetails(api);
  const capped = await readWithDetails(api);
  assert.equal(capped.history.length, 10000);
  assert.ok(capped.history.every(item => item.lap_number === 3));
  assert.equal(capped.laps.find(item => item.lap === 1)?.time_ms, 300000);
  state.now += 3000;
  state.items = [sample('restart', { total: 1000, uptime: 10000, received: state.now })];
  state.lapError = true;
  assert.deepEqual((await readWithDetails(api)).laps, []);
});

test('skipped and unobserved laps stay unknown, including the final lap', () => {
  const items = [sample('a', { lap: 3, total: 900000, lapTime: 1000 }), sample('b', { lap: 5, total: 1500000, lapTime: 1000 })];
  assert.equal(inferLaps(items)[0].time_ms, null);
  assert.deepEqual(inferLaps([sample('last', { lap: 7, total: 2100000, lapTime: 300000 })]), []);
});

test('real GPS loss keeps sensor missing values and does not fabricate map positions', () => {
  const current = { ...sample('none'), latitude: null, longitude: null, speed: null };
  const value = snapshot([current], course);
  assert.equal(value.position, null);
  assert.equal(value.latest.speed, null);
});

test('archive input dates use Tokyo independently of the viewer timezone', () => {
  const ms = fromTokyoInput('2026-10-18T15:30');
  assert.equal(new Date(ms).toISOString(), '2026-10-18T06:30:00.000Z');
  assert.equal(toTokyoInput(ms), '2026-10-18T15:30');
  assert.ok(Number.isNaN(fromTokyoInput('2026-02-30T15:30')));
});

test('freshness uses receipt time with server offset; archived records have a distinct state', () => {
  const response = { latest: sample('x'), received_at_ms: NOW, clock_offset_ms: 60000 };
  assert.equal(connectionState({ response, now: NOW - 60000 }), 'live');
  assert.equal(connectionState({ response, now: NOW - 40000 }), 'stale');
  assert.equal(connectionState({ response: { ...response, archived: true }, now: NOW + 999999 }), 'archived');
  assert.equal(connectionState({ error: new ApiError('missing_token', { status: 401 }) }), 'locked');
});

test('retries honor server backoff and are bounded', () => {
  assert.equal(retryDelay(new ApiError('network'), 1), 3000);
  assert.equal(retryDelay(new ApiError('network'), 100), 30000);
  assert.equal(retryDelay(new ApiError('rate', { retryAfterMs: 45000 }), 1), 45000);
});
