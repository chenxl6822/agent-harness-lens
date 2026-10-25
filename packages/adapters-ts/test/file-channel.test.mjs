// test/file-channel.test.mjs — T03 文件通道：§5 六个故障场景 + G4-G7 判据
// 全部用例使用临时 CODEBUDDY_HOME，不触碰真实 ~/.codebuddy。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { collectFromFiles, collectTraces, collectTranscripts, collectTasks, tailJsonl } from '../dist/codebuddy/index.js';

const HOME = 'C:\\Users\\testuser'; // 假 home，用于断言路径脱敏

function tmpRoot() {
  return mkdtempSync(join(tmpdir(), 'hs-t03-'));
}

function makeHome(root) {
  const home = join(root, 'cbhome');
  mkdirSync(join(home, 'traces', '111'), { recursive: true });
  mkdirSync(join(home, 'projects', 'slug-x'), { recursive: true });
  mkdirSync(join(home, 'tasks', 'sess-1'), { recursive: true });
  return home;
}

const OPTS = (home, cursorPath) => ({ home, cursorPath, homeDir: HOME });

const TRACE_LINES = [
  { type: 'trace_start', trace: { traceId: 'trace_abc', name: 'Agent workflow', workerPid: 111, workerHostname: 'H1', startedAt: '2026-10-25T00:00:00.000Z', status: 'running', spanCount: 0, totalTokens: 0 } },
  { type: 'span_start', span: { traceId: 'trace_abc', spanId: 'span_1', parentId: null, name: 'cli', type: 'custom', startedAt: '2026-10-25T00:00:01.000Z', status: 'running' } },
  { type: 'span_start', span: { traceId: 'trace_abc', spanId: 'span_2', parentId: 'span_1', name: 'Read', type: 'function', startedAt: '2026-10-25T00:00:02.000Z', status: 'running', toolName: 'Read', toolInput: '{"file_path":"C:/Users/testuser/secret.txt"}' } },
  { type: 'span_end', span: { traceId: 'trace_abc', spanId: 'span_2', parentId: 'span_1', name: 'Read', type: 'function', startedAt: '2026-10-25T00:00:02.000Z', endedAt: '2026-10-25T00:00:03.000Z', duration: 1000, status: 'ok' } },
  { type: 'span_start', span: { traceId: 'trace_abc', spanId: 'span_3', parentId: 'span_1', name: 'generation', type: 'generation', startedAt: '2026-10-25T00:00:04.000Z', status: 'running' } },
  { type: 'span_end', span: { traceId: 'trace_abc', spanId: 'span_3', parentId: 'span_1', name: 'generation', type: 'generation', startedAt: '2026-10-25T00:00:04.000Z', endedAt: '2026-10-25T00:00:05.000Z', duration: 1000, status: 'ok' } },
  {
    type: 'trace_end',
    trace: { traceId: 'trace_abc', name: 'Agent workflow', workerPid: 111, workerHostname: 'H1', startedAt: '2026-10-25T00:00:00.000Z', endedAt: '2026-10-25T00:01:00.000Z', duration: 60000, status: 'ok', spanCount: 3, totalTokens: 42 },
    summary: { traceId: 'trace_abc', name: 'Agent workflow', category: 'auxiliary', agentName: 'terminalTitleGenerator', startedAt: '2026-10-25T00:00:00.000Z', endedAt: '2026-10-25T00:01:00.000Z', duration: 60000, status: 'ok', spanCount: 3, totalTokens: 42, errorCount: 0 },
  },
];

const TRANS_LINES = [
  { timestamp: 1792894894220, type: 'file-history-snapshot', _meta: { traceparent: '00-f4f57455a3c13b54940f8186175d9241-23457e23fd6da981-01', baggage: 'codebuddy.session_id=sess-1' } },
  { id: 'm1', timestamp: 1792894894221, type: 'message', role: 'user', content: [{ type: 'input_text', text: 'hello 内容量测 TOKEN_MARKER_TOPSECRET' }], _meta: { traceparent: '00-f4f57455a3c13b54940f8186175d9241-23457e23fd6da981-01', baggage: 'codebuddy.session_id=sess-1' } },
  { id: 'fc1', timestamp: 1792894894222, type: 'function_call', callId: 'call_1', name: 'Bash', arguments: '{"command":"echo SK-ARG-MARKER"}', _meta: { baggage: 'codebuddy.session_id=sess-1' } },
  { timestamp: 1792894894223, type: 'function_call_result', callId: 'call_1', name: 'Bash', output: 'ok', status: 'success', _meta: { baggage: 'codebuddy.session_id=sess-1' } },
];

