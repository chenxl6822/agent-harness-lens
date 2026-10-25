#!/usr/bin/env node
/**
 * dispatch.mjs —— 派单台账（Dispatch Ledger）
 *
 * 解决的问题：WorkBuddy 说"我把任务派给 CodeBuddy 了"，这句话本身不可验证。
 * 本脚本把一次派单变成一条**可审计的记录**，落盘到 runs/dispatch-ledger.jsonl。
 *
 * 一条记录包含四方独立证据：
 *   1. 调用侧：完整命令行、提示词 sha256、退出码、耗时
 *   2. CodeBuddy 侧：它自己写的会话文件 projects/<slug>/<sessionId>.jsonl
 *      —— 并校验提示词原文是否真的出现在该文件里（这是"确实派给了 CodeBuddy"的硬证据）
 *   3. 运行侧：执行期间新出现的进程 + 主窗口标题（可定位任何弹出的窗口）
 *   4. 仓库侧：git HEAD 前后、变更文件、diff 的 sha256
 *
 * 用法：
 *   node scripts/dispatch.mjs --task T04a --prompt-file ../.tmp/t04a.txt
 *   node scripts/dispatch.mjs --task T04a --prompt "..." --workdir .
 *   node scripts/dispatch.mjs --task T04a --prompt-file x.txt --dry-run
 *
 * 可选：--timeout <分钟>  --no-skip-permissions  --poll <秒>
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';

const REPO = path.resolve(import.meta.dirname, '..');
const RUNS = path.join(REPO, 'runs');
const LEDGER = path.join(RUNS, 'dispatch-ledger.jsonl');

// ---------------------------------------------------------------- recheck
// --recheck <runTag>：对一次已完成的派单重新取证并改写台账。
// 用途：门禁自身有 bug 时事后修正——证据应该能反复重算，而不是一次性快照。
{
  const i = process.argv.indexOf('--recheck');
  if (i !== -1 && process.argv[i + 1]) {
    recheck(process.argv[i + 1]);
    process.exit(0);
  }
}

function recheck(tag) {
  const dir = path.join(RUNS, tag);
  const promptPath = path.join(dir, 'prompt.txt');
  if (!fs.existsSync(promptPath)) {
    console.error(`找不到 ${promptPath}`);
    process.exit(2);
  }
  const text = fs.readFileSync(promptPath, 'utf8');
  const m = tag.match(/(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z$/);
  if (!m) {
    console.error('无法从 runTag 解析起始时间');
    process.exit(2);
  }
  const sinceMs = Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}.${m[7]}Z`);
  const arts = codebuddyArtifacts(sinceMs);

  let ev = null;
  const sorted = arts.sessions.sort((a, b) => b.size - a.size);
  for (const s of sorted) {
    const v = verifyPromptInSession(s.path, text);
    if (v.matched) {
      ev = { sessionId: path.basename(s.path, '.jsonl'), file: s.path, bytes: s.size, ...v };
      break;
    }
  }
  if (!ev && sorted.length) {
    const s = sorted[0];
    ev = {
      sessionId: path.basename(s.path, '.jsonl'),
      file: s.path,
      bytes: s.size,
      matched: false,
      reason: 'candidate-only: 提示词指纹未命中，仅为时间窗内最大的会话',
      fingerprint: fingerprint(text),
    };
  }

  if (!fs.existsSync(LEDGER)) {
    console.error('台账文件不存在');
    process.exit(2);
  }
  const lines = fs.readFileSync(LEDGER, 'utf8').split(/\r?\n/).filter(Boolean);
  let hit = 0;
  const out = lines.map((l) => {
    let r;
    try {
      r = JSON.parse(l);
    } catch {
      return l;
    }
    if (r.runTag !== tag) return l;
    hit++;
    r.evidence = r.evidence || {};
    r.evidence.codebuddySession = ev;
    r.evidence.codebuddyTraces = arts.traces.sort((a, b) => b.size - a.size).slice(0, 3);
    r.verdict = r.verdict || {};
    r.verdict.dispatchedToCodeBuddy = Boolean(ev?.matched);
    r.recheckedAt = new Date().toISOString();
    r.recheckNote = '由 --recheck 重算：修复了 jsonl 反斜杠转义导致原文比对误判的 bug';
    return JSON.stringify(r);
  });
  fs.writeFileSync(LEDGER, out.join('\n') + '\n', 'utf8');
  console.log(`[recheck] ${tag}`);
  console.log(`  命中记录      ${hit} 条`);
  console.log(`  候选会话      ${sorted.length} 个（时间窗内）`);
  console.log(`  采用会话      ${ev ? ev.sessionId : '无'}`);
  console.log(`  提示词原文命中 ${ev?.matched ? '是' : '否'}`);
}


// ---------------------------------------------------------------- args
function arg(name, dflt = null) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return dflt;
  const v = process.argv[i + 1];
  return v && !v.startsWith('--') ? v : true;
}
const has = (name) => process.argv.includes(`--${name}`);

const task = arg('task');
if (!task || task === true) {
  console.error('缺少 --task <任务编号>');
  process.exit(2);
}
const workdir = path.resolve(arg('workdir', REPO));
const timeoutMin = Number(arg('timeout', '40'));
const pollSec = Number(arg('poll', '6'));
const skipPerms = !has('no-skip-permissions');
const dryRun = has('dry-run');

let prompt = arg('prompt', null);
const promptFile = arg('prompt-file', null);
if (promptFile && promptFile !== true) prompt = fs.readFileSync(path.resolve(promptFile), 'utf8');
if (!prompt || prompt === true) {
  console.error('缺少 --prompt 或 --prompt-file');
  process.exit(2);
}
prompt = String(prompt);

// ---------------------------------------------------------------- helpers
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');
const nowIso = () => new Date().toISOString();
const ms = () => Date.now();

function git(args, cwd = workdir) {
  const r = spawnSync('git', args, { cwd, encoding: 'utf8', windowsHide: true });
  return (r.stdout ?? '').trim();
}

/** 提示词去空白化后取前 N 字符，用于在会话文件里做原文比对 */
function fingerprint(text, n = 48) {
  return text.replace(/\s+/g, ' ').trim().slice(0, n);
}

