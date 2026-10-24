import { POLL_MS } from './format.js';
import { projectOnRoute } from './strategy.js';
import { pointAtDistance } from './mock-api.js';
import { validLapState, lapPhase, deviceLaps } from './lap-state.js';

export const MAX_WINDOW_MS = 7200000;
const MAX_SAMPLES = 10000;
const OVERLAP_MS = 5000;

export class ApiError extends Error {
  constructor(code, { status = 0, retryAfterMs = 0 } = {}) {
    super(code);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
}

export function readViewerToken(hash) {
  const params = new URLSearchParams(hash.replace(/^#/, ''));
  const tokens = params.getAll('token');
  if (tokens.length !== 1 || !/^[A-Za-z0-9_-]{1,256}$/.test(tokens[0])) return null;
  return tokens[0];
}

export function retryDelay(error, failures = 1) {
  return Math.max(Math.min(30000, POLL_MS * 2 ** Math.min(4, Math.max(0, failures - 1))), Math.min(60000, error?.retryAfterMs || 0));
}

export function isRunReset(previous, sample) {
  return previous && (sample.machine_id !== previous.machine_id || sample.timestamp_ms < previous.timestamp_ms || sample.total_time_ms < previous.total_time_ms || sample.lap_number < previous.lap_number);
}

export function splitRuns(samples) {
  const unique = new Map(samples.map(sample => [sample.sample_id, sample]));
  const sorted = [...unique.values()].sort((a, b) => a.received_at_ms - b.received_at_ms || a.timestamp_ms - b.timestamp_ms || a.sample_id.localeCompare(b.sample_id));
  const runs = [];
  for (const sample of sorted) {
    if (!runs.length || isRunReset(runs.at(-1).at(-1), sample)) runs.push([]);
    runs.at(-1).push(sample);
  }
  return runs;
}

export function snapshot(samples, course, { latest = samples.at(-1) ?? null, archived = false, warnings = [], clockOffsetMs = 0, loadingHistory = false } = {}) {
  // MQTT has no course ID or planned lap count. Use GPS against the configured
  // map rather than treating lap 7 as the final route for every measurement.
  const candidates = latest ? Object.values(course.routes).map(route => ({ route, projection: projectOnRoute(route, latest.latitude, latest.longitude) })).filter(item => item.projection) : [];
  const nearest = candidates.sort((a, b) => a.projection.distance_m - b.projection.distance_m)[0];
  return {
    latest, received_at_ms: latest?.received_at_ms ?? null, history: samples,
    laps: deviceLaps(latest), lap_times_available: Array.isArray(latest?.completed_lap_times_ms),
    phase: lapPhase(latest),
    position: nearest ? pointAtDistance(nearest.route, nearest.projection.s) : null,
    archived, warnings, clock_offset_ms: clockOffsetMs, loading_history: loadingHistory,
  };
}

function sampleIsValid(sample) {
  return sample && typeof sample.sample_id === 'string' && sample.sample_id.length > 0 && sample.machine_id === 'delta_machine_alpha'
    && ['received_at_ms', 'timestamp_ms', 'total_time_ms', 'lap_time_ms', 'lap_number'].every(key => Number.isSafeInteger(sample[key]) && sample[key] >= 0)
    && validLapState(sample)
    && ['speed', 'average_speed', 'latitude', 'longitude'].every(key => sample[key] === null || (typeof sample[key] === 'number' && Number.isFinite(sample[key])));
}

export function createTelemetryApi({ course, token, fetchImpl = globalThis.fetch, now = Date.now, timeoutMs = 10000 }) {
  let history = [], latest = null, generation = 0;
  let pending = null, syncedTo = null, truncated = false, offset = 0;
  let latestMissing = false, historyBusy = false, detailsTask = null, detailsController = null;
  const retry = { history: { at: 0, failures: 0 } };

  async function request(path, params, signal) {
    if (!token) throw new ApiError('missing_token', { status: 401 });
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(abort, timeoutMs);
    try {
      const query = new URLSearchParams(params);
      const result = await fetchImpl(`/api/telemetry/${path}${query.size ? `?${query}` : ''}`, {
        method: 'GET', headers: { Accept: 'application/json', 'X-Deneb-Token': token },
        signal: controller.signal, cache: 'no-store', credentials: 'omit', mode: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer',
      });
      if (!result.ok) throw new ApiError(result.status === 401 ? 'unauthorized' : 'request_failed', {
        status: result.status, retryAfterMs: Math.max(0, Number(result.headers.get('retry-after')) || 0) * 1000,
      });
      if (!result.headers.get('content-type')?.includes('application/json')) throw new ApiError('invalid_response');
      const value = await result.json();
      if (!value || typeof value !== 'object' || value.invalid_items > 0) throw new ApiError('invalid_data');
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      return value;
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      if (error instanceof ApiError) throw error;
      throw new ApiError(controller.signal.aborted ? 'timeout' : 'network');
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', abort);
    }
  }

  function checkItems(items) {
    if (!Array.isArray(items) || !items.every(sampleIsValid)) throw new ApiError('invalid_response');
  }

  function rememberSamples(items) {
    const all = splitRuns([...history, ...items, latest]).at(-1) ?? [];
    truncated ||= all.length > MAX_SAMPLES;
    history = all.slice(-MAX_SAMPLES);
  }

  function currentSnapshot() {
    const warnings = [];
    if (latestMissing) warnings.push('latest_unavailable');
    if (retry.history.failures) warnings.push('history_unavailable');
    if (truncated) warnings.push('history_truncated');
    return snapshot(history, course, {
      latest, warnings, clockOffsetMs: offset,
      loadingHistory: !!latest && !retry.history.failures && (historyBusy || !!pending || syncedTo === null),
    });
  }

  async function background(channel, action, ensureCurrent) {
    ensureCurrent();
    if (now() < retry[channel].at) return;
    try {
      await action();
      ensureCurrent();
      retry[channel] = { at: 0, failures: 0 };
    } catch (error) {
      ensureCurrent();
      if (error.name === 'AbortError' || error.status === 401) throw error;
      const failures = retry[channel].failures + 1;
      retry[channel] = { at: now() + retryDelay(error, failures), failures };
      if (channel === 'history' && error.status === 400) pending = null;
    }
  }

  return {
    async getLatest({ signal } = {}) {
      const result = await request('latest', {}, signal);
      if (!Number.isSafeInteger(result.fetched_at_ms) || (result.latest !== null && !sampleIsValid(result.latest))) throw new ApiError('invalid_response');
      offset = result.fetched_at_ms - now();
      const candidate = result.latest;
      if (candidate && (!latest || candidate.received_at_ms >= latest.received_at_ms)) {
        if (isRunReset(latest, candidate)) {
          detailsController?.abort();
          history = []; syncedTo = null; pending = null;
          truncated = false; historyBusy = false; generation++;
          retry.history = { at: 0, failures: 0 };
        }
        latest = candidate;
        rememberSamples([candidate]);
      }
      latestMissing = !candidate && !!latest;
      // This method never waits for history or lap endpoints.
      return currentSnapshot();
    },

    refreshDetails({ signal } = {}) {
      if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
      if (detailsTask) return detailsTask;
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener('abort', abort, { once: true });
      detailsController = controller;
      const run = generation, anchor = latest;
      const ensureCurrent = () => {
        if (controller.signal.aborted || generation !== run) throw new DOMException('Aborted', 'AbortError');
      };
      // One bounded backfill may run while multiple latest polls complete.
      detailsTask = Promise.resolve().then(async () => {
        ensureCurrent();
        if (!anchor) return currentSnapshot();
        const end = anchor.received_at_ms;
        const start = Math.max(0, end - Math.min(MAX_WINDOW_MS, anchor.total_time_ms + OVERLAP_MS));
        await background('history', async () => {
          historyBusy = true;
          try {
            if (!pending) pending = { from: Math.max(start, (syncedTo ?? start) - OVERLAP_MS), to: end, cursor: null };
            for (let page = 0; page < 3; page++) {
              const params = pending.cursor ? { cursor: pending.cursor, limit: 1000 } : { from_ms: pending.from, to_ms: pending.to, limit: 1000 };
              const result = await request('history', params, controller.signal);
              ensureCurrent();
              checkItems(result.history);
              rememberSamples(result.history);
              pending.cursor = result.next_cursor;
              if (!pending.cursor) { syncedTo = pending.to; pending = null; break; }
            }
          } finally {
            historyBusy = false;
          }
        }, ensureCurrent);
        ensureCurrent();
        return currentSnapshot();
      }).finally(() => {
        signal?.removeEventListener('abort', abort);
        detailsTask = null;
        detailsController = null;
      });
      return detailsTask;
    },

    async getArchive({ from, to, signal } = {}) {
      if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 0 || from >= to || to - from > MAX_WINDOW_MS) throw new ApiError('invalid_window');
      let cursor = null, items = [], more = false;
      for (let page = 0; page < 20; page++) {
        const result = await request('history', cursor ? { cursor, limit: 1000 } : { from_ms: from, to_ms: to, limit: 1000 }, signal);
        checkItems(result.history);
        items.push(...result.history);
        cursor = result.next_cursor;
        more = !!cursor;
        if (items.length >= MAX_SAMPLES || !cursor) break;
      }
      const warnings = more || items.length > MAX_SAMPLES ? ['history_truncated'] : [];
      const runs = splitRuns(items.slice(0, MAX_SAMPLES));
      return runs.map(samples => snapshot(samples, course, { archived: true, warnings }));
    },
  };
}

export function toTokyoInput(ms) { return new Date(ms + 9 * 3600000).toISOString().slice(0, 16); }
export function fromTokyoInput(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return NaN;
  const ms = Date.parse(`${value}:00+09:00`);
  return Number.isFinite(ms) && toTokyoInput(ms) === value ? ms : NaN;
}
