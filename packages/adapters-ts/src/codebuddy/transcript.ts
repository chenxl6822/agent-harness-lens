// src/codebuddy/transcript.ts — 通道 B：projects/<slug>/<sessionId>.jsonl → TraceEvent[]（T03）
// 会话全文（内容源）。跳过 file-history-snapshot；从 _meta.traceparent / baggage 提取关联键；
// function_call / function_call_result 按 callId 配对（跨批次配对状态持久化在游标中）。
// 内容策略：message.content / arguments / output 默认只存 hash + bytes；capture=on 才留原文（过 redact）。
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import type { TraceEvent } from '../types.js';
import { scrubHome } from '../normalize.js';
import { tailJsonl, CursorStore } from './jsonl-tail.js';
import { codebuddyPaths, cursorFilePath } from './paths.js';
import {
  makeEvent,
  contentFields,
  epochMsToIso,
  defaultRunId,
  type FileChannelOptions,
  type CollectResult,
} from './trace-reader.js';

export { classifySpan } from './trace-reader.js';

interface TranscriptCursor {
  offset: number;
  ino?: number;
  dev?: number;
  size?: number;
  /** 尚未等到 function_call_result 的 callId（FIFO，上限 512 防膨胀） */
  openCalls: string[];
}

const MAX_OPEN_CALLS = 512;

/** 从 W3C traceparent（00-<32hex>-<16hex>-<2hex>）提取 trace id。 */
export function parseTraceparent(tp: unknown): string | undefined {
  if (typeof tp !== 'string') return undefined;
  const parts = tp.split('-');
  return parts.length === 4 && /^[0-9a-f]{32}$/.test(parts[1] ?? '') ? parts[1] : undefined;
}

/** 从 baggage（k=v,k=v）提取 codebuddy.session_id。 */
export function parseBaggageSession(baggage: unknown): string | undefined {
  if (typeof baggage !== 'string') return undefined;
  for (const pair of baggage.split(',')) {
    const idx = pair.indexOf('=');
    if (idx < 0) continue;
    if (pair.slice(0, idx).trim() === 'codebuddy.session_id') {
      const v = pair.slice(idx + 1).trim();
      return v.length > 0 ? v : undefined;
    }
  }
  return undefined;
}

interface LineMeta {
  sessionId?: string | undefined;
  traceId?: string | undefined;
}

function extractMeta(rec: Record<string, unknown>): LineMeta {
  const meta = rec._meta;
  if (meta === null || typeof meta !== 'object') return {};
  const m = meta as Record<string, unknown>;
  return { traceId: parseTraceparent(m.traceparent), sessionId: parseBaggageSession(m.baggage) };
}

