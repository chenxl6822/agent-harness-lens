#!/usr/bin/env node
// scripts/gen_all.mjs — 生成编排：TS 类型 + Python models + 哈希记录（串行）
import { resolvePython, run } from './lib.mjs';

run('node', ['scripts/gen_ts.mjs']);
const py = resolvePython();
run(py, ['scripts/gen_python_models.py']);
run(py, ['scripts/gen_hash.py']);
