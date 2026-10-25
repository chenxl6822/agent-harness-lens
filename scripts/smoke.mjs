#!/usr/bin/env node
// scripts/smoke.mjs — CLI 冒烟：version / schema list / 4 份 example 校验
import { resolvePython, run } from './lib.mjs';

const py = resolvePython();
const cli = ['-m', 'hs_core.cli'];

run(py, [...cli, 'version']);
run(py, [...cli, 'schema', 'list']);
const cases = [
  ['trace-event.sample.json', 'trace-event'],
  ['metric-set.sample.json', 'metric-set'],
  ['taskset.sample.yaml', 'taskset'],
  ['capability.sample.json', 'capability'],
];
for (const [file, kind] of cases) {
  run(py, [...cli, 'schema', 'check', `packages/schema/examples/${file}`, '--kind', kind]);
}
console.log('smoke PASS');