function writeJsonl(path, lines) {
  writeFileSync(path, lines.map((l) => JSON.stringify(l)).join('\n') + '\n', 'utf8');
}

// ---------- §5 场景 1 + G6：半行保护 ----------

test('半行保护：文件尾无换行不消费该行，补全后可正常读取', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const trace = join(home, 'traces', '111', 'trace_t1.jsonl');
  writeFileSync(trace, TRACE_LINES.slice(0, 2).map((l) => JSON.stringify(l)).join('\n') + '\n', 'utf8');
  // 半行（写入中）：无换行符
  appendFileSync(trace, JSON.stringify(TRACE_LINES[2]));
  const r1 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r1.stats.skippedLines, 0);
  assert.ok(!r1.events.some((e) => e.payload?.span_name === 'Read' && e.type === 'tool.call'), '半行不得被消费');
  // 补全
  appendFileSync(trace, '\n');
  const r2 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.ok(r2.events.some((e) => e.type === 'tool.call' && e.payload?.span_name === 'Read'), '补全后应读到该行');
  // 幂等：再跑一次无新增
  const r3 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r3.events.length, 0);
  rmSync(root, { recursive: true, force: true });
});

// ---------- §5 场景 2：坏行容错 ----------

test('坏行容错：非法 JSON 跳过并计数，其余正常', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const trace = join(home, 'traces', '111', 'trace_t2.jsonl');
  writeFileSync(trace, ['{not json', JSON.stringify(TRACE_LINES[0]), '{"also bad"', JSON.stringify(TRACE_LINES[6])].join('\n') + '\n', 'utf8');
  const r = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r.stats.skippedLines, 2);
  assert.equal(r.events.filter((e) => e.type === 'session.start').length, 1);
  assert.equal(r.events.filter((e) => e.type === 'session.end').length, 1);
  rmSync(root, { recursive: true, force: true });
});

// ---------- §5 场景 3：截断重写 ----------

test('截断检测：size < offset 时归零重读并发 error 事件', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const trace = join(home, 'traces', '111', 'trace_t3.jsonl');
  writeJsonl(trace, TRACE_LINES);
  const r1 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.ok(r1.events.length > 0);
  // 整体重写为更短内容
  writeJsonl(trace, [TRACE_LINES[0]]);
  const r2 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r2.stats.truncatedFiles, 1);
  const errEv = r2.events.find((e) => e.type === 'error' && e.payload?.error_type === 'file-truncated');
  assert.ok(errEv, '必须发 error 事件，不得静默');
  assert.equal(r2.events.filter((e) => e.type === 'session.start').length, 1, '归零重读');
  rmSync(root, { recursive: true, force: true });
});

// ---------- §5 场景 4：目录被删除 ----------

test('traces 目录被删：不抛异常，返回空', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  rmSync(join(home, 'traces'), { recursive: true, force: true });
  const r = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r.events.length, 0);
  assert.ok(r.warnings.length > 0);
  rmSync(root, { recursive: true, force: true });
});

// ---------- §5 场景 5：隐藏文件 ----------

test('tasks 下隐藏文件（.acp-v2-plan-revision）跳过不解析', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeFileSync(join(home, 'tasks', 'sess-1', '.acp-v2-plan-revision'), 'garbage{{{', 'utf8');
  writeFileSync(join(home, 'tasks', 'sess-1', '1.json'), JSON.stringify({ subject: 's', description: 'd', status: 'pending', id: '1', createdAt: 1792894999185, updatedAt: 1792894999185 }), 'utf8');
  const r = collectTasks(OPTS(home, join(root, 'cur.json')));
  assert.equal(r.events.length, 1);
  assert.equal(r.events[0].type, 'task.create');
  assert.equal(r.events[0].payload.created_at, '2026-10-25T02:23:19.185Z', '毫秒 epoch → ISO-8601');
  rmSync(root, { recursive: true, force: true });
});

// ---------- §5 场景 6：CODEBUDDY_HOME 不存在 ----------

