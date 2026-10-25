#!/usr/bin/env node
// scripts/check_gen.mjs — 漂移校验：TS 重生成比对 + Python models 重生成比对 + 哈希比对
import { resolvePython, run } from './lib.mjs';

run('node', ['scripts/gen_ts.mjs', '--check']);
const py = resolvePython();
run(py, ['scripts/gen_python_models.py', '--check']);
run(py, ['scripts/gen_hash.py', '--check']);
console.log('check-gen PASS');
