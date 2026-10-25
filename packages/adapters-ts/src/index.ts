// src/index.ts — 统一导出 + 适配器工厂
import { homedir } from 'node:os';
import type { CollectorAdapter, TraceEvent, CapabilityReport, Platform, Channel, SpoolResult } from './types.js';
import { normalizeEvent, MAPPED_EVENT_TYPES } from './normalize.js';
import { appendLine } from './spool.js';
import { buildCapabilityReport } from './capability.js';

export * from './types.js';
export * from './redact.js';
export * from './spool.js';
export * from './capability.js';
export * from './normalize.js';
export * from './codebuddy/index.js';

export interface CreateAdapterOptions {
  runId?: string;
  contentCapture?: boolean;
  homeDir?: string;
}

/** CodeBuddy Hook 主通道适配器工厂。 */
export function createAdapter(opts: CreateAdapterOptions = {}): CollectorAdapter {
  const normalize = (raw: Record<string, unknown>): TraceEvent | null => {
    const normOpts: import('./normalize.js').NormalizeOptions = {
      contentCapture: opts.contentCapture ?? false,
      homeDir: opts.homeDir ?? homedir(),
    };
    if (opts.runId !== undefined) normOpts.runId = opts.runId;
    return normalizeEvent(raw, normOpts);
  };
  return {
    platform: 'codebuddy' as Platform,
    channel: 'hook' as Channel,
    normalize,
    write(event: TraceEvent): SpoolResult {
      return appendLine(event.run_id, JSON.stringify(event));
    },
    capabilities(): CapabilityReport {
      return buildCapabilityReport();
    },
  };
}

export { MAPPED_EVENT_TYPES };
