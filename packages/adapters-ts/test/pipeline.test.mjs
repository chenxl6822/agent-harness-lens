// test/pipeline.test.mjs — normalize 字段契约、ULID、脱敏红队用例、spool、hook-cli 4 类故障
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, existsSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { normalizeEvent, ulid, redact, shannonEntropy, appendLine, readStats } from '../dist/index.js';

const PKG = join(dirname(fileURLToPath(import.meta.url)), '..');
const HOOK_CLI = join(PKG, 'dist', 'codebuddy', 'hook-cli.js');
// 红队假密钥（拼接构造，避免被密钥扫描器当作真实泄露）
const FAKE_KEY = `sk-${'a'.repeat(4)}${'b'.repeat(28)}`;

function tmpSpool() {
  return mkdtempSync(join(tmpdir(), 'hs-spool-'));
}

// ---------- ULID / normalize 契约 ----------

test('ULID：26 位 Crockford Base32', () => {
  const id = ulid(0);
  assert.match(id, /^[0-9A-HJKMNP-TV-Z]{26}$/);
});

test('normalize：seq=0 占位、ts ISO UTC、ts_mono_ms、source=codebuddy/hook', () => {
  const e = normalizeEvent(
    { hook_event_name: 'SessionStart', session_id: 's-9' },
    { now: new Date('2026-10-25T00:00:00Z'), monoMs: 7, runId: 'run-x' },
  );
  assert.equal(e.seq, 0);
  assert.equal(e.ts, '2026-10-25T00:00:00.000Z');
  assert.equal(e.ts_mono_ms, 7);
  assert.equal(e.run_id, 'run-x');
  assert.equal(e.session_id, 's-9');
  assert.deepEqual(e.source, { platform: 'codebuddy', channel: 'hook' });
  assert.equal(e.schema_version, '1.0.0');
  assert.match(e.event_id, /^[0-9A-HJKMNP-TV-Z]{26}$/);
});

test('normalize：未知事件返回 null', () => {
  assert.equal(normalizeEvent({ hook_event_name: 'Notification', session_id: 's' }, {}), null);
  assert.equal(normalizeEvent({ hook_event_name: 'PermissionRequest', session_id: 's' }, {}), null);
});

test('normalize：home 目录前缀替换为 ~', () => {
  const e = normalizeEvent(
    { hook_event_name: 'SessionEnd', session_id: 's', reason: 'C:\\Users\\me\\proj done' },
    { homeDir: 'C:\\Users\\me' },
  );
  assert.equal(e.payload.reason, '~\\proj done');
});

// ---------- 脱敏（§5 红队用例） ----------

test('红队：sk- + 32 位十六进制被替换', () => {
  const r = redact(`key=${FAKE_KEY} end`);
  assert.equal(r.redacted, true);
  assert.match(r.text, /\[REDACTED:api-key\]/);
  assert.ok(!r.text.includes(FAKE_KEY));
});

test('红队：Authorization Bearer 被替换', () => {
  const r = redact('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.token');
  assert.equal(r.redacted, true);
  assert.match(r.text, /\[REDACTED:bearer-token\]/);
  assert.ok(!r.text.includes('eyJhbGciOiJIUzI1NiJ9'));
});

test('红队：40 位随机 base64（高熵）被替换', () => {
  const token = 'Zm9vQmFyQmF6UXV1eENvcnKUDoW4vXeKk9m3aAb';
  const r = redact(`token ${token} ok`);
  assert.equal(r.redacted, true);
  assert.match(r.text, /\[REDACTED:high-entropy\]/);
  assert.ok(!r.text.includes(token));
});

test('红队：正常文件路径不被误伤', () => {
  const r = redact('see /home/user/a.py and C:/proj/src/index.ts');
  assert.equal(r.redacted, false);
  assert.equal(r.text, 'see /home/user/a.py and C:/proj/src/index.ts');
});

test('shannonEntropy：高熵 > 低熵', () => {
  assert.ok(shannonEntropy('Zm9vQmFyQmF6UXV1eENvcnKUDoW4vXeKk9m3aAb') >= 4.0);
  assert.ok(shannonEntropy('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') < 4.0);
});

test('内容捕获开：prompt 原文经脱敏后落 payload 且 redacted=true', () => {
  const e = normalizeEvent(
    { hook_event_name: 'UserPromptSubmit', session_id: 's', prompt: `use ${FAKE_KEY} please` },
    { contentCapture: true },
  );
  assert.equal(e.redacted, true);
  assert.match(String(e.payload.prompt), /\[REDACTED:api-key\]/);
  assert.ok(!JSON.stringify(e).includes(FAKE_KEY));
});

// ---------- spool ----------

