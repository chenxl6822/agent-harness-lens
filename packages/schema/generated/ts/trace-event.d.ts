// AUTO-GENERATED, DO NOT EDIT

export type AgentRole = 'lead' | 'teammate' | 'subagent' | 'system';
/**
 * 事件类型，共 26 个，逐一与架构文档 §4.1 一致，缺一即返工
 */
export type EventType =
  | 'run.start'
  | 'run.end'
  | 'session.start'
  | 'session.end'
  | 'agent.spawn'
  | 'agent.stop'
  | 'turn.start'
  | 'turn.end'
  | 'step.start'
  | 'step.end'
  | 'llm.request'
  | 'llm.response'
  | 'tool.call'
  | 'tool.result'
  | 'tool.error'
  | 'task.create'
  | 'task.claim'
  | 'task.update'
  | 'task.complete'
  | 'message.send'
  | 'message.recv'
  | 'artifact.write'
  | 'artifact.ref'
  | 'context.compact'
  | 'error'
  | 'metric.custom';
export type Platform = 'codebuddy' | 'dsh' | 'generic';
export type Channel = 'hook' | 'file' | 'otlp' | 'sdk' | 'cli';
/**
 * 降级通道等级（dsh 三重降级链）
 */
export type DegradedLevel = 'L1' | 'L2' | 'L3';

/**
 * 唯一写入原语：所有派生实体均由事件流物化而来。字段定义与架构文档 §2.4.1 逐字一致。
 */
export interface TraceEvent {
  /**
   * 契约版本，固定为 SCHEMA_VERSION '1.0.0'
   */
  schema_version: '1.0.0';
  /**
   * ULID，字典序即时间序
   */
  event_id: string;
  /**
   * 评测执行单元 ID
   */
  run_id: string;
  /**
   * run 内单调自增序号（断点续跑用）
   */
  seq: number;
  /**
   * 事件发生时间，ISO 8601 UTC，毫秒精度（墙钟）
   */
  ts: string;
  /**
   * 同机单调时钟毫秒值（clock_gettime MONOTONIC / performance.now 来源），用于同机精确耗时计算，跨机不可比
   */
  ts_mono_ms?: number;
  /**
   * 被测平台会话 ID
   */
  session_id: string;
  /**
   * 产生者智能体 ID（Lead / Teammate / Subagent）
   */
  agent_id?: string;
  agent_role?: AgentRole;
  /**
   * 委托链上的父智能体 ID
   */
  parent_agent_id?: string;
  /**
   * OpenTelemetry trace id（GenAI 语义约定对齐）
   */
  trace_id?: string;
  /**
   * OpenTelemetry span id
   */
  span_id?: string;
  /**
   * OpenTelemetry 父 span id
   */
  parent_span_id?: string;
  type: EventType;
  source: Source;
  /**
   * 维度标签（variant / model / taskset / task_id / team 等），开放结构
   */
  labels: {
    [k: string]: unknown;
  };
  /**
   * 事件主体（已脱敏；未知字段原样透传），开放结构
   */
  payload: {
    [k: string]: unknown;
  };
  /**
   * 原始记录归档指针（file:line 或 jsonl offset）
   */
  raw_ref?: string;
  /**
   * 本事件是否命中脱敏规则
   */
  redacted?: boolean;
}
export interface Source {
  platform: Platform;
  /**
   * 被测平台版本号（如 dsh 0.2.1-rc.2）
   */
  version?: string;
  channel: Channel;
  degraded?: DegradedLevel;
}