/** 用 PowerShell 抓当前所有带窗口的进程（含中文标题，UTF-8 输出） */
function windowSnapshot() {
  const ps =
    '[Console]::OutputEncoding=[Text.Encoding]::UTF8;' +
    'Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | ' +
    'ForEach-Object { "{0}`t{1}`t{2}" -f $_.Id,$_.ProcessName,$_.MainWindowTitle }';
  const r = spawnSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', ps], {
    encoding: 'utf8',
    windowsHide: true,
    timeout: 15000,
  });
  const out = new Map();
  for (const line of (r.stdout ?? '').split(/\r?\n/)) {
    const [pid, name, ...rest] = line.split('\t');
    if (pid && /^\d+$/.test(pid.trim())) out.set(Number(pid), { pid: Number(pid), name, title: rest.join('\t') });
  }
  return out;
}

/** 找本次派单窗口内被修改的 CodeBuddy 会话文件与 trace 文件 */
function codebuddyArtifacts(sinceMs) {
  const home = path.join(os.homedir(), '.codebuddy');
  const found = { sessions: [], traces: [] };
  const scan = (dir, depth, sink, match) => {
    let ents;
    try {
      ents = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of ents) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (depth > 0) scan(p, depth - 1, sink, match);
      } else if (match(e.name)) {
        try {
          const st = fs.statSync(p);
          if (st.mtimeMs >= sinceMs - 5000) sink.push({ path: p, mtime: st.mtime.toISOString(), size: st.size });
        } catch {}
      }
    }
  };
  scan(path.join(home, 'projects'), 2, found.sessions, (n) => n.endsWith('.jsonl'));
  scan(path.join(home, 'traces'), 2, found.traces, (n) => n.startsWith('trace_') && n.endsWith('.jsonl'));
  return found;
}

/** 递归收集对象里所有字符串值，用于在「解码后」的文本上做比对 */
function flattenStrings(v, out = []) {
  if (typeof v === 'string') out.push(v);
  else if (Array.isArray(v)) for (const x of v) flattenStrings(x, out);
  else if (v && typeof v === 'object') for (const x of Object.values(v)) flattenStrings(x, out);
  return out;
}

/**
 * 在会话文件里找提示词原文。
 * 这是"任务确实交给了 CodeBuddy"最硬的证据：它由 CodeBuddy 自己写入，WorkBuddy 无法伪造。
 *
 * 【曾经的 bug】第一版直接拿 jsonl 的原始字节做子串匹配。但 jsonl 里反斜杠会被转义成 `\\`，
 * 所以只要提示词里出现 Windows 路径（C:\Users\...），比对必然失败——冒烟用例恰好没有反斜杠，
 * 于是"通过"了，直到 T04a 才暴露。现在改为先 JSON.parse 再在解码后的文本上比对。
 */