test('spool：追加 NDJSON 成功，返回 ok+path', () => {
  const dir = tmpSpool();
  process.env.HS_SPOOL_DIR = dir;
  const r = appendLine('run-1', '{"a":1}');
  assert.equal(r.ok, true);
  assert.match(r.path, /run-1\.jsonl$/);
  const lines = readFileSync(join(dir, 'run-1.jsonl'), 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  assert.deepEqual(JSON.parse(lines[0]), { a: 1 });
});

test('spool：目录不可写（目标是文件）→ ok:false 不抛', () => {
  const dir = tmpSpool();
  const blocker = join(dir, 'blocker');
  writeFileSync(blocker, 'x');
  process.env.HS_SPOOL_DIR = blocker; // 一个文件不是目录，mkdir 失败
  const r = appendLine('run-2', '{"a":1}');
  assert.equal(r.ok, false);
  assert.ok(r.reason);
  process.env.HS_SPOOL_DIR = dir;
});

// ---------- hook-cli：4 类故障（G4 反例，进程级） ----------

function runHook(stdin, spoolDir) {
  return spawnSync(process.execPath, [HOOK_CLI], {
    input: stdin,
    encoding: 'utf8',
    env: { ...process.env, HS_SPOOL_DIR: spoolDir, HS_DEBUG: '' },
  });
}

test('故障1：stdin 非法 JSON → exit 0、stdout 空、parse_error+1、不写 spool', () => {
  const dir = tmpSpool();
  const r = runHook('{not json', dir);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, '');
  process.env.HS_SPOOL_DIR = dir;
  assert.equal(readStats()['parse_error'], 1);
  assert.equal(existsSync(join(dir, 'run-x.jsonl')), false);
});

test('故障2：HS_SPOOL_DIR 不可写 → exit 0、stdout 空、不写 spool', () => {
  const dir = tmpSpool();
  const blocker = join(dir, 'blocker');
  writeFileSync(blocker, 'x');
  const r = runHook(JSON.stringify({ hook_event_name: 'Stop', session_id: 's' }), blocker);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, '');
  // spool 目录本身不可写时 _stats.json 也无法落盘（尽力而为，见 hook-contract 已知坑）
  assert.equal(existsSync(join(dir, 'run-s.jsonl')), false);
  process.env.HS_SPOOL_DIR = dir;
});

test('故障3：缺 hook_event_name → exit 0、malformed_payload+1', () => {
  const dir = tmpSpool();
  const r = runHook(JSON.stringify({ session_id: 's' }), dir);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, '');
  process.env.HS_SPOOL_DIR = dir;
  assert.equal(readStats()['malformed_payload'], 1);
});

test('故障4：未知 hook_event_name → exit 0、dropped_unknown_event+1', () => {
  const dir = tmpSpool();
  const r = runHook(JSON.stringify({ hook_event_name: 'Notification', session_id: 's' }), dir);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, '');
  process.env.HS_SPOOL_DIR = dir;
  assert.equal(readStats()['dropped_unknown_event'], 1);
});

test('G2 可运行：合法 PreToolUse → spool 新增 1 行合法 TraceEvent', () => {
  const dir = tmpSpool();
  const payload = {
    hook_event_name: 'PreToolUse',
    session_id: 's-g2',
    tool_name: 'Read',
    tool_input: { file_path: 'a.ts' },
  };
  const r = runHook(JSON.stringify(payload), dir);
  assert.equal(r.status, 0);
  assert.equal(r.stdout, '');
  const file = join(dir, 'run-s-g2.jsonl');
  assert.ok(existsSync(file));
  const event = JSON.parse(readFileSync(file, 'utf8').trim());
  assert.equal(event.type, 'tool.call');
  assert.equal(event.seq, 0);
});

test('G5 安全：默认关内容捕获，payload 中的假密钥原文不落盘', () => {
  const dir = tmpSpool();
  const payload = {
    hook_event_name: 'UserPromptSubmit',
    session_id: 's-sec',
    prompt: `leak ${FAKE_KEY} here`,
  };
  runHook(JSON.stringify(payload), dir);
  const raw = readFileSync(join(dir, 'run-s-sec.jsonl'), 'utf8');
  assert.ok(!raw.includes(FAKE_KEY.slice(3)));
});

test('G5 安全：开内容捕获（HS_CONTENT_CAPTURE=1）→ [REDACTED:*]，无明文', () => {
  const dir = tmpSpool();
  const payload = {
    hook_event_name: 'UserPromptSubmit',
    session_id: 's-sec2',
    prompt: `leak ${FAKE_KEY} here`,
  };
  const r = spawnSync(process.execPath, [HOOK_CLI], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, HS_SPOOL_DIR: dir, HS_CONTENT_CAPTURE: '1', HS_DEBUG: '' },
  });
  assert.equal(r.status, 0);
  const raw = readFileSync(join(dir, 'run-s-sec2.jsonl'), 'utf8');
  assert.match(raw, /\[REDACTED:api-key\]/);
  assert.ok(!raw.includes(FAKE_KEY.slice(3)));
});

test('清理计数污染', () => {
  // 占位：保持每个用例独立 spool 目录，无需清理
  assert.ok(true);
});
