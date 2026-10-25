// test/mapping.test.mjs — §2.3 的 12 个事件映射逐个验证（G4 映射正确）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEvent, MAPPED_EVENT_TYPES, HOOK_EVENT_MAP } from '../dist/index.js';

const BASE = { session_id: 's-1', hook_event_name: '' };

function ev(name, extra = {}) {
  return normalizeEvent({ ...BASE, hook_event_name: name, ...extra }, { now: new Date(0), monoMs: 42, runId: 'run-t' });
}

test('SessionStart -> session.start', () => {
  const e = ev('SessionStart', { matcher: 'resume' });
  assert.equal(e.type, 'session.start');
  assert.deepEqual(e.payload, { matcher: 'resume' });
});

test('UserPromptSubmit -> turn.start（默认只存 hash+bytes）', () => {
  const e = ev('UserPromptSubmit', { prompt: 'hello world' });
  assert.equal(e.type, 'turn.start');
  assert.equal(typeof e.payload.prompt_hash, 'string');
  assert.equal(e.payload.prompt_bytes, 11);
  assert.equal(e.payload.prompt, undefined);
});

test('PreToolUse -> tool.call（args_hash，不存 args）', () => {
  const e = ev('PreToolUse', { tool_name: 'Read', tool_input: { file_path: 'a.ts' } });
  assert.equal(e.type, 'tool.call');
  assert.equal(e.payload.tool_name, 'Read');
  assert.equal(typeof e.payload.args_hash, 'string');
  assert.equal(e.payload.args, undefined);
});

test('PostToolUse -> tool.result（status=ok）', () => {
  const e = ev('PostToolUse', { tool_name: 'Read', duration_ms: 12 });
  assert.equal(e.type, 'tool.result');
  assert.equal(e.payload.status, 'ok');
  assert.equal(e.payload.duration_ms, 12);
});

test('PostToolUseFailure -> tool.error', () => {
  const e = ev('PostToolUseFailure', { tool_name: 'Bash', error_type: 'timeout' });
  assert.equal(e.type, 'tool.error');
  assert.equal(e.payload.error_type, 'timeout');
});

test('SubagentStart -> agent.spawn', () => {
  const e = ev('SubagentStart', { agent_id: 'a1', parent_agent_id: 'lead' });
  assert.equal(e.type, 'agent.spawn');
  assert.equal(e.payload.parent_agent_id, 'lead');
});

test('SubagentStop -> agent.stop', () => {
  const e = ev('SubagentStop', { agent_id: 'a1' });
  assert.equal(e.type, 'agent.stop');
  assert.equal(e.payload.agent_id, 'a1');
});

test('TaskCreated -> task.create', () => {
  const e = ev('TaskCreated', { task_id: 't1', owner: 'impl-01' });
  assert.equal(e.type, 'task.create');
  assert.equal(e.payload.owner, 'impl-01');
});

test('TaskCompleted -> task.complete', () => {
  const e = ev('TaskCompleted', { task_id: 't1' });
  assert.equal(e.type, 'task.complete');
});

test('PreCompact -> context.compact', () => {
  const e = ev('PreCompact', { trigger: 'auto' });
  assert.equal(e.type, 'context.compact');
  assert.equal(e.payload.trigger, 'auto');
});

test('Stop -> turn.end', () => {
  const e = ev('Stop', { stop_hook_active: true });
  assert.equal(e.type, 'turn.end');
  assert.equal(e.payload.stop_hook_active, true);
});

test('SessionEnd -> session.end', () => {
  const e = ev('SessionEnd', { reason: 'clear' });
  assert.equal(e.type, 'session.end');
  assert.equal(e.payload.reason, 'clear');
});

test('映射表恰好 12 个事件、覆盖 12 种 type', () => {
  assert.equal(Object.keys(HOOK_EVENT_MAP).length, 12);
  assert.equal(MAPPED_EVENT_TYPES.length, 12);
});
