// src/capability.ts — 能力探测报告（复用 T01 capability.schema.json 契约）
import type { CapabilityReport, ChannelStatus } from './types.js';
import { MAPPED_EVENT_TYPES } from './normalize.js';

/** 本卡确认存在但不映射的 hook 事件（登记进报告 notes，理由见 docs/hook-contract.md）。 */
export const UNUSED_HOOK_EVENTS: ReadonlyArray<{ name: string; reason: string }> = [
  {
    name: 'PermissionRequest',
    reason: '无对应指标口径，接入只会污染 trace 语义（架构 §2.3 未定义相关指标）',
  },
  {
    name: 'Notification',
    reason: '纯 UI 通知事件，无评测口径；且高频低信息量',
  },
];

export function buildCapabilityReport(version?: string): CapabilityReport {
  const channels: [ChannelStatus, ...ChannelStatus[]] = [
    {
      channel: 'hook',
      status: 'ok',
      note: `SDK hooks 主通道：${MAPPED_EVENT_TYPES.length} 种事件类型已映射`,
    },
  ];
  return {
    platform: 'codebuddy',
    ...(version !== undefined ? { version } : {}),
    telemetry: 'ok',
    channels,
    supported_events: [...MAPPED_EVENT_TYPES],
    notes: [
      '未使用事件：' + UNUSED_HOOK_EVENTS.map((e) => e.name).join('、') + '（理由见 docs/hook-contract.md）',
      'hook 侧 seq 恒为 0 占位，由 T05 collector 入库时按 (run_id, ts_mono_ms, event_id) 单调赋号',
      '默认 HS_CONTENT_CAPTURE=off：prompt/args 只存 sha256 hash + 字节数，不存原文',
    ],
  };
}
