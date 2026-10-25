#!/usr/bin/env node
// scripts/bench_hook.mjs — T02 G6 性能门禁
// ① 进程总耗时：spawn hook-cli.js 200 次（含 Node 冷启动），输出 P50/P95，P95 < 150ms
// ② 处理逻辑本身：进程内 normalize + spool 单次耗时，P95 < 10ms
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const hookCli = join(repoRoot, 'packages', 'adapters-ts', 'dist', 'codebuddy', 'hook-cli.js');
const N = 200;

const payload = JSON.stringify({
  hook_event_name: 'PreToolUse',
  session_id: 'bench-session',
  tool_name: 'Read',
  tool_input: {
    file_path: 'packages/adapters-ts/src/normalize.ts',
    limit: 100,
    offset: 1,
  },
});

function percentile(sorted, p) {
  const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[idx];
}

// ---------- ① 进程总耗时 ----------
const spoolA = mkdtempSync(join(tmpdir(), 'hs-bench-proc-'));
const procMs = [];
for (let i = 0; i < N; i++) {
  const t0 = performance.now();
  const r = spawnSync(process.execPath, [hookCli], {
    input: payload,
    env: { ...process.env, HS_SPOOL_DIR: spoolA, HS_DEBUG: '' },
  });
  const dt = performance.now() - t0;
  if (r.status !== 0) {
    console.error(`hook-cli exited ${r.status}`);
    process.exit(1);
  }
  procMs.push(dt);
}

// ---------- ② 进程内处理逻辑 ----------
const { normalizeEvent } = await import(
  pathToFileURL(join(repoRoot, 'packages', 'adapters-ts', 'dist', 'normalize.js')).href
);
const { appendLine } = await import(
  pathToFileURL(join(repoRoot, 'packages', 'adapters-ts', 'dist', 'spool.js')).href
);
const spoolB = mkdtempSync(join(tmpdir(), 'hs-bench-inproc-'));
process.env.HS_SPOOL_DIR = spoolB;
const raw = JSON.parse(payload);
const inprocMs = [];
for (let i = 0; i < N; i++) {
  const t0 = performance.now();
  const event = normalizeEvent(raw, { contentCapture: false });
  const r = appendLine(event.run_id, JSON.stringify(event));
  const dt = performance.now() - t0;
  if (!r.ok) {
    console.error('in-process spool failed:', r.reason);
    process.exit(1);
  }
  inprocMs.push(dt);
}

procMs.sort((a, b) => a - b);
inprocMs.sort((a, b) => a - b);

// 对照组：裸 Node 进程冷启动基线（hook 总耗时 - 基线 ≈ hook 净开销）
const baseMs = [];
for (let i = 0; i < 50; i++) {
  const t0 = performance.now();
  spawnSync(process.execPath, ['-e', ''], { stdio: 'ignore' });
  baseMs.push(performance.now() - t0);
}
baseMs.sort((a, b) => a - b);

const result = {
  count: N,
  process_p50_ms: +percentile(procMs, 50).toFixed(2),
  process_p95_ms: +percentile(procMs, 95).toFixed(2),
  inproc_p50_ms: +percentile(inprocMs, 50).toFixed(2),
  inproc_p95_ms: +percentile(inprocMs, 95).toFixed(2),
  bare_node_baseline_p95_ms: +percentile(baseMs, 95).toFixed(2),
  hook_net_overhead_p95_ms: +(percentile(procMs, 95) - percentile(baseMs, 95)).toFixed(2),
  thresholds: { process_p95_ms: 150, inproc_p95_ms: 10 },
};
console.log(JSON.stringify(result, null, 2));

const pass = result.process_p95_ms < 150 && result.inproc_p95_ms < 10;
console.log(pass ? 'bench PASS' : 'bench FAIL');
rmSync(spoolA, { recursive: true, force: true });
rmSync(spoolB, { recursive: true, force: true });
process.exit(pass ? 0 : 1);
