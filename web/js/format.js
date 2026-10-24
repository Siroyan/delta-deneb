export const TARGET_MS = 38.5 * 60 * 1000; // UI demo setting, not an official race limit.
export const POLL_MS = 3000;
export const STALE_MS = 10000;

export function decimal(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

export function duration(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '—:—';
  const seconds = Math.floor(ms / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function deltaDuration(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '—';
  return `${ms < 0 ? '−' : '+'}${duration(Math.abs(ms))}`;
}

export function receiveClock(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '—:—:—';
  return new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(ms);
}

export function connectionState({ response, error, now = Date.now() }) {
  if (error?.code === 'missing_token') return 'locked';
  if (error?.status === 401) return 'unauthorized';
  if (error) return 'error';
  if (!response) return 'loading';
  if (!response.latest) return 'empty';
  if (response.archived) return 'archived';
  if (response.phase === 'finished') return 'finished';
  if (response.phase === 'waiting') return 'waiting';
  now += response.clock_offset_ms || 0;
  if (now - response.received_at_ms > STALE_MS) return 'stale';
  return 'live';
}

export function csvText(history) {
  const fields = ['timestamp_ms', 'received_at_ms', 'speed', 'average_speed', 'lap_number', 'total_time_ms', 'lap_time_ms', 'completed_lap_times_ms', 'latitude', 'longitude', 'machine_id'];
  const escape = value => value == null ? '' : `"${String(value).replaceAll('"', '""')}"`;
  return '\uFEFF' + fields.join(',') + '\r\n' + history.map(sample => fields.map(key => escape(key === 'completed_lap_times_ms' && Array.isArray(sample[key]) ? JSON.stringify(sample[key]) : sample[key])).join(',')).join('\r\n');
}
