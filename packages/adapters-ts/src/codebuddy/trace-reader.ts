// src/codebuddy/trace-reader.ts — 通道 A：traces/<pid>/trace_<traceId>.jsonl → TraceEvent[]（T03）
// 结构化执行轨迹（主结构源）。映射表见 docs/file-contract.md §映射。
// 内容策略：toolInput / attributes 默认不落盘（只存 args_hash + args_bytes）；
// 仅 HS_CONTENT_CAPTURE=on 时保留原文，且必须过 redact.ts。
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import type { TraceEvent, EventType, AgentRole } from '../types.js';
import { SCHEMA_VERSION } from '../types.js';
import { ulid, sha256Hex } from '../normalize.js';
import { redactDeep } from '../redact.js';
import { scrubHome } from '../normalize.js';
import { tailJsonl, CursorStore, type TailCursor } from './jsonl-tail.js';
import { codebuddyPaths, cursorFilePath, type PathResolveOptions } from './paths.js';

export interface FileChannelOptions extends PathResolveOptions {
  /** 游标文件覆盖（测试用） */
  cursorPath?: string;
  /** 内容捕获，默认 false（安全默认） */
  contentCapture?: boolean;
  /** run_id 覆盖；缺省 run-<sessionId> */
  runId?: string;
  /** home 目录前缀脱敏基准，默认 os.homedir() */
  homeDir?: string;
}

export interface CollectResult {
  events: TraceEvent[];
  warnings: string[];
  stats: {
    filesRead: number;
    filesSkipped: number;
    skippedLines: number;
    truncatedFiles: number;
  };
}

// ---------- 共享小工具（三通道各自内联，避免引入卡外新文件） ----------

/** 递归把 payload 中字符串里的 home 前缀替换为 ~（与 normalize.ts 同策略，此处内联副本）。 */
function scrubDeep(value: unknown, homeDir: string | undefined): unknown {
  if (typeof value === 'string') return scrubHome(value, homeDir);
  if (Array.isArray(value)) return value.map((v) => scrubDeep(v, homeDir));
  if (value !== null && typeof value === 'object') {
    const obj: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) obj[k] = scrubDeep(v, homeDir);
    return obj;
  }
  return value;
}

export interface MakeEventInput {
  type: EventType;
  sessionId: string;
  /** ISO-8601（通道内已统一转换） */
  ts: string;
  payload: Record<string, unknown>;
  runId: string;
  /* 以下可选字段允许显式传 undefined（exactOptionalPropertyTypes 下等价于缺省） */
  traceId?: string | undefined;
  spanId?: string | undefined;
  parentSpanId?: string | undefined;
  agentId?: string | undefined;
  agentRole?: AgentRole | undefined;
  rawRef?: string | undefined;
  homeDir?: string | undefined;
  contentCapture?: boolean | undefined;
}

export function makeEvent(input: MakeEventInput): TraceEvent {
  let payload = scrubDeep(input.payload, input.homeDir) as Record<string, unknown>;
  let redacted = false;
  if (input.contentCapture) {
    const r = redactDeep(payload);
    payload = r.value as Record<string, unknown>;
    redacted = r.redacted;
  }
  const tsMs = Date.parse(input.ts);
  const event: TraceEvent = {
    schema_version: SCHEMA_VERSION,
    event_id: ulid(Number.isFinite(tsMs) ? tsMs : Date.now()),
    run_id: input.runId,
    seq: 0,
    ts: input.ts,
    session_id: input.sessionId,
    ...(input.traceId !== undefined ? { trace_id: input.traceId } : {}),
    ...(input.spanId !== undefined ? { span_id: input.spanId } : {}),
    ...(input.parentSpanId !== undefined ? { parent_span_id: input.parentSpanId } : {}),
    ...(input.agentId !== undefined ? { agent_id: input.agentId } : {}),
    ...(input.agentRole !== undefined ? { agent_role: input.agentRole } : {}),
    type: input.type,
    source: { platform: 'codebuddy', channel: 'file' },
    labels: {},
    payload,
    ...(input.rawRef !== undefined ? { raw_ref: input.rawRef } : {}),
    ...(redacted ? { redacted: true } : {}),
  };
  return event;
}