function verifyPromptInSession(sessionPath, promptText) {
  const fp = fingerprint(promptText);
  let raw;
  try {
    raw = fs.readFileSync(sessionPath, 'utf8');
  } catch {
    return { matched: false, reason: 'unreadable', fingerprint: fp };
  }
  let matched = false;
  let firstUser = null;
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let d;
    try {
      d = JSON.parse(line);
    } catch {
      continue;
    }
    if (!matched) {
      const flat = flattenStrings(d).join(' ').replace(/\s+/g, ' ');
      if (flat.includes(fp)) matched = true;
    }
    if (!firstUser && d.type === 'user') {
      const c = d.message?.content ?? d.content;
      firstUser = typeof c === 'string' ? c : JSON.stringify(c);
    }
    if (matched && firstUser) break;
  }
  return { matched, fingerprint: fp, firstUser: firstUser ? firstUser.slice(0, 300) : null };
}

// ---------------------------------------------------------------- pre-flight
const startMs = ms();
const startedAt = nowIso();
const headBefore = git(['rev-parse', 'HEAD']);
const statusBefore = git(['status', '--porcelain']);
const promptHash = sha(prompt);

const cbcRun = path.join(os.homedir(), '.workbuddy', 'binaries', 'node', 'workspace', 'cbc-run.js');
const args = [cbcRun, '-p', prompt];
if (skipPerms) args.push('--dangerously-skip-permissions');

const runTag = `${task}-${new Date(startMs).toISOString().replace(/[:.]/g, '-')}`;
const runDir = path.join(RUNS, runTag);
fs.mkdirSync(runDir, { recursive: true });
fs.writeFileSync(path.join(runDir, 'prompt.txt'), prompt, 'utf8');

console.log(`[dispatch] 任务     ${task}`);
console.log(`[dispatch] 工作目录 ${workdir}`);
console.log(`[dispatch] 提示词   ${prompt.length} 字符  sha256=${promptHash.slice(0, 16)}…`);
console.log(`[dispatch] 执行器   ${cbcRun}`);
console.log(`[dispatch] 日志     ${runDir}`);

if (dryRun) {
  console.log(`[dispatch] --dry-run，仅展示命令：`);
  console.log(`  node ${args[0]} -p <prompt:${prompt.length}字符> ${skipPerms ? '--dangerously-skip-permissions' : ''}`);
  process.exit(0);
}

// ---------------------------------------------------------------- run
const win0 = windowSnapshot();
const out = fs.createWriteStream(path.join(runDir, 'stdout.log'));
const err = fs.createWriteStream(path.join(runDir, 'stderr.log'));

const child = spawn(process.execPath, args, {
  cwd: workdir,
  windowsHide: true,
  env: { ...process.env, NODE_OPTIONS: '' },
});
child.stdout.pipe(out);
child.stderr.pipe(err);

// 执行期间的进程/窗口采样：任何弹出的窗口都会在这里留下名字和标题
const seen = new Set(win0.keys());
const appeared = new Map();
let stopPoll = false;
const poller = setInterval(() => {
  if (stopPoll) return;
  const snap = windowSnapshot();
  for (const [pid, info] of snap) {
    if (!seen.has(pid)) {
      seen.add(pid);
      appeared.set(pid, { ...info, firstSeen: nowIso() });
      console.log(`[dispatch] 新窗口出现  pid=${pid}  ${info.name}  "${info.title}"`);
    }
  }
}, pollSec * 1000);

const killTimer = setTimeout(() => {
  console.error(`[dispatch] 超时 ${timeoutMin} 分钟，终止子进程`);
  child.kill();
}, timeoutMin * 60 * 1000);

const exitCode = await new Promise((resolve) => {
  child.on('error', (e) => {
    console.error(`[dispatch] 启动失败: ${e.message}`);
    resolve(-1);
  });
  child.on('close', (code) => resolve(code ?? -1));
});
clearTimeout(killTimer);
stopPoll = true;
clearInterval(poller);
out.end();
err.end();

const endMs = ms();
const endedAt = nowIso();

// ---------------------------------------------------------------- post-flight
const headAfter = git(['rev-parse', 'HEAD']);
const changed = git(['status', '--porcelain']).split(/\r?\n/).filter(Boolean);
const diffStat = git(['diff', '--stat']);
const patch = git(['diff']);
const patchHash = sha(patch);

const artifacts = codebuddyArtifacts(startMs);