test('CODEBUDDY_HOME 不存在：空数组 + warning，不崩溃', () => {
  const r = collectFromFiles({ home: join(tmpdir(), 'hs-nonexistent-zzz'), cursorPath: join(tmpdir(), 'hs-t03-cur6.json') });
  assert.equal(r.events.length, 0);
  assert.ok(r.warnings.some((w) => w.includes('CODEBUDDY_HOME 不存在')));
});

// ---------- G4：解析正确性（span 数、parentId 环检测） ----------

test('G4：span 事件数与文件一致、parent_span_id 正确、无环', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeJsonl(join(home, 'traces', '111', 'trace_t4.jsonl'), TRACE_LINES);
  const r = collectTraces(OPTS(home, join(root, 'cur.json')));
  const spanStarts = TRACE_LINES.filter((l) => l.type === 'span_start').length;
  const gotStarts = r.events.filter((e) => ['tool.call', 'llm.request', 'step.start'].includes(e.type)).length;
  assert.equal(gotStarts, spanStarts);
  const read = r.events.find((e) => e.type === 'tool.call');
  assert.equal(read.parent_span_id, 'span_1');
  assert.equal(read.trace_id, 'trace_abc');
  assert.equal(read.span_id, 'span_2');
  // 环检测：parent_span_id 不等于自身 span_id
  for (const e of r.events) {
    if (e.span_id) assert.notEqual(e.parent_span_id, e.span_id);
  }
  // 默认不落 toolInput 原文
  assert.equal(read.payload.args, undefined);
  assert.ok(read.payload.args_hash);
  assert.equal(read.payload.args_bytes, Buffer.byteLength('{"file_path":"C:/Users/testuser/secret.txt"}'));
  // generation → llm
  assert.ok(r.events.some((e) => e.type === 'llm.request'));
  assert.ok(r.events.some((e) => e.type === 'llm.response'));
  // trace_end auxiliary → agent.spawn + agent.stop + metric.custom
  assert.ok(r.events.some((e) => e.type === 'agent.spawn' && e.payload.agent_name === 'terminalTitleGenerator' && e.payload.category === 'auxiliary'));
  assert.ok(r.events.some((e) => e.type === 'agent.stop'));
  const metric = r.events.find((e) => e.type === 'metric.custom');
  assert.equal(metric.payload.span_count, 3);
  assert.equal(metric.payload.total_tokens, 42);
  assert.equal(metric.payload.error_count, 0);
  rmSync(root, { recursive: true, force: true });
});

// ---------- G5：增量幂等 ----------

test('G5：连续跑两次第二次为空，游标文件内容不变', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeJsonl(join(home, 'traces', '111', 'trace_t5.jsonl'), TRACE_LINES);
  writeJsonl(join(home, 'projects', 'slug-x', 'sess-1.jsonl'), TRANS_LINES);
  const cur = join(root, 'cur.json');
  const r1 = collectFromFiles(OPTS(home, cur));
  assert.ok(r1.events.length > 0);
  assert.ok(existsSync(cur));
  const curBefore = readFileSync(cur, 'utf8');
  const r2 = collectFromFiles(OPTS(home, cur));
  assert.deepEqual(r2.events, [], '第二次必须为空数组');
  assert.equal(readFileSync(cur, 'utf8'), curBefore, '游标文件内容不变');
  rmSync(root, { recursive: true, force: true });
});

// ---------- G7：脱敏 ----------

