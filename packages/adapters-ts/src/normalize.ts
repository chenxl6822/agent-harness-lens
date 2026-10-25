// src/normalize.ts — 原始事件 -> TraceEvent。纯函数，无 I/O；时间与随机源可通过 opts 注入以便单测。
import { createHash, randomBytes } from 'node:crypto';
import type { TraceEvent } from './types.js';
import { SCHEMA_VERSION } from './types.js';
import { redactDeep } from './redact.js';

// ---------- ULID（Crockford Base32，48bit 时间 + 80bit 随机；自实现，不引包） ----------

const ENC = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

function encodeTime(t: number): string {
  let out = '';
  for (let i = 0; i < 10; i++) {
    out = ENC.charAt(t % 32) + out;
    t = Math.floor(t / 32);
  }
  return out;
}

function encodeRandom(): string {
  const bytes = randomBytes(10);
  let acc = 0;
  let bits = 0;
  let pos = 0;
  let out = '';
  for (let i = 0; i < 16; i++) {
    while (bits < 5 && pos < bytes.length) {
      acc = (acc << 8) | (bytes[pos] ?? 0);
      pos += 1;
      bits += 8;
    }
    out += ENC.charAt((acc >> (bits - 5)) & 31);
    bits -= 5;
  }
  return out;
}

export function ulid(now: number = Date.now()): string {
  return encodeTime(now) + encodeRandom();
}

// ---------- 工具 ----------

export function sha256Hex(s: string): string {
  return createHash('sha256').update(s, 'utf8').digest('hex');
}

/** payload 字符串里的 home 目录前缀替换为 ~（减少环境泄露）。 */
export function scrubHome(text: string, homeDir: string | undefined): string {
  if (!homeDir || homeDir.length < 2) return text;
  return text
    .replaceAll(homeDir.replace(/\\/g, '/'), '~')
    .replaceAll(homeDir, '~');
}

// ---------- 归一化 ----------

export interface NormalizeOptions {
  now?: Date;
  monoMs?: number;
  runId?: string;
  contentCapture?: boolean;
  homeDir?: string;
}

function str(v: unknown, fallback = 'unknown'): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback;
}

/**
 * 原始 hook/stdin 事件 -> TraceEvent。
 * 返回 null 表示事件不在映射范围内（调用方应计数丢弃，不得抛错）。
 * 注意：seq 恒为 0（占位值，仅在摄入前有效）——由 T05a collector 的
 * assign_seq()（纯函数，run 内全局单调、输入顺序无关、输出确定）统一赋号。
 */
export function normalizeEvent(
  raw: Record<string, unknown>,
  opts: NormalizeOptions = {},
): TraceEvent | null {
  const mapped = mapPayload(raw, opts);
  if (mapped === null) return null;
  const { type, payload, extraRedacted } = mapped;

  const sessionId = str(raw.session_id, 'unknown-session');
  const runId = opts.runId ?? (typeof process !== 'undefined' ? process.env.HS_RUN_ID : undefined) ?? `run-${sessionId}`;

  let finalPayload: Record<string, unknown> = payload;
  let redacted = extraRedacted;
  if (opts.contentCapture) {
    const r = redactDeep(payload);
    finalPayload = r.value as Record<string, unknown>;
    redacted = redacted || r.redacted;
  }
  // home 目录脱敏（无论内容捕获是否开启）
  finalPayload = scrubHomeDeep(finalPayload, opts.homeDir) as Record<string, unknown>;

  const event: TraceEvent = {
    schema_version: SCHEMA_VERSION,
    event_id: ulid((opts.now ?? new Date()).getTime()),
    run_id: runId,
    seq: 0,
    ts: (opts.now ?? new Date()).toISOString(),
    ts_mono_ms: opts.monoMs ?? monotonoMs(),
    session_id: sessionId,
    type,
    source: { platform: 'codebuddy', channel: 'hook' },
    labels: {},
    payload: finalPayload,
    ...(redacted ? { redacted: true } : {}),
  };
  return event;
}

function scrubHomeDeep(value: unknown, homeDir: string | undefined): unknown {
  if (typeof value === 'string') return scrubHome(value, homeDir);
  if (Array.isArray(value)) return value.map((v) => scrubHomeDeep(v, homeDir)) as never;
  if (value !== null && typeof value === 'object') {
    const obj: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (typeof v === 'string') obj[k] = scrubHome(v, homeDir);
      else if (v !== null && typeof v === 'object') obj[k] = scrubHomeDeep(v, homeDir);
      else obj[k] = v;
    }
    return obj;
  }
  return value as never;
}

/** 同机单调时钟毫秒（performance.now，单调、只用于同机相对时长）。 */
export function monotonoMs(): number {
  return Math.round(performance.now());
}

// ---------- 事件映射（表驱动，§2.3 权威表） ----------