// 逐个会话文件校验提示词原文；取命中的那一个作为"本次派单的 CodeBuddy 会话"
let sessionEvidence = null;
const sorted = artifacts.sessions.sort((a, b) => b.size - a.size);
for (const s of sorted) {
  const v = verifyPromptInSession(s.path, prompt);
  if (v.matched) {
    sessionEvidence = {
      sessionId: path.basename(s.path, '.jsonl'),
      file: s.path,
      bytes: s.size,
      ...v,
    };
    break;
  }
}
// 未命中时不要只说"没有"——把最像的那个作为候选列出来，供人工判断
if (!sessionEvidence && sorted.length) {
  const s = sorted[0];
  sessionEvidence = {
    sessionId: path.basename(s.path, '.jsonl'),
    file: s.path,
    bytes: s.size,
    matched: false,
    reason: 'candidate-only: 提示词指纹未命中，仅为时间窗内最大的会话',
    fingerprint: fingerprint(prompt),
  };
}
const traceEvidence = artifacts.traces.sort((a, b) => b.size - a.size).slice(0, 3);

const record = {
  schema: 'dispatch-ledger/1',
  task,
  runTag,
  startedAt,
  endedAt,
  durationSec: Math.round((endMs - startMs) / 1000),
  exitCode,
  workdir,
  executor: cbcRun,
  skipPermissions: skipPerms,
  prompt: { chars: prompt.length, sha256: promptHash, file: path.join(runDir, 'prompt.txt') },
  evidence: {
    codebuddySession: sessionEvidence,
    codebuddyTraces: traceEvidence,
    windowsAppeared: [...appeared.values()],
    git: {
      headBefore,
      headAfter,
      headChanged: headBefore !== headAfter,
      changedFiles: changed,
      diffStat,
      patchSha256: patchHash,
    },
  },
  verdict: {
    dispatchedToCodeBuddy: Boolean(sessionEvidence?.matched),
    producedChanges: changed.length > 0 || headBefore !== headAfter,
    succeeded: exitCode === 0,
  },
};

fs.mkdirSync(RUNS, { recursive: true });
fs.appendFileSync(LEDGER, JSON.stringify(record) + '\n', 'utf8');

// 人类可读版
const md = [
  `# 派单台账 · ${task}`,
  ``,
  `- 运行标签：\`${runTag}\``,
  `- 起止：${startedAt} → ${endedAt}（${record.durationSec}s）`,
  `- 退出码：**${exitCode}**`,
  `- 提示词：${prompt.length} 字符，sha256 \`${promptHash.slice(0, 16)}…\``,
  ``,
  `## 证据 1 · CodeBuddy 侧`,
  sessionEvidence
    ? [
        `- 会话 ID：\`${sessionEvidence.sessionId}\``,
        `- 会话文件：\`${sessionEvidence.file}\`（${sessionEvidence.bytes} 字节）`,
        `- 提示词原文命中：**${sessionEvidence.matched ? '是' : '否'}**（比对指纹 \`${sessionEvidence.fingerprint}\`）`,
        sessionEvidence.firstUser ? `- 首条用户消息：「${sessionEvidence.firstUser.replace(/\n/g, ' ')}」` : '',
      ].join('\n')
    : `- **未找到匹配的会话文件** → 无法证明任务交给了 CodeBuddy`,
  ``,
  `## 证据 2 · 执行期间出现的窗口`,
  appeared.size
    ? [...appeared.values()].map((w) => `- pid=${w.pid} \`${w.name}\` 「${w.title}」 @ ${w.firstSeen}`).join('\n')
    : `- 无新窗口（全程无可见界面）`,
  ``,
  `## 证据 3 · 仓库变更`,
  `- HEAD：\`${headBefore.slice(0, 7)}\` → \`${headAfter.slice(0, 7)}\`${headBefore !== headAfter ? '（有新提交）' : '（无新提交）'}`,
  `- 变更文件：${changed.length} 个`,
  changed.length ? '\n```\n' + changed.slice(0, 40).join('\n') + '\n```' : '',
  ``,
  `## 判定`,
  `- 确实派给 CodeBuddy：**${record.verdict.dispatchedToCodeBuddy ? '是' : '否'}**`,
  `- 产生了变更：**${record.verdict.producedChanges ? '是' : '否'}**`,
  `- 执行成功：**${record.verdict.succeeded ? '是' : '否'}**`,
].join('\n');
fs.writeFileSync(path.join(runDir, 'record.md'), md, 'utf8');

console.log('');
console.log('─'.repeat(64));
console.log(`退出码 ${exitCode}   耗时 ${record.durationSec}s`);
console.log(
  `CodeBuddy 会话：${sessionEvidence ? sessionEvidence.sessionId : '未匹配'}   提示词原文命中：${sessionEvidence?.matched ? '是' : '否'}`,
);
console.log(`变更文件 ${changed.length} 个   test=${patchHash.slice(0, 12)}`);
console.log(`台账已追加：${LEDGER}`);
console.log(`本次记录：${path.join(runDir, 'record.md')}`);
console.log('─'.repeat(64));

process.exit(exitCode === 0 ? 0 : 1);
