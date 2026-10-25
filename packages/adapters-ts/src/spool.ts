// src/spool.ts — NDJSON 追加写 spool + 故障计数。失败绝不抛错。
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import type { SpoolResult } from './types.js';

export const STATS_FILE = '_stats.json';

export function spoolDir(): string {
  return process.env.HS_SPOOL_DIR || join(homedir(), '.harness_scope', 'spool');
}

/**
 * 单行 NDJSON 追加（O_APPEND 语义）。目录不存在则建；失败返回 {ok:false,reason}，不抛。
 */
export function appendLine(runId: string, line: string): SpoolResult {
  try {
    const dir = spoolDir();
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `${runId}.jsonl`);
    appendFileSync(path, `${line}\n`, { flag: 'a' }); // append flag = 原子 O_APPEND 追加
    return { ok: true, path };
  } catch (err) {
    return { ok: false, reason: err instanceof Error ? err.message : String(err) };
  }
}

/** 故障/丢弃计数（累加写 _stats.json；非事务性可接受）。失败静默。 */
export function bumpStat(key: string): void {
  try {
    const dir = spoolDir();
    mkdirSync(dir, { recursive: true });
    const path = join(dir, STATS_FILE);
    let stats: Record<string, number> = {};
    if (existsSync(path)) {
      try {
        const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
        if (parsed !== null && typeof parsed === 'object') {
          stats = parsed as Record<string, number>;
        }
      } catch {
        stats = {}; // 文件损坏则重来（计数允许丢失）
      }
    }
    stats[key] = (stats[key] ?? 0) + 1;
    writeFileSync(path, `${JSON.stringify(stats, null, 2)}\n`, 'utf8');
  } catch {
    // 计数失败不影响主流程
  }
}

export function readStats(): Record<string, number> {
  const path = join(spoolDir(), STATS_FILE);
  if (!existsSync(path)) return {};
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
    return parsed !== null && typeof parsed === 'object' ? (parsed as Record<string, number>) : {};
  } catch {
    return {};
  }
}
