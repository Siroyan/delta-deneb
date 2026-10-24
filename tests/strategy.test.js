import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseStrategy, projectOnRoute, strategyCue } from '../web/js/strategy.js';
import { pointAtDistance } from '../web/js/mock-api.js';

const course = JSON.parse(await readFile(new URL('../web/assets/course.json', import.meta.url), 'utf8'));
const raw = await readFile(new URL('../web/assets/motegi_demo.json', import.meta.url), 'utf8');
const sample = JSON.parse(raw);
const changed = mutate => { const value = structuredClone(sample); mutate(value); return JSON.stringify(value); };

test('reads the exact delta-vega bundled schema 1 demo and permits race plans', () => {
  const plan = parseStrategy(raw, course);
  assert.equal(plan.plan_id, 'dummy-race-002');
  assert.equal(plan.laps.length, 7);
  assert.equal(plan.laps.reduce((sum, lap) => sum + lap.runs.length, 0), 21);
  assert.equal(parseStrategy(changed(value => { value.plan_type = 'race'; }), course).plan_type, 'race');
});

test('rejects wrong course, unsupported schemas, missing laps and wrong route IDs', () => {
  assert.throws(() => parseStrategy(changed(value => { value.course_id = 'another_course'; }), course), /コースID/);
  assert.throws(() => parseStrategy(changed(value => { value.schema_version = 3; }), course), /schema_version/);
  assert.throws(() => parseStrategy(changed(value => { value.laps.pop(); }), course), /全7周/);
  assert.throws(() => parseStrategy(changed(value => { value.laps[0].route_id = 'regular_lap'; }), course), /route_id/);
  assert.throws(() => parseStrategy(changed(value => { value.laps[1].lap = 1; }), course), /重複/);
});

test('rejects bad distance boundaries, overlapping runs, adjacent runs and empty runs', () => {
  const bad = [
    run => { run.on_s_m = -1; },
    run => { run.off_s_m = run.on_s_m; },
    run => { run.off_s_m = 1e6; },
    run => { run.on_s_m = '100'; }
  ];
  bad.forEach(mutate => assert.throws(() => parseStrategy(changed(value => mutate(value.laps[0].runs[0])), course)));
  assert.throws(() => parseStrategy(changed(value => { value.laps[0].runs[1].on_s_m = 300; }), course), /次のON/);
  assert.throws(() => parseStrategy(changed(value => { value.laps[0].runs[1].on_s_m = 100; }), course), /次のON/);
  assert.throws(() => parseStrategy(changed(value => { value.laps[0].runs = []; }), course), /1件以上/);
  assert.equal(parseStrategy(changed(value => { value.laps[0].runs.push({ on_s_m: 1900, off_s_m: 2000 }); }), course).laps[0].runs.length, 4);
});

