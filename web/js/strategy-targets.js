import { TARGET_MS } from './format.js';

// Only schema 2 declares per-plan targets; never derive them from the filename
// or distribute a total target across laps (their sums may differ by design).
export function strategyTargets(plan, lapNumber) {
  const explicit = plan?.schema_version === 2;
  const lap = explicit ? plan.laps.find(item => item.lap === lapNumber) : null;
  return {
    explicit,
    total_ms: explicit ? plan.total_target_s * 1000 : TARGET_MS,
    lap_ms: lap ? lap.lap_target_s * 1000 : null,
  };
}
