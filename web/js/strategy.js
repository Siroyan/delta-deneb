// Schema 1, matching delta-vega-v2/lib/vega_core/src/domain/strategy.cpp.
export const MAX_STRATEGY_BYTES = 4096;

// Retain duplicate keys for validation; JSON.parse would discard them.
export function parseStrategy(text, course) {
  const bytes = new TextEncoder().encode(text).length;
  if (!bytes || bytes > MAX_STRATEGY_BYTES) throw new Error('戦略ファイルは1〜4096バイトのUTF-8 JSONにしてください。');
  let index = 0;
  function space() { while (/[ \t\r\n]/.test(text[index] ?? '\0')) index++; }
  function take(expected) {
    space();
    if (text[index] !== expected) throw new Error(`JSONの形式が正しくありません（${index + 1}文字目）。`);
    index++;
  }
  function word() {
    take('"');
    const start = index;
    while (index < text.length && text[index] !== '"') index++;
    const result = text.slice(start, index);
    if (index === text.length || !/^[A-Za-z0-9_.-]+$/.test(result)) throw new Error('キーと識別子には英数字・_・-・.のみ使用できます。');
    index++;
    return result;
  }
  function value(depth = 0) {
    if (depth > 8) throw new Error('JSONの階層が深すぎます。');
    space();
    if (text[index] === '{') {
      index++;
      const object = Object.create(null);
      space();
      if (text[index] === '}') { index++; return object; }
      while (true) {
        const key = word();
        if (Object.hasOwn(object, key)) throw new Error(`重複した項目: ${key}`);
        take(':');
        object[key] = value(depth + 1);
        space();
        if (text[index] === '}') { index++; break; }
        take(',');
      }
      return object;
    }
    if (text[index] === '[') {
      index++;
      const array = [];
      space();
      if (text[index] === ']') { index++; return array; }
      while (true) {
        array.push(value(depth + 1));
        space();
        if (text[index] === ']') { index++; break; }
        take(',');
      }
      return array;
    }
    if (text[index] === '"') return word();
    const token = text.slice(index).match(/^\d+(?:\.\d+)?/)?.[0];
    if (!token || token.length >= 40) throw new Error('数値は0以上の通常の10進表記で、39文字以内にしてください。');
    index += token.length;
    const number = Number(token);
    if (!Number.isFinite(number)) throw new Error('数値の範囲が正しくありません。');
    return number;
  }
  const plan = value();
  space();
  if (index !== text.length) throw new Error('JSONの末尾または数値表記が正しくありません。指数表記は使用できません。');
  function exactKeys(object, keys, label) {
    if (!object || Array.isArray(object) || typeof object !== 'object' || Object.keys(object).length !== keys.length || !keys.every(key => Object.hasOwn(object, key))) throw new Error(`${label}の項目は ${keys.join(' / ')} のみ指定してください。`);
  }
  const legacy = plan?.plan_id === 'dummy-race-001' && !Object.hasOwn(plan, 'plan_type');
  exactKeys(plan, legacy ? ['schema_version', 'course_id', 'plan_id', 'laps'] : ['schema_version', 'course_id', 'plan_id', 'plan_type', 'laps'], 'トップ階層');
  if (plan.schema_version !== 1) throw new Error('schema_version は1のみ対応しています。');
  if (plan.course_id !== course.id) throw new Error(`コースIDが一致しません。この画面のコースは ${course.id} です。`);
  if (typeof plan.course_id !== 'string' || plan.course_id.length > 47) throw new Error('course_id は47文字以内にしてください。');
  if (typeof plan.plan_id !== 'string' || !/^[A-Za-z0-9_.-]{1,39}$/.test(plan.plan_id)) throw new Error('plan_id は39文字以内の英数字・_・-・.で指定してください。');
  if (legacy) plan.plan_type = 'demo';
  if (!['demo', 'race'].includes(plan.plan_type)) throw new Error('plan_type はdemoまたはraceにしてください。');
  if (!Array.isArray(plan.laps) || plan.laps.length !== course.lap_count || course.lap_count > 7) throw new Error(`全${course.lap_count}周の定義が必要です。戦略は最大7周まで対応しています。`);
  const seen = new Set();
  for (const lap of plan.laps) {
    exactKeys(lap, ['lap', 'route_id', 'runs'], '各周');
    if (!Number.isInteger(lap.lap) || lap.lap < 1 || lap.lap > course.lap_count || seen.has(lap.lap)) throw new Error('周回番号が範囲外、または重複しています。');
    seen.add(lap.lap);
    const expected = lap.lap === 1 ? 'first_lap' : lap.lap === course.lap_count ? 'final_lap' : 'regular_lap';
    if (lap.route_id !== expected) throw new Error(`LAP ${lap.lap}: route_id は${expected}にしてください。`);
    if (!Array.isArray(lap.runs) || lap.runs.length < 1 || lap.runs.length > 3) throw new Error(`LAP ${lap.lap}: ON/OFF区間は1〜3件にしてください。`);
    let previousOff = -1;
    for (const [runIndex, run] of lap.runs.entries()) {
      exactKeys(run, ['on_s_m', 'off_s_m'], `LAP ${lap.lap} 区間${runIndex + 1}`);
      const on = run.on_s_m;
      const off = run.off_s_m;
      const max = course.routes[expected].length_m;
      if (typeof on !== 'number' || typeof off !== 'number' || !Number.isFinite(on) || !Number.isFinite(off) || !(on >= 0 && on < off && off <= max && on > previousOff)) throw new Error(`LAP ${lap.lap} 区間${runIndex + 1}: 0 ≤ ON < OFF ≤ ${max.toFixed(1)} m、次のONは前のOFFより先にしてください。`);
      previousOff = off;
    }
  }
  plan.laps.sort((a, b) => a.lap - b.lap);
  return plan;
}

export function projectOnRoute(route, latitude, longitude) {
  if (typeof latitude !== 'number' || typeof longitude !== 'number' || !Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  const lonScale = 111000 * Math.cos(latitude * Math.PI / 180);
  let best = null;
  for (let i = 1; i < route.points.length; i++) {
    const a = route.points[i - 1];
    const b = route.points[i];
    const ax = (a.longitude - longitude) * lonScale;
    const ay = (a.latitude - latitude) * 111000;
    const bx = (b.longitude - longitude) * lonScale;
    const by = (b.latitude - latitude) * 111000;
    const dx = bx - ax;
    const dy = by - ay;
    const fraction = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy || 1)));
    const distance_m = Math.hypot(ax + dx * fraction, ay + dy * fraction);
    if (!best || distance_m < best.distance_m) best = { s: a.s + (b.s - a.s) * fraction, distance_m };
  }
  return best?.distance_m <= 60 ? best : null;
}

export function strategyCue(lap, s, length) {
  if (!lap || typeof s !== 'number' || !Number.isFinite(s)) return null;
  for (const run of lap.runs) {
    if (s <= run.on_s_m) return { mode: 'coast', next: 'ON', distance_m: run.on_s_m - s };
    if (s <= run.off_s_m) return { mode: 'engine', next: 'OFF', distance_m: run.off_s_m - s };
  }
  return { mode: 'coast', next: 'LAP END', distance_m: Math.max(0, length - s) };
}
