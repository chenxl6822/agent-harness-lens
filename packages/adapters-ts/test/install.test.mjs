// test/install.test.mjs — install 幂等（G6）、dry-run、uninstall
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { installHooks, uninstallHooks, settingsPath, hookCommand } from '../dist/codebuddy/install.js';

function tmpSettings() {
  const dir = mkdtempSync(join(tmpdir(), 'hs-settings-'));
  const p = join(dir, 'settings.json');
  process.env.HS_CODEBUDDY_SETTINGS = p;
  return p;
}

test('G6 幂等：install 连跑 2 次，hooks 条目不重复（12 事件各 1 条）', () => {
  const p = tmpSettings();
  installHooks();
  installHooks();
  const settings = JSON.parse(readFileSync(p, 'utf8'));
  const names = Object.keys(settings.hooks);
  assert.equal(names.length, 12);
  for (const name of names) {
    assert.equal(settings.hooks[name].length, 1, `${name} 条目重复`);
    assert.match(settings.hooks[name][0].hooks[0].command, /hook-cli\.js/);
  }
});

test('用户已有 hooks 不被碰：install 保留他人条目', () => {
  const p = tmpSettings();
  writeFileSync(
    p,
    JSON.stringify({ hooks: { PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo user-own' }] }] } }),
  );
  installHooks();
  const settings = JSON.parse(readFileSync(p, 'utf8'));
  const pre = settings.hooks.PreToolUse;
  assert.equal(pre.length, 2); // 1 条用户的 + 1 条我们的
  assert.ok(pre.some((e) => e.hooks[0].command === 'echo user-own'));
});

test('写前备份：settings 已存在时产生 .hs-backup-<ts>', () => {
  const p = tmpSettings();
  writeFileSync(p, '{}');
  const r = installHooks();
  assert.ok(r.backup);
  assert.ok(existsSync(r.backup));
});

test('--dry-run 等价：不改文件', () => {
  const p = tmpSettings();
  const before = installHooks({ dryRun: true });
  assert.equal(existsSync(p), false); // dry-run 绝不落盘
  assert.equal(before.added.length, 12);
});

test('--uninstall：精确移除我们的条目，不碰用户其它 hooks', () => {
  const p = tmpSettings();
  writeFileSync(
    p,
    JSON.stringify({ hooks: { Stop: [{ matcher: '*', hooks: [{ type: 'command', command: 'echo mine' }] }] }, other: 1 }),
  );
  installHooks();
  const r = uninstallHooks();
  const settings = JSON.parse(readFileSync(p, 'utf8'));
  assert.equal(settings.other, 1);
  assert.deepEqual(settings.hooks.Stop, [{ matcher: '*', hooks: [{ type: 'command', command: 'echo mine' }] }]);
  assert.equal(r.removed.length, 12);
});

test('hookCommand 指向 dist/codebuddy/hook-cli.js 绝对路径', () => {
  assert.match(hookCommand(), /^node ".*hook-cli\.js"$/);
  assert.ok(!hookCommand().includes('\n'));
});

test('清理临时 settings', () => {
  rmSync(settingsPath(), { force: true, recursive: true });
  assert.ok(true);
});