export interface MappedEvent {
  type: TraceEvent['type'];
  payload: Record<string, unknown>;
  /** 内容捕获开启时带入的原文是否已触发脱敏替换 */
  extraRedacted?: boolean;
}

type PayloadBuilder = (raw: Record<string, unknown>, opts: NormalizeOptions) => MappedEvent;

function contentWithCapture(
  rawText: unknown,
  hashOf: string,
  opts: NormalizeOptions,
  field: { textKey: string; hashKey: string; bytesKey: string },
): { base: Record<string, unknown>; redacted: boolean } {
  const base: Record<string, unknown> = { [field.hashKey]: hashOf };
  if (typeof rawText === 'string') base[field.bytesKey] = Buffer.byteLength(rawText, 'utf8');
  if (opts.contentCapture && typeof rawText === 'string') {
    base[field.textKey] = rawText;
    return { base, redacted: true };
  }
  return { base, redacted: false };
}

export const HOOK_EVENT_MAP: Record<string, PayloadBuilder> = {
  SessionStart: (raw) => ({
    type: 'session.start',
    payload: { matcher: str(raw.matcher ?? raw.source, 'startup') },
  }),
  UserPromptSubmit: (raw, opts) => {
    const prompt = typeof raw.prompt === 'string' ? raw.prompt : '';
    const { base, redacted } = contentWithCapture(prompt, sha256Hex(prompt), opts, {
      textKey: 'prompt',
      hashKey: 'prompt_hash',
      bytesKey: 'prompt_bytes',
    });
    return { type: 'turn.start', payload: base, extraRedacted: redacted };
  },
  PreToolUse: (raw, opts) => {
    const toolName = str(raw.tool_name, 'unknown-tool');
    const argsJson = JSON.stringify(raw.tool_input ?? {});
    const { base, redacted } = contentWithCapture(argsJson, sha256Hex(argsJson), opts, {
      textKey: 'args',
      hashKey: 'args_hash',
      bytesKey: 'args_bytes',
    });
    return {
      type: 'tool.call',
      payload: { tool_name: toolName, ...base },
      extraRedacted: redacted,
    };
  },
  PostToolUse: (raw) => {
    const payload: Record<string, unknown> = {
      tool_name: str(raw.tool_name, 'unknown-tool'),
      status: 'ok',
    };
    if (typeof raw.duration_ms === 'number') payload.duration_ms = raw.duration_ms;
    if (typeof raw.tool_use_id === 'string') payload.tool_use_id = raw.tool_use_id;
    return { type: 'tool.result', payload };
  },
  PostToolUseFailure: (raw) => ({
    type: 'tool.error',
    payload: {
      tool_name: str(raw.tool_name, 'unknown-tool'),
      error_type: str(raw.error_type, 'unknown'),
    },
  }),
  SubagentStart: (raw) => ({
    type: 'agent.spawn',
    payload: {
      agent_id: str(raw.agent_id, 'unknown-agent'),
      ...(typeof raw.parent_agent_id === 'string' ? { parent_agent_id: raw.parent_agent_id } : {}),
    },
  }),
  SubagentStop: (raw) => ({
    type: 'agent.stop',
    payload: { agent_id: str(raw.agent_id, 'unknown-agent') },
  }),
  TaskCreated: (raw) => ({
    type: 'task.create',
    payload: {
      task_id: str(raw.task_id, 'unknown-task'),
      ...(typeof raw.owner === 'string' ? { owner: raw.owner } : {}),
    },
  }),
  TaskCompleted: (raw) => ({
    type: 'task.complete',
    payload: {
      task_id: str(raw.task_id, 'unknown-task'),
      ...(typeof raw.owner === 'string' ? { owner: raw.owner } : {}),
    },
  }),
  PreCompact: (raw) => ({
    type: 'context.compact',
    payload: { trigger: raw.trigger === 'manual' ? 'manual' : 'auto' },
  }),
  Stop: (raw) => ({
    type: 'turn.end',
    payload: { stop_hook_active: Boolean(raw.stop_hook_active) },
  }),
  SessionEnd: (raw) => ({
    type: 'session.end',
    payload: { reason: str(raw.reason, 'unknown') },
  }),
};

/** 本通道映射覆盖的全部 TraceEvent.type（去重，供 capability 报告使用）。 */
export const MAPPED_EVENT_TYPES: TraceEvent['type'][] = [
  ...new Set(Object.values(HOOK_EVENT_MAP).map((b) => b({} as Record<string, unknown>, {}).type)),
];

function mapPayload(raw: Record<string, unknown>, opts: NormalizeOptions): MappedEvent | null {
  const name = raw.hook_event_name;
  if (typeof name !== 'string') return null;
  const builder = HOOK_EVENT_MAP[name];
  if (!builder) return null;
  return builder(raw, opts);
}