test('G7：capture=off 时落盘内容不含 toolInput 原文 / 用户名 / 密钥；capture=on 时过脱敏', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeJsonl(join(home, 'traces', '111', 'trace_g7.jsonl'), TRACE_LINES);
  writeJsonl(join(home, 'projects', 'slug-x', 'sess-1.jsonl'), TRANS_LINES);
  const r = collectTraces(OPTS(home, join(root, 'cur.json')));
  const all = JSON.stringify(r.events);
  assert.ok(!all.includes('secret.txt'), '不得含 toolInput 原文');
  assert.ok(!all.includes('testuser'), '不得含路径中的用户名');
  const r2 = collectTranscripts(OPTS(home, join(root, 'cur-g7b.json')));
  const all2 = JSON.stringify(r2.events);
  assert.ok(!all2.includes('TOKEN_MARKER_TOPSECRET'), 'message 原文不得落盘（capture=off）');
  assert.ok(!all2.includes('SK-ARG-MARKER'), 'arguments 原文不得落盘（capture=off）');
  assert.ok(!all2.includes('testuser'));
  assert.ok(r2.events.some((e) => e.payload.content_hash), 'hash 必须存在');
  // capture=on：原文保留但 home 前缀被替换为 ~（scrubHome），路径不含用户名
  const r3 = collectTraces({ ...OPTS(home, join(root, 'cur-g7c.json')), contentCapture: true });
  const read = r3.events.find((e) => e.type === 'tool.call');
  assert.ok(read.payload.args.includes('~/secret.txt'), 'capture=on 保留原文但 home 前缀脱敏');
  assert.ok(!JSON.stringify(r3.events).includes('testuser'));
  rmSync(root, { recursive: true, force: true });
});

// ---------- callId 配对 ----------

test('callId 配对：跨批次等待不误报；result 无 call 才发 error', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const f = join(home, 'projects', 'slug-x', 'sess-2.jsonl');
  // 第一批：只有 function_call
  writeJsonl(f, [TRANS_LINES[2]]);
  const r1 = collectTranscripts(OPTS(home, join(root, 'cur.json')));
  assert.ok(!r1.events.some((e) => e.payload?.error_type === 'callid-unpaired'), '跨批次等待，不误报');
  // 第二批：result 到达，配对成功
  appendFileSync(f, JSON.stringify(TRANS_LINES[3]) + '\n', 'utf8');
  const r2 = collectTranscripts(OPTS(home, join(root, 'cur.json')));
  assert.ok(!r2.events.some((e) => e.payload?.error_type === 'callid-unpaired'));
  assert.ok(r2.events.some((e) => e.type === 'tool.result' && e.payload.call_id === 'call_1'));
  // 第三批：孤儿 result
  appendFileSync(f, JSON.stringify({ ...TRANS_LINES[3], callId: 'call_orphan' }) + '\n', 'utf8');
  const r3 = collectTranscripts(OPTS(home, join(root, 'cur.json')));
  assert.ok(r3.events.some((e) => e.type === 'error' && e.payload.error_type === 'callid-unpaired' && e.payload.call_id === 'call_orphan'));
  rmSync(root, { recursive: true, force: true });
});

// ---------- 任务板状态机 ----------

test('任务板：create → update → complete；status 变化驱动；mtime+hash 幂等', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const f = join(home, 'tasks', 'sess-1', '7.json');
  const write = (status) =>
    writeFileSync(f, JSON.stringify({ subject: '任务七', description: 'd', status, id: '7', createdAt: 1792894999185, updatedAt: 1792895000000 }), 'utf8');
  write('pending');
  const r1 = collectTasks(OPTS(home, join(root, 'cur.json')));
  assert.deepEqual(r1.events.map((e) => e.type), ['task.create']);
  // 内容不变（mtime 可能变）：hash 未变不重复投递
  const r2 = collectTasks(OPTS(home, join(root, 'cur.json')));
  assert.equal(r2.events.length, 0);
  // in_progress → update
  write('in_progress');
  const r3 = collectTasks(OPTS(home, join(root, 'cur.json')));
  assert.deepEqual(r3.events.map((e) => e.type), ['task.update']);
  // completed → complete
  write('completed');
  const r4 = collectTasks(OPTS(home, join(root, 'cur.json')));
  assert.deepEqual(r4.events.map((e) => e.type), ['task.complete']);
  rmSync(root, { recursive: true, force: true });
});

// ---------- §6 多智能体：span type=agent ----------