/** 任意 content 字段（string / 数组 / 对象）序列化为稳定字符串用于 hash。 */
function serializeContent(v: unknown): string | undefined {
  if (v === undefined) return undefined;
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/** 列出 projects/<slug> 下所有顶层会话 jsonl（不递归子目录）。 */
function listTranscriptFiles(projectsDir: string): string[] {
  let slugs: string[];
  try {
    slugs = readdirSync(projectsDir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return [];
  }
  const files: string[] = [];
  for (const slug of slugs) {
    let names: string[];
    try {
      names = readdirSync(join(projectsDir, slug));
    } catch {
      continue;
    }
    for (const name of names) {
      if (name.endsWith('.jsonl')) files.push(join(projectsDir, slug, name));
    }
  }
  return files.sort();
}

/** 通道 B：增量解析会话全文。目录不存在 → 空结果 + warning，不抛。 */
export function collectTranscripts(opts: FileChannelOptions = {}, cursors?: CursorStore): CollectResult {
  const warnings: string[] = [];
  const stats = { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 };
  const events: TraceEvent[] = [];
  const paths = codebuddyPaths(opts);
  let projectsExist = false;
  try {
    readdirSync(paths.projectsDir);
    projectsExist = true;
  } catch {
    warnings.push(`projects 目录不存在或不可读：${paths.projectsDir}`);
    return { events, warnings, stats };
  }
  const store = cursors ?? new CursorStore(opts.cursorPath ?? cursorFilePath(opts));
  const capture = opts.contentCapture ?? false;
  const homeDir = opts.homeDir ?? homedir(); // 安全默认：未显式传入时脱敏基准取真实 home（T03-R1）

  for (const file of listTranscriptFiles(paths.projectsDir)) {
    const prev = store.get(file) as TranscriptCursor | undefined;
    let st;
    try {
      st = statSync(file);
    } catch {
      stats.filesSkipped += 1;
      continue;
    }
    if (prev && prev.offset === st.size && st.size > 0 && prev.size === st.size) {
      stats.filesSkipped += 1;
      continue;
    }
    const out = tailJsonl(file, prev);
    if (out.truncated) {
      stats.truncatedFiles += 1;
      events.push(
        makeEvent({
          type: 'error',
          sessionId: file,
          ts: new Date().toISOString(),
          runId: 'run-file-collect',
          payload: { error_type: 'file-truncated', path: scrubHome(file, homeDir), reset_reason: out.resetReason ?? 'shrunk' },
          homeDir,
        }),
      );
    }
    const fileSessionId = basenameStem(file);
    const openCalls: string[] = prev?.openCalls ? [...prev.openCalls] : [];
    let consumed = 0;

    for (let i = 0; i < out.lines.length; i++) {
      let rec: Record<string, unknown>;
      try {
        const raw = out.lines[i];
        if (raw === undefined) continue;
        rec = JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw) as Record<string, unknown>;
      } catch {
        stats.skippedLines += 1;
        continue;
      }
      const recType = rec.type;
      if (recType === 'file-history-snapshot') continue; // 噪声，直接跳过
      const meta = extractMeta(rec);
      const sessionId = meta.sessionId ?? fileSessionId;
      const runId = defaultRunId(sessionId, opts.runId);
      const ts = epochMsToIso(rec.timestamp) ?? new Date().toISOString();
      const rawRef = `${scrubHome(file, homeDir)}@${out.lineStarts[i]}`;

      if (recType === 'message') {
        const role = typeof rec.role === 'string' ? rec.role : 'unknown';
        const contentStr = serializeContent(rec.content) ?? '';
        const { fields } = contentFields(contentStr, capture, { text: 'content', hash: 'content_hash', bytes: 'content_bytes' });
        events.push(
          makeEvent({
            type: role === 'user' ? 'message.recv' : 'message.send',
            sessionId,
            ts,
            runId,
            payload: { role, ...fields },
            traceId: meta.traceId,
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
      } else if (recType === 'reasoning') {
        const contentStr = serializeContent(rec.rawContent ?? rec.content) ?? '';
        const { fields } = contentFields(contentStr, capture, { text: 'content', hash: 'content_hash', bytes: 'content_bytes' });
        events.push(
          makeEvent({
            type: 'message.send',
            sessionId,
            ts,
            runId,
            payload: { kind: 'reasoning', ...fields },
            traceId: meta.traceId,
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
      } else if (recType === 'function_call') {
        const callId = typeof rec.callId === 'string' ? rec.callId : undefined;
        const name = typeof rec.name === 'string' ? rec.name : 'unknown-tool';
        const { fields } = contentFields(
          typeof rec.arguments === 'string' ? rec.arguments : serializeContent(rec.arguments),
          capture,
          { text: 'args', hash: 'args_hash', bytes: 'args_bytes' },
        );
        events.push(
          makeEvent({
            type: 'tool.call',
            sessionId,
            ts,
            runId,
            payload: { tool_name: name, ...(callId ? { call_id: callId } : {}), ...fields },
            traceId: meta.traceId,
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
        if (callId) {
          openCalls.push(callId);
          if (openCalls.length > MAX_OPEN_CALLS) openCalls.splice(0, openCalls.length - MAX_OPEN_CALLS);
        }
        // 任务板工具（与通道 C 交叉验证归属，§2.3）：只透传白名单字段，不落 toolInput 原文
        emitTaskToolEvent(events, rec, { sessionId, ts, runId, traceId: meta.traceId, rawRef, homeDir });
      } else if (recType === 'function_call_result') {
        const callId = typeof rec.callId === 'string' ? rec.callId : undefined;
        const name = typeof rec.name === 'string' ? rec.name : 'unknown-tool';
        const isError = rec.status === 'error';
        // callId 配对：结果找不到对应调用 → 配对异常，发 error 事件（§3.4）
        const pairIdx = callId ? openCalls.indexOf(callId) : -1;
        if (callId && pairIdx < 0) {
          events.push(
            makeEvent({
              type: 'error',
              sessionId,
              ts,
              runId,
              payload: { error_type: 'callid-unpaired', call_id: callId, direction: 'result-without-call' },
              traceId: meta.traceId,
              rawRef,
              homeDir,
            }),
          );
        } else if (pairIdx >= 0) {
          openCalls.splice(pairIdx, 1);
        }
        const outputStr = serializeContent(rec.output);
        const { fields: outFields } = contentFields(outputStr, false, { text: 'output', hash: 'output_hash', bytes: 'output_bytes' });
        if (isError) {
          events.push(
            makeEvent({
              type: 'tool.error',
              sessionId,
              ts,
              runId,
              payload: { tool_name: name, ...(callId ? { call_id: callId } : {}), error_type: 'tool-result-error' },
              traceId: meta.traceId,
              rawRef,
              homeDir,
            }),
          );
        } else {
          events.push(
            makeEvent({
              type: 'tool.result',
              sessionId,
              ts,
              runId,
              payload: { tool_name: name, ...(callId ? { call_id: callId } : {}), status: strOr(rec.status, 'ok'), ...outFields },
              traceId: meta.traceId,
              rawRef,
              homeDir,
            }),
          );
        }
      } else {
        stats.skippedLines += 1;
        continue;
      }
      consumed += 1;
    }

    // 本批次结束时仍未配对的调用：跨批次等待（游标持久化 openCalls），不发异常
    if (consumed > 0 || out.truncated) {
      store.set(file, {
        path: file,
        ino: st.ino,
        dev: st.dev,
        size: st.size,
        offset: out.newOffset,
        openCalls,
      });
      stats.filesRead += 1;
    } else {
      stats.filesSkipped += 1;
    }
  }
  store.save();
  return { events, warnings, stats };
}

/**
 * 任务板工具调用 → task.* 事件（§2.5 交叉验证）。只透传结构性字段（taskId/status/subject 等），
 * 绝不落 arguments 原文（G7：toolInput 原文不出现在落盘内容）。
 * 实测 TaskCreate/TaskUpdate 参数无 owner/assignee 字段（docs/file-contract.md 偏差表），
 * 故 task.claim 仅在参数显式含 owner 类字段时发出。
 */
function emitTaskToolEvent(
  events: TraceEvent[],
  rec: Record<string, unknown>,
  ctx: { sessionId: string; ts: string; runId: string; traceId?: string | undefined; rawRef: string; homeDir?: string | undefined },
): void {
  const name = typeof rec.name === 'string' ? rec.name : '';
  if (name !== 'TaskCreate' && name !== 'TaskUpdate') return;
  let args: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(typeof rec.arguments === 'string' ? rec.arguments : '{}');
    if (parsed !== null && typeof parsed === 'object') args = parsed as Record<string, unknown>;
  } catch {
    return;
  }
  const taskId = strOr(args.taskId, strOr(args.id, ''));
  const base = { sessionId: ctx.sessionId, ts: ctx.ts, runId: ctx.runId, traceId: ctx.traceId, rawRef: ctx.rawRef, homeDir: ctx.homeDir };
  if (name === 'TaskCreate') {
    events.push(
      makeEvent({
        type: 'task.create',
        ...base,
        payload: { task_id: taskId || 'unknown-task' },
      }),
    );
    return;
  }
  // TaskUpdate
  const owner = typeof args.owner === 'string' ? args.owner : typeof args.assignee === 'string' ? args.assignee : undefined;
  if (owner !== undefined) {
    events.push(makeEvent({ type: 'task.claim', ...base, payload: { task_id: taskId || 'unknown-task', owner } }));
    return;
  }
  if (args.status === 'completed') {
    events.push(makeEvent({ type: 'task.complete', ...base, payload: { task_id: taskId || 'unknown-task' } }));
  } else {
    events.push(
      makeEvent({
        type: 'task.update',
        ...base,
        payload: { task_id: taskId || 'unknown-task', ...(typeof args.status === 'string' ? { status: args.status } : {}) },
      }),
    );
  }
}

function basenameStem(p: string): string {
  const base = p.replace(/\\/g, '/').split('/').pop() ?? p;
  return base.endsWith('.jsonl') ? base.slice(0, -6) : base;
}

function strOr(v: unknown, fallback: string): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback;
}
