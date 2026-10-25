// src/types.ts — 契约类型 re-export（唯一真源：packages/schema/generated/ts/*.d.ts）
// 禁止手写重复类型定义。

import type { TraceEvent, Platform, Channel } from '../../schema/generated/ts/trace-event.js';
import type { CapabilityReport } from '../../schema/generated/ts/capability.js';

export type {
  TraceEvent,
  Source,
  Platform,
  Channel,
  AgentRole,
  DegradedLevel,
  EventType,
} from '../../schema/generated/ts/trace-event.js';

export type {
  MetricSet,
  MetricValue,
  EvidenceRef,
} from '../../schema/generated/ts/metric-set.js';

export type { CapabilityReport, ChannelStatus } from '../../schema/generated/ts/capability.js';

/** 适配器统一接口：T03/T04 的通道也实现它。 */
export interface CollectorAdapter {
  /** 平台标识 */
  readonly platform: Platform;
  /** 采集通道 */
  readonly channel: Channel;
  /** 原始事件 -> TraceEvent（返回 null = 该事件不属于本通道映射范围） */
  normalize(raw: Record<string, unknown>): TraceEvent | null;
  /** 落盘一行 NDJSON；失败不抛 */
  write(event: TraceEvent): SpoolResult;
  /** 能力探测报告 */
  capabilities(): CapabilityReport;
}

export interface SpoolResult {
  ok: boolean;
  reason?: string;
  path?: string;
}

/** 契约版本（与 packages/schema/index.ts 的 SCHEMA_VERSION 保持一致） */
export const SCHEMA_VERSION = '1.0.0' as const;
