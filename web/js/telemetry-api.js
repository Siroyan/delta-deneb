import { TARGET_MS, POLL_MS } from './format.js';
import { projectOnRoute } from './strategy.js';
import { pointAtDistance, routeForLap } from './mock-api.js';

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

export function inferLaps(samples) {
  const laps = [];
  // Both counters share the race clock, so any observed lap gives its start.
  let start = samples.length ? samples[0].total_time_ms - samples[0].lap_time_ms : null;
  for (let index = 1; index < samples.length; index++) {
    const before = samples[index - 1], after = samples[index];
    if (after.lap_number === before.lap_number) continue;
    const end = after.total_time_ms - after.lap_time_ms;
    const duration = start !== null && after.lap_number === before.lap_number + 1 ? end - start : null;
    laps.push({ lap: before.lap_number, time_ms: duration > 0 ? duration : null, source: 'inferred_transition' });
    start = end;
  }
  return laps;
}

function mergeLaps(...sources) {
  const merged = new Map();
  for (const source of sources) for (const lap of source) {
    // Partial index reads must not erase a duration already observed this run.
    if (lap.time_ms !== null || !merged.has(lap.lap)) merged.set(lap.lap, lap);
  }
  return [...merged.values()].sort((a, b) => a.lap - b.lap);
}

export function snapshot(samples, course, { laps = null, archived = false, warnings = [], clockOffsetMs = 0, loadingHistory = false } = {}) {
  const latest = samples.at(-1) ?? null;
  const lap = latest ? routeForLap(course, latest.lap_number) : null;
  const projection = lap && latest ? projectOnRoute(lap, latest.latitude, latest.longitude) : null;
  return {
    latest, received_at_ms: latest?.received_at_ms ?? null, history: samples,
    laps: (laps ?? inferLaps(samples)).map(item => {
      const route = routeForLap(course, item.lap);
      return { ...item, distance_m: route.length_m, target_ms: route.length_m / course.length_m * TARGET_MS };
    }),
    phase: latest ? 'running' : 'waiting',
    position: projection ? pointAtDistance(lap, projection.s) : null,
    archived, warnings, clock_offset_ms: clockOffsetMs, loading_history: loadingHistory,
  };
}

function sampleIsValid(sample) {
  return sample && typeof sample.sample_id === 'string' && sample.sample_id.length > 0 && sample.machine_id === 'delta_machine_alpha'
    && ['received_at_ms', 'timestamp_ms', 'total_time_ms', 'lap_time_ms', 'lap_number'].every(key => Number.isSafeInteger(sample[key]) && sample[key] >= 0)
    && sample.lap_number >= 1 && sample.lap_number <= 7 && sample.lap_time_ms <= sample.total_time_ms
    && ['speed', 'average_speed', 'latitude', 'longitude'].every(key => sample[key] === null || (typeof sample[key] === 'number' && Number.isFinite(sample[key])));
}

export function createTelemetryApi({ course, token, fetchImpl = globalThis.fetch, now = Date.now, timeoutMs = 10000 }) {
  let history = [], latest = null, laps = [], lapKey = null, generation = 0, lapsTruncated = false;
  let pending = null, syncedTo = null, truncated = false, offset = 0;
  const retry = { history: { at: 0, failures: 0 }, laps: { at: 0, failures: 0 } };

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

  async function background(channel, action, warnings) {
    if (now() < retry[channel].at) { warnings.push(`${channel}_unavailable`); return; }
    try {
      await action();
      retry[channel] = { at: 0, failures: 0 };
    } catch (error) {
      if (error.name === 'AbortError' || error.status === 401) throw error;
      const failures = retry[channel].failures + 1;
      retry[channel] = { at: now() + retryDelay(error, failures), failures };
      if (channel === 'history' && error.status === 400) pending = null;
      warnings.push(`${channel}_unavailable`);
    }
  }

  return {
    async getLatest({ signal } = {}) {
      const result = await request('latest', {}, signal);
      if (!Number.isSafeInteger(result.fetched_at_ms) || (result.latest !== null && !sampleIsValid(result.latest))) throw new ApiError('invalid_response');
      offset = result.fetched_at_ms - now();
      const candidate = result.latest;
      if (candidate && (!latest || candidate.received_at_ms >= latest.received_at_ms)) {
        if (isRunReset(latest, candidate)) { history = []; laps = []; lapKey = null; syncedTo = null; pending = null; truncated = false; lapsTruncated = false; generation++; retry.history.at = 0; retry.laps.at = 0; }
        latest = candidate;
      }
      if (!latest) return snapshot([], course, { clockOffsetMs: offset });
      const warnings = [];
      if (!candidate) warnings.push('latest_unavailable');
      const end = latest.received_at_ms;
      const start = Math.max(0, end - Math.min(MAX_WINDOW_MS, latest.total_time_ms + OVERLAP_MS));
      await background('history', async () => {
        if (!pending) pending = { from: Math.max(start, (syncedTo ?? start) - OVERLAP_MS), to: end, cursor: null };
        for (let page = 0; page < 3; page++) {
          const params = pending.cursor ? { cursor: pending.cursor, limit: 1000 } : { from_ms: pending.from, to_ms: pending.to, limit: 1000 };
          const result = await request('history', params, signal);
          checkItems(result.history);
          const all = splitRuns([...history, ...result.history, latest]).at(-1) ?? [];
          truncated ||= all.length > MAX_SAMPLES;
          history = all.slice(-MAX_SAMPLES);
          pending.cursor = result.next_cursor;
          if (!pending.cursor) { syncedTo = pending.to; pending = null; break; }
        }
      }, warnings);
      const key = `${generation}:${latest.lap_number}`;
      let fetchedLaps = [];
      if (key !== lapKey) await background('laps', async () => {
        const result = await request('laps', { from_ms: start, to_ms: end }, signal);
        if (!Array.isArray(result.laps) || !result.laps.every(lap => Number.isInteger(lap.lap) && lap.lap >= 1 && lap.lap <= 7 && (lap.time_ms === null || (Number.isFinite(lap.time_ms) && lap.time_ms > 0)))) throw new ApiError('invalid_response');
        // An eventually consistent index may not yet include the lap transition.
        if (!result.latest || result.latest.lap_number !== latest.lap_number || Math.abs((result.latest.timestamp_ms - result.latest.total_time_ms) - (latest.timestamp_ms - latest.total_time_ms)) > 1000) throw new ApiError('index_pending');
        fetchedLaps = result.laps;
        lapsTruncated = !!result.truncated;
        lapKey = key;
      }, warnings);
      if (truncated || lapsTruncated) warnings.push('history_truncated');
      const samples = splitRuns([...history, latest]).at(-1) ?? [latest];
      laps = mergeLaps(laps, inferLaps(samples), fetchedLaps);
      return snapshot(samples, course, { laps, warnings, clockOffsetMs: offset, loadingHistory: !!pending });
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
