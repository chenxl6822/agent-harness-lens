// src/codebuddy/hook-mapping.ts — §2.3 映射表的权威实现（表驱动）
// 权威表见任务卡 T02 §2.3；新增/修改事件必须先改任务卡。
import { HOOK_EVENT_MAP } from '../normalize.js';

export { HOOK_EVENT_MAP };

/** 已接入映射的 12 个 hook 事件名（CodeBuddy CLI 2.173.0 实测存在）。 */
export const MAPPED_HOOK_EVENTS: readonly string[] = [
  'SessionStart',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'PostToolUseFailure',
  'SubagentStart',
  'SubagentStop',
  'TaskCreated',
  'TaskCompleted',
  'PreCompact',
  'Stop',
  'SessionEnd',
];

/** 存在但本卡不映射（丢弃并计数，不报错）。 */
export const UNUSED_HOOK_EVENTS: readonly string[] = ['PermissionRequest', 'Notification'];

/**
 * mapHookEvent(name, payload) -> {type, payload} | null
 * 未知事件返回 null（调用方计数 dropped_unknown_event）。
 */
export function mapHookEvent(
  name: string,
  payload: Record<string, unknown>,
  opts: import('../normalize.js').NormalizeOptions = {},
): { type: import('../normalize.js').MappedEvent['type']; payload: Record<string, unknown> } | null {
  const builder = HOOK_EVENT_MAP[name];
  if (!builder) return null;
  const mapped = builder(payload, opts);
  return { type: mapped.type, payload: mapped.payload };
}