/** 内容三件套：hash 恒存、bytes 恒存（有原文时）、原文仅 capture 时（交由 makeEvent 统一脱敏）。 */
export function contentFields(
  raw: string | undefined,
  capture: boolean,
  keys: { text: string; hash: string; bytes: string },
): { fields: Record<string, unknown>; hasRaw: boolean } {
  const text = typeof raw === 'string' ? raw : '';
  const fields: Record<string, unknown> = { [keys.hash]: sha256Hex(text) };
  if (typeof raw === 'string') fields[keys.bytes] = Buffer.byteLength(raw, 'utf8');
  if (capture && typeof raw === 'string' && raw.length > 0) {
    fields[keys.text] = raw;
    return { fields, hasRaw: true };
  }
  return { fields, hasRaw: false };
}

export function epochMsToIso(ms: unknown): string | undefined {
  return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toISOString() : undefined;
}

/** run_id 缺省规则：与 normalize.ts 保持一致（run-<sessionId>）。 */
export function defaultRunId(sessionId: string, runId?: string): string {
  return runId ?? process.env.HS_RUN_ID ?? `run-${sessionId}`;
}

// ---------- 通道 A ----------

interface SpanRecord {
  traceId: string;
  spanId: string;
  parentId: string | null;
  name: string;
  type: string;
  startedAt: string;
  endedAt: string | null;
  duration: number | null;
  status: string;
  error: string | null;
  toolName?: string;
  toolInput?: string;
  agentName?: string;
  attributes?: Record<string, unknown>;
}

/** span 类别 → 事件映射：function→tool.*，generation→llm.*，agent→agent.*，其余→step.*
 *  type=agent 为多智能体验证（T03 §6）发现的 span 类别：根智能体 agentName=cli，子智能体 agentName=子智能体名 */
export function classifySpan(name: string, type: string): 'tool' | 'llm' | 'agent' | 'step' {
  if (type === 'generation' || name === 'generation') return 'llm';
  if (type === 'function') return 'tool';
  if (type === 'agent') return 'agent';
  return 'step';
}

function agentRoleForCategory(category: string): AgentRole {
  if (category === 'main') return 'lead';
  if (category === 'auxiliary') return 'system';
  return 'subagent';
}

