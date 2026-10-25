// src/codebuddy/install.ts — 把 hs-hook 幂等写入 CodeBuddy settings
// 目标文件：HS_CODEBUDDY_SETTINGS -> ~/.codebuddy/settings.json；写前备份；支持 --dry-run / --uninstall。
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { MAPPED_HOOK_EVENTS } from './hook-mapping.js';

/** hook-cli.js 的绝对路径（install.js 与 hook-cli.js 同目录产出）。 */
export function hookCliPath(): string {
  return join(dirname(fileURLToPath(import.meta.url)), 'hook-cli.js');
}

export function hookCommand(): string {
  return `node "${hookCliPath()}"`;
}

export function settingsPath(): string {
  return process.env.HS_CODEBUDDY_SETTINGS || join(homedir(), '.codebuddy', 'settings.json');
}

export function backupPath(settings: string): string {
  return `${settings}.hs-backup-${new Date().toISOString().replace(/[:.]/g, '-')}`;
}

interface HookEntry {
  matcher: string;
  hooks: Array<{ type: string; command: string }>;
}

interface Settings {
  hooks?: Record<string, HookEntry[]>;
  [k: string]: unknown;
}

/** 生成 12 个事件的 hooks 配置（形状 = Claude-Code 兼容，见 docs/hook-contract.md）。 */
export function buildHooksConfig(command: string): Record<string, HookEntry[]> {
  const hooks: Record<string, HookEntry[]> = {};
  for (const name of MAPPED_HOOK_EVENTS) {
    hooks[name] = [{ matcher: '*', hooks: [{ type: 'command', command }] }];
  }
  return hooks;
}

function isOurEntry(entry: unknown): boolean {
  if (entry === null || typeof entry !== 'object') return false;
  const e = entry as { hooks?: Array<{ command?: unknown }> };
  return Array.isArray(e.hooks) && e.hooks.some((h) => typeof h?.command === 'string' && h.command.includes('hook-cli.js'));
}

function readSettings(path: string): Settings {
  if (!existsSync(path)) return {};
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
    return parsed !== null && typeof parsed === 'object' ? (parsed as Settings) : {};
  } catch {
    throw new Error(`settings 文件不是合法 JSON：${path}（请人工修复后再 install）`);
  }
}

export interface InstallReport {
  settings: string;
  backup?: string;
  added: string[];
  skipped: string[];
  removed: string[];
}

export function installHooks(opts: { dryRun?: boolean } = {}): InstallReport {
  const target = settingsPath();
  const command = hookCommand();
  const wanted = buildHooksConfig(command);
  const current = readSettings(target);
  const existingHooks = current.hooks ?? {};
  const report: InstallReport = { settings: target, added: [], skipped: [], removed: [] };

  const next: Record<string, HookEntry[]> = { ...existingHooks };
  for (const [name, entries] of Object.entries(wanted)) {
    const entry = entries[0];
    if (entry === undefined) continue;
    const list = Array.isArray(next[name]) ? next[name] : [];
    const others = list.filter((e) => !isOurEntry(e)); // 用户自己的 hooks，原样保留
    const ours = list.filter(isOurEntry);
    const identical = ours.some((e) => JSON.stringify(e) === JSON.stringify(entry));
    if (identical) {
      report.skipped.push(name);
    } else {
      report.added.push(name);
    }
    next[name] = [entry, ...others]; // 幂等：我们的条目至多一条且永远最新
  }

  const output: Settings = { ...current, hooks: next };
  if (opts.dryRun) {
    return report;
  }
  mkdirSync(dirname(target), { recursive: true });
  if (existsSync(target)) {
    const backup = backupPath(target);
    copyFileSync(target, backup);
    report.backup = backup;
  }
  writeFileSync(target, `${JSON.stringify(output, null, 2)}\n`, 'utf8');
  return report;
}

export function uninstallHooks(opts: { dryRun?: boolean } = {}): InstallReport {
  const target = settingsPath();
  const current = readSettings(target);
  const existingHooks = current.hooks ?? {};
  const report: InstallReport = { settings: target, added: [], skipped: [], removed: [] };
  const next: Record<string, HookEntry[]> = {};
  for (const [name, list] of Object.entries(existingHooks)) {
    if (!Array.isArray(list)) {
      next[name] = list;
      continue;
    }
    const kept = list.filter((e) => !isOurEntry(e));
    report.removed.push(...list.filter((e) => isOurEntry(e)).map(() => name));
    if (kept.length > 0) next[name] = kept;
  }
  if (opts.dryRun) return report;
  if (existsSync(target)) {
    const backup = backupPath(target);
    copyFileSync(target, backup);
    report.backup = backup;
  }
  const output: Settings = { ...current, hooks: next };
  writeFileSync(target, `${JSON.stringify(output, null, 2)}\n`, 'utf8');
  return report;
}

function main(): void {
  const args = process.argv.slice(2);
  const dryRun = args.includes('--dry-run');
  if (args.includes('--uninstall')) {
    const r = uninstallHooks({ dryRun });
    console.log(`[hs-hook] uninstall ${dryRun ? '(dry-run) ' : ''}settings=${r.settings}`);
    console.log(`  removed entries: ${r.removed.length}`);
    if (r.backup) console.log(`  backup: ${r.backup}`);
    return;
  }
  const r = installHooks({ dryRun });
  console.log(`[hs-hook] install ${dryRun ? '(dry-run) ' : ''}settings=${r.settings}`);
  console.log(`  added(${r.added.length}): ${r.added.join(', ') || '-'}`);
  console.log(`  skipped(${r.skipped.length}): ${r.skipped.join(', ') || '-'}`);
  if (r.backup) console.log(`  backup: ${r.backup}`);
  if (!dryRun) console.log('  重启 CodeBuddy 会话后 hooks 生效。');
}

if (process.argv[1] && process.argv[1].replace(/\\/g, '/').endsWith('install.js')) {
  main();
}
