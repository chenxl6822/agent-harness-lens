// packages/schema/index.ts — 跨语言契约唯一真源入口
// SCHEMA_VERSION 变更必须同步四份 *.schema.json 中的 const "1.0.0"

export const SCHEMA_VERSION = '1.0.0' as const;

export const SCHEMA_FILES = {
  'trace-event': 'trace-event.schema.json',
  'metric-set': 'metric-set.schema.json',
  taskset: 'taskset.schema.json',
  capability: 'capability.schema.json',
} as const satisfies Record<string, string>;

export type SchemaKind = keyof typeof SCHEMA_FILES;

const SCHEMA_DIR = 'packages/schema';

export function schemaPath(kind: SchemaKind): string {
  return `${SCHEMA_DIR}/${SCHEMA_FILES[kind]}`;
}

export function schemaId(kind: SchemaKind): string {
  return `https://harness-scope.dev/schema/${kind}.json`;
}

export type { TraceEvent } from './generated/ts/trace-event';
export type { MetricSet } from './generated/ts/metric-set';
export type { TaskSet } from './generated/ts/taskset';
export type { CapabilityReport } from './generated/ts/capability';