test('rejects unknown and duplicate fields instead of silently overwriting them', () => {
  assert.throws(() => parseStrategy(changed(value => { value.foo = 1; }), course), /トップ階層/);
  assert.throws(() => parseStrategy(changed(value => { value.laps[0].runs[0].extra = 1; }), course), /のみ指定/);
  assert.throws(() => parseStrategy(raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'), course), /重複した項目/);
  assert.throws(() => parseStrategy(raw.replace('"on_s_m": 0', '"on_s_m": 0, "on_s_m": 100'), course), /重複した項目/);
});

test('rejects exponent, non-finite, BOM, escaped identifiers, trailing JSON and oversized files', () => {
  for (const token of ['1e2', 'NaN', 'Infinity', 'true', 'null', '0.' ]) {
    assert.throws(() => parseStrategy(raw.replace('"on_s_m": 0', `"on_s_m": ${token}`), course));
  }
  assert.throws(() => parseStrategy('\ufeff' + raw, course));
  assert.throws(() => parseStrategy(raw.replace('dummy-race-002', 'dummy\\u002d002'), course));
  assert.throws(() => parseStrategy(raw + '{}', course));
  assert.throws(() => parseStrategy(raw.padEnd(4097, ' '), course), /4096/);
});

test('supports reordered laps and only the named legacy demo without plan_type', () => {
  const reversed = parseStrategy(changed(value => { value.laps.reverse(); }), course);
  assert.deepEqual(reversed.laps.map(lap => lap.lap), [1, 2, 3, 4, 5, 6, 7]);
  assert.equal(parseStrategy(changed(value => { delete value.plan_type; value.plan_id = 'dummy-race-001'; }), course).plan_type, 'demo');
  assert.throws(() => parseStrategy(changed(value => { delete value.plan_type; }), course));
});

test('projects GPS to route distance and refuses missing or off-course positions', () => {
  const route = course.routes.regular_lap;
  const position = pointAtDistance(route, 850);
  const projected = projectOnRoute(route, position.latitude, position.longitude);
  assert.ok(Math.abs(projected.s - 850) < .02);
  assert.ok(projected.distance_m < .01);
  assert.equal(projectOnRoute(route, null, null), null);
  assert.equal(projectOnRoute(route, 35, 139), null);
});

test('next ON/OFF cue mirrors the vehicle parser boundaries without implying actual engine state', () => {
  const lap = parseStrategy(raw, course).laps[3];
  assert.deepEqual(strategyCue(lap, 100, 2412), { mode: 'coast', next: 'ON', distance_m: 50 });
  assert.deepEqual(strategyCue(lap, 200, 2412), { mode: 'engine', next: 'OFF', distance_m: 230 });
  assert.deepEqual(strategyCue(lap, 430, 2412), { mode: 'engine', next: 'OFF', distance_m: 0 });
  assert.deepEqual(strategyCue(lap, 2000, 2412), { mode: 'coast', next: 'LAP END', distance_m: 412 });
  assert.equal(strategyCue(lap, undefined, 2412), null);
});

function schema2() {
  const plan = structuredClone(sample);
  plan.schema_version = 2;
  plan.plan_id = 'schema-2-test';
  plan.total_target_s = 2270;
  for (const lap of plan.laps) lap.lap_target_s = lap.lap === 1 ? 269 : 315;
  return plan;
}

test('schema 2 preserves explicit total and lap targets, without requiring matching sums', () => {
  const value = schema2();
  assert.notEqual(value.total_target_s, value.laps.reduce((total, lap) => total + lap.lap_target_s, 0));
  const plan = parseStrategy(JSON.stringify(value), course);
  assert.equal(plan.schema_version, 2);
  assert.equal(plan.total_target_s, 2270);
  assert.equal(plan.laps[0].lap_target_s, 269);
  assert.ok(plan.laps.every(lap => lap.lap_target_s > 0));
  value.laps.reverse();
  assert.equal(parseStrategy(JSON.stringify(value), course).laps[0].lap_target_s, 269);
});

test('schema 2 requires all targets and plan_type; schema 1 forbids target fields', () => {
  for (const mutate of [
    plan => { delete plan.total_target_s; },
    plan => { delete plan.laps[3].lap_target_s; },
    plan => { plan.plan_id = 'dummy-race-001'; delete plan.plan_type; },
  ]) {
    const value = schema2(); mutate(value);
    assert.throws(() => parseStrategy(JSON.stringify(value), course));
  }
  assert.throws(() => parseStrategy(changed(plan => { plan.total_target_s = 2270; }), course), /トップ階層/);
  assert.throws(() => parseStrategy(changed(plan => { plan.laps[0].lap_target_s = 269; }), course), /各周/);
});

test('schema 2 target bounds are integer seconds from 1 to 86400', () => {
  for (const boundary of [1, 86400]) {
    const value = schema2();
    value.total_target_s = boundary;
    value.laps[0].lap_target_s = boundary;
    assert.equal(parseStrategy(JSON.stringify(value), course).total_target_s, boundary);
  }
  for (const bad of [0, -1, 1.5, 86401, '269', null, true, NaN, Infinity]) {
    for (const field of ['total', 'lap']) {
      const value = schema2();
      if (field === 'total') value.total_target_s = bad;
      else value.laps[0].lap_target_s = bad;
      assert.throws(() => parseStrategy(JSON.stringify(value), course));
    }
  }
});

test('schema 2 still rejects unknown and duplicate fields at every level', () => {
  const text = JSON.stringify(schema2());
  assert.throws(() => parseStrategy(text.replace('"total_target_s":2270', '"total_target_s":2270,"total_target_s":3000'), course), /重複/);
  assert.throws(() => parseStrategy(text.replace('"lap_target_s":269', '"lap_target_s":269,"lap_target_s":300'), course), /重複/);
  for (const mutate of [
    plan => { plan.extra = 1; },
    plan => { plan.laps[0].extra = 1; },
    plan => { plan.laps[0].runs[0].extra = 1; },
  ]) {
    const value = schema2(); mutate(value);
    assert.throws(() => parseStrategy(JSON.stringify(value), course), /のみ指定/);
  }
});
