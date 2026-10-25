#!/usr/bin/env node
// scripts/py.mjs — 用 venv 里的 Python 运行任意命令（跨平台）
// 用法：node scripts/py.mjs -m ruff check engine
import { resolvePython, run } from './lib.mjs';

run(resolvePython(), process.argv.slice(2));
