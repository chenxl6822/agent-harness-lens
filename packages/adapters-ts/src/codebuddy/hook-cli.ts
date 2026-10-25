// src/codebuddy/hook-cli.ts — hs-hook 入口
// 契约：stdin -> 归一化 -> spool；任何情况下 stdout 0 字节、exit 0（失败只计数不阻断）。
// HS_DEBUG=1 时诊断信息只写 stderr。
import { enableCompileCache } from 'node:module';
// V8 编译缓存：显著降低 hook 冷启动（高频小进程的关键优化）
try {
  enableCompileCache();
} catch {
  // 旧版 Node 或目录不可写：忽略，退回无缓存
}
import { normalizeEvent } from '../normalize.js';
import { appendLine, bumpStat } from '../spool.js';
import { homedir } from 'node:os';

function debugLog(...args: unknown[]): void {
  if (process.env.HS_DEBUG === '1') {
    console.error('[hs-hook]', ...args);
  }
}

function readStdin(): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
    // 300ms 无输入即按 EOF 处理（防挂起的调用方不关 stdin，拖死 P95）
    setTimeout(() => resolve(data), 300).unref();
  });
}

async function main(): Promise<void> {
  let input: string;
  try {
    input = await readStdin();
  } catch (err) {
    bumpStat('parse_error');
    debugLog('stdin read failed', err);
    return;
  }

  let raw: unknown;
  try {
    raw = JSON.parse(input);
  } catch {
    bumpStat('parse_error');
    debugLog('stdin is not valid JSON');
    return;
  }

  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    bumpStat('malformed_payload');
    debugLog('stdin payload is not an object');
    return;
  }
  const obj = raw as Record<string, unknown>;
  if (typeof obj.hook_event_name !== 'string' || obj.hook_event_name.length === 0) {
    bumpStat('malformed_payload');
    debugLog('missing hook_event_name');
    return;
  }

  const contentCapture = process.env.HS_CONTENT_CAPTURE === '1' || process.env.HS_CONTENT_CAPTURE === 'true';
  let event;
  try {
    event = normalizeEvent(obj, {
      contentCapture,
      homeDir: homedir(),
      ...(process.env.HS_RUN_ID ? { runId: process.env.HS_RUN_ID } : {}),
    });
  } catch (err) {
    bumpStat('malformed_payload');
    debugLog('normalize failed', err);
    return;
  }
  if (event === null) {
    bumpStat('dropped_unknown_event');
    debugLog('unknown hook_event_name:', String(obj.hook_event_name));
    return;
  }

  const result = appendLine(event.run_id, JSON.stringify(event));
  if (!result.ok) {
    bumpStat('spool_error');
    debugLog('spool failed:', result.reason);
    return;
  }
  debugLog('wrote', result.path, event.type);
}

main()
  .catch((err) => {
    bumpStat('spool_error');
    debugLog('unexpected error', err);
  })
  .finally(() => {
    process.exitCode = 0; // 失败不阻断：永远 exit 0，永不写 stdout
  });
