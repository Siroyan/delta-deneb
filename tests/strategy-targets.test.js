import test from 'node:test';
import assert from 'node:assert/strict';
import { strategyTargets } from '../web/js/strategy-targets.js';
import { duration, TARGET_MS } from '../web/js/format.js';

const plan = { schema_version: 2, total_target_s: 2270, laps: [
  { lap: 1, lap_target_s: 269 }, { lap: 2, lap_target_s: 313 },
] };

test('schema 2 converts each declared target from seconds to milliseconds once', () => {
  const result = strategyTargets(plan, 1);
  assert.deepEqual(result, { explicit: true, total_ms: 2270000, lap_ms: 269000 });
  assert.equal(duration(result.total_ms), '37:50');
  assert.equal(duration(result.lap_ms), '04:29');
  assert.equal(strategyTargets(plan, 2).lap_ms, 313000);
});

test('waiting, completed or unsupported lap numbers never borrow another lap target', () => {
  for (const lap of [undefined, 0, 3, 100]) {
    assert.deepEqual(strategyTargets(plan, lap), { explicit: true, total_ms: 2270000, lap_ms: null });
  }
});

test('old format, clearing and restoring plans switch targets without retaining an old value', () => {
  for (const selected of [plan, { schema_version: 1 }, plan, null]) {
    const result = strategyTargets(selected, 1);
    assert.equal(result.total_ms, selected === plan ? 2270000 : TARGET_MS);
    assert.equal(result.lap_ms, selected === plan ? 269000 : null);
    assert.equal(result.explicit, selected === plan);
  }
});