function listTraceFiles(tracesDir: string): string[] {
  let pids: string[];
  try {
    pids = readdirSync(tracesDir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return [];
  }
  const files: string[] = [];
  for (const pid of pids) {
    let names: string[];
    try {
      names = readdirSync(join(tracesDir, pid));
    } catch {
      continue; // 目录被并发清理：跳过，不抛（§5 场景 4）
    }
    for (const name of names) {
      if (name.startsWith('trace_') && name.endsWith('.jsonl')) {
        files.push(join(tracesDir, pid, name));
      }
    }
  }
  return files.sort();
}

/** 通道 A：扫描 traces 目录下全部 jsonl，增量解析为 TraceEvent[]。目录不存在或被删 → 空结果不抛。 */
export function collectTraces(opts: FileChannelOptions = {}, cursors?: CursorStore): CollectResult {
  const warnings: string[] = [];
  const stats = { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 };
  const events: TraceEvent[] = [];
  const paths = codebuddyPaths(opts);
  let pidsExist = false;
  try {
    readdirSync(paths.tracesDir);
    pidsExist = true;
  } catch {
    /* 目录不存在 → 空结果 + warning（§5 场景 6） */
  }
  if (!pidsExist) {
    warnings.push(`traces 目录不存在或不可读：${paths.tracesDir}`);
    return { events, warnings, stats };
  }
  const store = cursors ?? new CursorStore(opts.cursorPath ?? cursorFilePath(opts));
  const capture = opts.contentCapture ?? false;
  const homeDir = opts.homeDir ?? homedir(); // 安全默认：未显式传入时脱敏基准取真实 home（T03-R1）

  for (const file of listTraceFiles(paths.tracesDir)) {
    // mtime 预过滤：mtime 早于上次游标消费且 size 未变 → 直接跳过（快速路径）
    const prev = store.get(file) as (TailCursor & { size?: number }) | undefined;
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
    const agentNamesSeen: string[] = (prev as (TailCursor & { agentNames?: string[] }) | undefined)?.agentNames ?? [];
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
    let linesConsumed = 0;
    for (let i = 0; i < out.lines.length; i++) {
      const line = out.lines[i];
      if (line === undefined) continue;
      let rec: Record<string, unknown>;
      try {
        rec = JSON.parse(line.charCodeAt(0) === 0xfeff ? line.slice(1) : line) as Record<string, unknown>;
      } catch {
        stats.skippedLines += 1;
        continue;
      }
      const rawRef = `${scrubHome(file, homeDir)}@${out.lineStarts[i]}`;
      const traceId = typeof rec.traceId === 'string' ? rec.traceId : undefined;
      const recType = rec.type;
      if (recType === 'trace_start' && rec.trace !== null && typeof rec.trace === 'object') {
        const t = rec.trace as Record<string, unknown>;
        const sessionId = strOr(t.traceId, file);
        events.push(
          makeEvent({
            type: 'session.start',
            sessionId,
            ts: strOr(t.startedAt, new Date().toISOString()),
            runId: defaultRunId(sessionId, opts.runId),
            payload: {
              trace_name: strOr(t.name, 'unknown'),
              worker_pid: t.workerPid,
              worker_hostname: t.workerHostname,
            },
            traceId: typeof t.traceId === 'string' ? t.traceId : undefined,
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
      } else if ((recType === 'span_start' || recType === 'span_end') && rec.span !== null && typeof rec.span === 'object') {
        const s = rec.span as unknown as SpanRecord;
        const sessionId = s.traceId ?? file;
        const cls = classifySpan(s.name, s.type);
        const isStart = recType === 'span_start';
        const payload: Record<string, unknown> = {
          span_name: s.name,
          span_type: s.type,
          status: s.status,
          ...(typeof s.duration === 'number' ? { duration_ms: s.duration } : {}),
          ...(s.error ? { error: s.error } : {}),
        };
        if (cls === 'tool') {
          payload.tool_name = s.toolName ?? s.name;
          const { fields } = contentFields(s.toolInput, capture, { text: 'args', hash: 'args_hash', bytes: 'args_bytes' });
          Object.assign(payload, fields);
          if (capture && s.attributes !== undefined) payload.attributes = s.attributes;
        }
        let type: EventType;
        let agentEv: { id: string; role: AgentRole } | undefined;
        if (cls === 'llm') type = isStart ? 'llm.request' : 'llm.response';
        else if (cls === 'tool') {
          if (isStart) type = 'tool.call';
          else type = s.status === 'error' ? 'tool.error' : 'tool.result';
        } else if (cls === 'agent') {
          // 多智能体（§6 实测）：span type=agent 携带 agentName（根=cli，子智能体=其名字）
          type = isStart ? 'agent.spawn' : 'agent.stop';
          const an = s.agentName ?? s.name;
          agentEv = { id: an, role: an === 'cli' ? 'lead' : 'subagent' };
          payload.agent_name = an;
          if (!agentNamesSeen.includes(an)) {
            agentNamesSeen.push(an);
            if (agentNamesSeen.length > 32) agentNamesSeen.splice(0, agentNamesSeen.length - 32);
          }
        } else type = isStart ? 'step.start' : 'step.end';
        events.push(
          makeEvent({
            type,
            sessionId,
            ts: isStart ? s.startedAt : (s.endedAt ?? s.startedAt),
            runId: defaultRunId(sessionId, opts.runId),
            payload,
            traceId: s.traceId,
            spanId: s.spanId,
            parentSpanId: s.parentId ?? undefined,
            ...(agentEv !== undefined ? { agentId: agentEv.id, agentRole: agentEv.role } : {}),
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
      } else if (recType === 'trace_end' && rec.trace !== null && typeof rec.trace === 'object') {
        const t = rec.trace as Record<string, unknown>;
        const summary = (rec.summary ?? {}) as Record<string, unknown>;
        const sessionId = strOr(t.traceId, file);
        const runId = defaultRunId(sessionId, opts.runId);
        const category = strOr(summary.category, 'main');
        const agentId = typeof summary.agentName === 'string' ? summary.agentName : undefined;
        const role = agentRoleForCategory(category);
        events.push(
          makeEvent({
            type: 'session.end',
            sessionId,
            ts: strOr(t.endedAt, new Date().toISOString()),
            runId,
            payload: {
              trace_name: strOr(t.name, 'unknown'),
              status: strOr(t.status, 'unknown'),
              ...(typeof t.duration === 'number' ? { duration_ms: t.duration } : {}),
            },
            traceId: typeof t.traceId === 'string' ? t.traceId : undefined,
            agentId,
            agentRole: role,
            rawRef,
            homeDir,
            contentCapture: capture,
          }),
        );
        if (category !== 'main' && (agentId === undefined || !agentNamesSeen.includes(agentId))) {
          // §3.3：category != main 时补发 agent.spawn / agent.stop（summary 只在 trace_end 出现）；
          // 若 span 流里已经发过同名 agent.*（T03 §6 实测路径），则不重复补发。
          events.push(
            makeEvent({
              type: 'agent.spawn',
              sessionId,
              ts: strOr(summary.startedAt, strOr(t.endedAt, new Date().toISOString())),
              runId,
              payload: { agent_name: agentId ?? 'unknown-agent', category },
              traceId: typeof t.traceId === 'string' ? t.traceId : undefined,
              agentId,
              agentRole: role,
              rawRef,
              homeDir,
            }),
          );
          events.push(
            makeEvent({
              type: 'agent.stop',
              sessionId,
              ts: strOr(summary.endedAt, strOr(t.endedAt, new Date().toISOString())),
              runId,
              payload: { agent_name: agentId ?? 'unknown-agent', category },
              traceId: typeof t.traceId === 'string' ? t.traceId : undefined,
              agentId,
              agentRole: role,
              rawRef,
              homeDir,
            }),
          );
        }
        const metricPayload: Record<string, unknown> = {
          metric: 'trace_summary',
          trace_id: t.traceId,
          span_count: summary.spanCount ?? t.spanCount,
          total_tokens: summary.totalTokens ?? t.totalTokens,
          error_count: summary.errorCount ?? 0,
        };
        if (typeof summary.firstErrorMessage === 'string') metricPayload.first_error_message = summary.firstErrorMessage;
        if (typeof summary.firstErrorSpanId === 'string') metricPayload.first_error_span_id = summary.firstErrorSpanId;
        if (typeof summary.firstErrorWhere === 'string') metricPayload.first_error_where = summary.firstErrorWhere;
        events.push(
          makeEvent({
            type: 'metric.custom',
            sessionId,
            ts: strOr(t.endedAt, new Date().toISOString()),
            runId,
            payload: metricPayload,
            traceId: typeof t.traceId === 'string' ? t.traceId : undefined,
            agentId,
            agentRole: role,
            rawRef,
            homeDir,
          }),
        );
      } else {
        stats.skippedLines += 1;
        continue;
      }
      linesConsumed += 1;
    }
    if (linesConsumed > 0 || out.truncated) {
      store.set(file, {
        path: file,
        ino: st.ino,
        dev: st.dev,
        size: st.size,
        offset: out.newOffset,
        agentNames: agentNamesSeen,
        ...(out.lastLineHash !== undefined ? { lastLineHash: out.lastLineHash } : {}),
      });
      stats.filesRead += 1;
    } else {
      stats.filesSkipped += 1;
    }
  }
  store.save();
  return { events, warnings, stats };
}

function strOr(v: unknown, fallback: string): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback;
}
