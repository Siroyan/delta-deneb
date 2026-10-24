// Device protocol: a running lap is excluded; the final lap is included on finish.
// null/absent is a legacy record, distinct from a confirmed empty list.
export function validLapState(sample) {
  const { lap_number: lap, total_time_ms: total, lap_time_ms: current, completed_lap_times_ms: times } = sample;
  if (!Number.isSafeInteger(lap) || lap < 0 || lap > 100 || !Number.isSafeInteger(total) || total < 0 || !Number.isSafeInteger(current) || current < 0 || current > total) return false;
  if (lap === 0 && (total !== 0 || current !== 0)) return false;
  if (times == null) return true;
  if (!Array.isArray(times) || times.length > 100 || !times.every(time => Number.isSafeInteger(time) && time >= 0)) return false;
  const sum = times.reduce((a, b) => a + b, 0);
  if (!Number.isSafeInteger(sum)) return false;
  if (lap === 0) return times.length === 0 && total === 0 && current === 0;
  return times.length === lap - 1 ? sum + current === total : times.length === lap && sum === total && times.at(-1) === current;
}

export function lapPhase(sample) {
  if (!sample || sample.lap_number === 0) return 'waiting';
  return sample.completed_lap_times_ms?.length === sample.lap_number ? 'finished' : 'running';
}

export function deviceLaps(sample) {
  return (sample?.completed_lap_times_ms ?? []).map((time_ms, index) => ({ lap: index + 1, time_ms, source: 'device' }));
}
