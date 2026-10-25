// src/codebuddy/tasks-watcher.ts — 通道 C：tasks/<sessionId>/<taskId>.json → TraceEvent[]（T03）
// 任务板快照。文件变化是整体重写而非追加 → 游标策略为 mtime + content hash（非 offset）。
// 首次见到 → task.create；status → completed → task.complete；其他变化 → task.update；
// 归属变化（owner/assignee 字段，实测样本不存在）→ task.claim。
// 毫秒 epoch → ISO-8601，与其余通道单位统一。
// 隐藏文件（.acp-v2-plan-revision 等）一律跳过。
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import type { TraceEvent } from '../types.js';
import { scrubHome } from '../normalize.js';
import { CursorStore } from './jsonl-tail.js';
import { codebuddyPaths, cursorFilePath } from './paths.js';
import { makeEvent, epochMsToIso, defaultRunId, type FileChannelOptions, type CollectResult } from './trace-reader.js';

interface TaskSnapshot {
  subject?: string;
  description?: string;
  activeForm?: string;
  status?: string;
  id?: string;
  createdAt?: number;
  updatedAt?: number;
  [k: string]: unknown;
}

interface TaskCursor {
  hash: string;
  mtimeMs: number;
  status?: string;
}

/** 已知任务状态机（CodeBuddy 任务板）；观测到 completed，其余按状态机枚举。 */
const KNOWN_STATUSES = new Set(['pending', 'in_progress', 'completed']);

function sha256(s: string): string {
  return createHash('sha256').update(s, 'utf8').digest('hex');
}

function listTaskFiles(tasksDir: string): { sessionId: string; file: string }[] {
  let sessions: string[];
  try {
    sessions = readdirSync(tasksDir, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return [];
  }
  const out: { sessionId: string; file: string }[] = [];
  for (const sessionId of sessions) {
    let names: string[];
    try {
      names = readdirSync(join(tasksDir, sessionId));
    } catch {
      continue;
    }
    for (const name of names) {
      if (name.startsWith('.')) continue; // .acp-v2-plan-revision 等隐藏文件：跳过不解析
      if (name.endsWith('.json')) out.push({ sessionId, file: join(tasksDir, sessionId, name) });
    }
  }
  return out.sort((a, b) => (a.file < b.file ? -1 : 1));
}

/** 通道 C：扫描 tasks/<sessionId>/*.json，按 mtime+hash 增量解析为 TraceEvent[]。 */
export function collectTasks(opts: FileChannelOptions = {}, cursors?: CursorStore): CollectResult {
  const warnings: string[] = [];
  const stats = { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 };
  const events: TraceEvent[] = [];
  const paths = codebuddyPaths(opts);
  let tasksExist = false;
  try {
    readdirSync(paths.tasksDir);
    tasksExist = true;
  } catch {
    warnings.push(`tasks 目录不存在或不可读：${paths.tasksDir}`);
    return { events, warnings, stats };
  }
  const store = cursors ?? new CursorStore(opts.cursorPath ?? cursorFilePath(opts));
  const homeDir = opts.homeDir ?? homedir(); // 安全默认：未显式传入时脱敏基准取真实 home（T03-R1）

  for (const { sessionId, file } of listTaskFiles(paths.tasksDir)) {
    let content: string;
    let st;
    try {
      content = readFileSync(file, 'utf8');
      st = statSync(file);
    } catch {
      stats.filesSkipped += 1;
      continue;
    }
    const hash = sha256(content);
    const prev = store.get(file) as TaskCursor | undefined;
    if (prev && prev.hash === hash) {
      stats.filesSkipped += 1;
      continue;
    }
    let task: TaskSnapshot;
    try {
      const parsed: unknown = JSON.parse(content);
      if (parsed === null || typeof parsed !== 'object') {
        stats.skippedLines += 1;
        continue;
      }
      task = parsed as TaskSnapshot;
    } catch {
      stats.skippedLines += 1;
      continue;
    }
    const taskId = typeof task.id === 'string' && task.id.length > 0 ? task.id : file.replace(/\\/g, '/').split('/').pop()?.replace(/\.json$/, '') ?? 'unknown-task';
    const runId = defaultRunId(sessionId, opts.runId);
    const rawRef = `${scrubHome(file, homeDir)}@0`;
    const createdAtIso = epochMsToIso(task.createdAt);
    const updatedAtIso = epochMsToIso(task.updatedAt);
    const ts = updatedAtIso ?? new Date().toISOString();
    const owner = typeof task.owner === 'string' ? task.owner : typeof task.assignee === 'string' ? task.assignee : undefined;
    const status = typeof task.status === 'string' ? task.status : undefined;
    if (status !== undefined && !KNOWN_STATUSES.has(status)) {
      warnings.push(`tasks: 未知任务状态 "${status}"（task ${taskId}），按 update 处理`);
    }

    const base = { sessionId, ts, runId, rawRef, homeDir, taskId };
    if (!prev) {
      // 首次见到该任务
      events.push(
        makeEvent({
          type: 'task.create',
          ...base,
          payload: {
            task_id: taskId,
            ...(typeof task.subject === 'string' ? { subject: task.subject } : {}),
            ...(typeof task.description === 'string' ? { description: task.description } : {}),
            ...(createdAtIso ? { created_at: createdAtIso } : {}),
            ...(updatedAtIso ? { updated_at: updatedAtIso } : {}),
            ...(status ? { status } : {}),
          },
        }),
      );
      if (owner !== undefined) {
        events.push(makeEvent({ type: 'task.claim', ...base, payload: { task_id: taskId, owner } }));
      }
    } else {
      // 文件变化：归属变化 → claim；status → completed → complete；其余 → update
      const prevOwner = (prev as TaskCursor & { owner?: string }).owner;
      if (owner !== undefined && owner !== prevOwner) {
        events.push(makeEvent({ type: 'task.claim', ...base, payload: { task_id: taskId, owner } }));
      }
      if (status !== undefined && status !== prev.status) {
        if (status === 'completed') {
          events.push(makeEvent({ type: 'task.complete', ...base, payload: { task_id: taskId, ...(updatedAtIso ? { updated_at: updatedAtIso } : {}) } }));
        } else {
          events.push(makeEvent({ type: 'task.update', ...base, payload: { task_id: taskId, status, ...(updatedAtIso ? { updated_at: updatedAtIso } : {}) } }));
        }
      } else if (owner === undefined || owner === prevOwner) {
        // 非状态非归属变化（subject/description 编辑等）→ task.update
        events.push(
          makeEvent({
            type: 'task.update',
            ...base,
            payload: {
              task_id: taskId,
              ...(typeof task.subject === 'string' ? { subject: task.subject } : {}),
              ...(updatedAtIso ? { updated_at: updatedAtIso } : {}),
            },
          }),
        );
      }
    }
    store.set(file, { hash, mtimeMs: st.mtimeMs, ...(status ? { status } : {}), ...(owner !== undefined ? { owner } : {}) });
    stats.filesRead += 1;
  }
  store.save();
  return { events, warnings, stats };
}