test('§6：span type=agent → agent.spawn/agent.stop；主智能体不因 summary 重复补发', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  const lines = [
    { type: 'trace_start', trace: { traceId: 'trace_ma', name: 'Agent workflow', workerPid: 111, workerHostname: 'H1', startedAt: '2026-10-25T00:00:00.000Z', status: 'running', spanCount: 0, totalTokens: 0 } },
    { type: 'span_start', span: { traceId: 'trace_ma', spanId: 'span_r', parentId: null, name: 'cli', type: 'agent', startedAt: '2026-10-25T00:00:00.000Z', status: 'running', agentName: 'cli' } },
    { type: 'span_start', span: { traceId: 'trace_ma', spanId: 'span_sub', parentId: 'span_r', name: 'Explore', type: 'agent', startedAt: '2026-10-25T00:00:01.000Z', status: 'running', agentName: 'Explore' } },
    { type: 'span_end', span: { traceId: 'trace_ma', spanId: 'span_sub', parentId: 'span_r', name: 'Explore', type: 'agent', startedAt: '2026-10-25T00:00:01.000Z', endedAt: '2026-10-25T00:00:02.000Z', duration: 1000, status: 'ok', agentName: 'Explore' } },
    { type: 'span_end', span: { traceId: 'trace_ma', spanId: 'span_r', parentId: null, name: 'cli', type: 'agent', startedAt: '2026-10-25T00:00:00.000Z', endedAt: '2026-10-25T00:00:03.000Z', duration: 3000, status: 'ok', agentName: 'cli' } },
    { type: 'trace_end', trace: { traceId: 'trace_ma', name: 'Agent workflow', workerPid: 111, startedAt: '2026-10-25T00:00:00.000Z', endedAt: '2026-10-25T00:00:03.000Z', duration: 3000, status: 'ok', spanCount: 2, totalTokens: 0 }, summary: { traceId: 'trace_ma', category: 'main', agentName: 'cli', spanCount: 2, totalTokens: 0, errorCount: 0 } },
  ];
  writeJsonl(join(home, 'traces', '111', 'trace_ma.jsonl'), lines);
  const r = collectTraces(OPTS(home, join(root, 'cur.json')));
  const spawns = r.events.filter((e) => e.type === 'agent.spawn');
  const stops = r.events.filter((e) => e.type === 'agent.stop');
  assert.deepEqual(spawns.map((e) => e.payload.agent_name).sort(), ['Explore', 'cli']);
  assert.equal(spawns.find((e) => e.payload.agent_name === 'cli').agent_role, 'lead');
  assert.equal(spawns.find((e) => e.payload.agent_name === 'Explore').agent_role, 'subagent');
  assert.equal(stops.length, 2);
  // category=main 且 span 流已发过 cli → summary 不重复补发
  assert.equal(r.events.filter((e) => e.type === 'agent.spawn' && e.payload.category === 'main').length, 0);
  // 幂等：重跑不重复
  const r2 = collectTraces(OPTS(home, join(root, 'cur.json')));
  assert.equal(r2.events.length, 0);
  rmSync(root, { recursive: true, force: true });
});

// ---------- T03-R1：raw_ref 默认不泄漏 os.homedir() ----------

import { homedir as realHomedir } from 'node:os';

const OPTS_NO_HOMEDIR = (home, cursorPath) => ({ home, cursorPath }); // 不传 homeDir → 应默认取 os.homedir()

/** 断言事件 JSON 不含真实 home 字面量（覆盖 Windows 反斜杠与正斜杠两种形态）。 */
function assertNoHomeLiteral(events) {
  const all = JSON.stringify(events);
  const h = realHomedir();
  assert.ok(!all.includes(h), `raw_ref 泄漏真实 home（反斜杠形态）: ${h}`);
  assert.ok(!all.includes(h.replace(/\\/g, '/')), `raw_ref 泄漏真实 home（正斜杠形态）: ${h.replace(/\\/g, '/')}`);
}

test('T03-R1：默认（不传 homeDir）三通道 raw_ref 不含 os.homedir() 字面量', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeJsonl(join(home, 'traces', '111', 'trace_r1.jsonl'), TRACE_LINES);
  writeJsonl(join(home, 'projects', 'slug-x', 'sess-r1.jsonl'), TRANS_LINES);
  writeFileSync(join(home, 'tasks', 'sess-1', 'r1.json'), JSON.stringify({ subject: 's', status: 'pending', id: 'r1', createdAt: 1792894999185, updatedAt: 1792894999185 }), 'utf8');
  const cur = join(root, 'cur.json');
  const t = collectTraces(OPTS_NO_HOMEDIR(home, cur));
  const b = collectTranscripts(OPTS_NO_HOMEDIR(home, join(root, 'cur-b.json')));
  const c = collectTasks(OPTS_NO_HOMEDIR(home, join(root, 'cur-c.json')));
  for (const r of [t, b, c]) {
    assert.ok(r.events.length > 0, '应产出事件');
    assertNoHomeLiteral(r.events);
    // Windows 反斜杠路径已被替换为 ~ 前缀（tmpdir 位于 home 之下时）
    const refs = r.events.map((e) => e.raw_ref ?? '').filter((s) => s.length > 0);
    assert.ok(refs.length > 0, '应有 raw_ref');
    for (const ref of refs) {
      if (ref.startsWith('~')) continue; // 已脱敏
      assert.ok(!ref.startsWith(realHomedir()), `raw_ref 未脱敏: ${ref}`);
    }
  }
  rmSync(root, { recursive: true, force: true });
});

