// scripts/lib.mjs — 跨平台构建辅助（仅 node 内置模块）
// Python 解释器探测顺序：.venv/Scripts/python.exe (Windows) -> .venv/bin/python -> .venv/bin/python3
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export function resolvePython() {
  for (const rel of ['.venv/Scripts/python.exe', '.venv/bin/python', '.venv/bin/python3']) {
    const candidate = join(repoRoot, rel);
    if (existsSync(candidate)) return candidate;
  }
  console.error('ERROR: .venv not found. Create it first: uv venv .venv');
  process.exit(1);
}

/** 同步运行子进程；非零退出码直接结束进程（透传子进程输出）。 */
export function run(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: 'inherit', cwd: repoRoot, shell: false });
  if (result.error) {
    console.error(`ERROR: failed to spawn ${cmd}: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) process.exit(result.status ?? 1);
}