test('T03-R1：显式传 homeDir 时仍按传入值替换（覆盖能力不丢）', () => {
  const root = tmpRoot();
  const home = makeHome(root);
  writeJsonl(join(home, 'traces', '111', 'trace_r1b.jsonl'), TRACE_LINES);
  writeJsonl(join(home, 'projects', 'slug-x', 'sess-r1b.jsonl'), TRANS_LINES);
  writeFileSync(join(home, 'tasks', 'sess-1', 'r1b.json'), JSON.stringify({ subject: 's', status: 'pending', id: 'r1b', createdAt: 1792894999185, updatedAt: 1792894999185 }), 'utf8');
  // 显式以 root（临时根）为脱敏基准：raw_ref 应以 ~ 开头，且不含 root 字面量
  const EXPLICIT = (cursorPath) => ({ home, cursorPath, homeDir: root });
  const t = collectTraces(EXPLICIT(join(root, 'cur.json')));
  const ref = t.events.find((e) => e.raw_ref).raw_ref;
  assert.ok(ref.startsWith('~'), `显式 homeDir 未生效: ${ref}`);
  assert.ok(!ref.includes(root), `显式 homeDir 未替换: ${ref}`);
  const b = collectTranscripts(EXPLICIT(join(root, 'cur-b.json')));
  assert.ok(b.events.every((e) => !e.raw_ref || e.raw_ref.startsWith('~')));
  const c = collectTasks(EXPLICIT(join(root, 'cur-c.json')));
  assert.ok(c.events.every((e) => !e.raw_ref || e.raw_ref.startsWith('~')));
  rmSync(root, { recursive: true, force: true });
});

// ---------- jsonl-tail 单元 ----------

test('tailJsonl：空文件、纯半行、CRLF', () => {
  const root = tmpRoot();
  const f = join(root, 'a.jsonl');
  // 空文件
  writeFileSync(f, '', 'utf8');
  let r = tailJsonl(f);
  assert.deepEqual(r.lines, []);
  assert.equal(r.newOffset, 0);
  // 纯半行
  writeFileSync(f, '{"partial":', 'utf8');
  r = tailJsonl(f);
  assert.deepEqual(r.lines, []);
  assert.equal(r.newOffset, 0);
  // CRLF
  writeFileSync(f, '{"a":1}\r\n{"b":2}\r\n', 'utf8');
  r = tailJsonl(f);
  assert.deepEqual(r.lines, ['{"a":1}', '{"b":2}']);
  assert.equal(r.newOffset, 18);
  // 半行 + 补全（offset 停在最后 \n 之后）
  appendFileSync(f, '{"c":');
  r = tailJsonl(f, { offset: 18 });
  assert.deepEqual(r.lines, []);
  assert.equal(r.newOffset, 18);
  appendFileSync(f, '3}\n');
  r = tailJsonl(f, { offset: 18 });
  assert.deepEqual(r.lines, ['{"c":3}']);
  rmSync(root, { recursive: true, force: true });
});

// ---------- 多块读取（大文件分块路径） ----------

test('tailJsonl：跨 64KB 块的长行不丢不裂', () => {
  const root = tmpRoot();
  const f = join(root, 'big.jsonl');
  const longVal = 'x'.repeat(150 * 1024);
  const line = JSON.stringify({ v: longVal });
  writeFileSync(f, line + '\n' + line + '\n', 'utf8');
  const r = tailJsonl(f);
  assert.equal(r.lines.length, 2);
  assert.equal(r.lines[0].length, line.length);
  assert.equal(r.lines[1].length, line.length);
  assert.equal(r.newOffset, line.length * 2 + 2);
  rmSync(root, { recursive: true, force: true });
});
