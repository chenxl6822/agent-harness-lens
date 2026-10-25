// src/codebuddy/index.ts — T03 文件通道统一导出 + collectFromFiles() 编排
// 三通道：A=traces（结构化轨迹）B=projects（会话全文）C=tasks（任务板快照）。
// 注意：实时性由 T02 Hook 通道负责，本模块只做按需增量读取（T03 §8）。
import { existsSync } from 'node:fs';
import type { TraceEvent } from '../types.js';
import { codebuddyPaths, cursorFilePath, hsHome, type CodebuddyPaths } from './paths.js';
import { CursorStore, tailJsonl, type FileCursor, type TailOutcome } from './jsonl-tail.js';
import {
  collectTraces,
  classifySpan,
  makeEvent,
  contentFields,
  epochMsToIso,
  defaultRunId,
  type FileChannelOptions,
  type CollectResult,
} from './trace-reader.js';
import { collectTranscripts, parseTraceparent, parseBaggageSession } from './transcript.js';
import { collectTasks } from './tasks-watcher.js';

export { codebuddyPaths, cursorFilePath, hsHome };
export type { CodebuddyPaths };
export { CursorStore, tailJsonl };
export type { FileCursor, TailOutcome };
export { collectTraces, collectTranscripts, collectTasks, classifySpan, makeEvent, contentFields, epochMsToIso, defaultRunId };
export { parseTraceparent, parseBaggageSession };
export type { FileChannelOptions, CollectResult };

export interface CollectFromFilesResult extends CollectResult {
  /** 各通道分开的结果（调试用）；events 为三通道合并结果 */
  channels: {
    traces: CollectResult;
    transcripts: CollectResult;
    tasks: CollectResult;
  };
}

/**
 * 统一编排：给定 CODEBUDDY_HOME（或回退 ~/.codebuddy），离线重建三通道事件流。
 * - CODEBUDDY_HOME 指向不存在路径 → 空数组 + 明确 warning，不崩溃（T03 §5）。
 * - 幂等：文件内容不变时重复调用返回空 events（游标持久化在 $HS_HOME/state/file-cursors.json）。
 * - 游标保存失败（HS_HOME 不可写）降级为 warning，不影响本次采集结果。
 */
export function collectFromFiles(opts: FileChannelOptions = {}): CollectFromFilesResult {
  const warnings: string[] = [];
  const paths = codebuddyPaths(opts);
  if (!existsSync(paths.home)) {
    warnings.push(`CODEBUDDY_HOME 不存在（回退路径也不存在）：${paths.home} —— 返回空结果`);
    return {
      events: [],
      warnings,
      stats: { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 },
      channels: {
        traces: { events: [], warnings: [], stats: { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 } },
        transcripts: { events: [], warnings: [], stats: { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 } },
        tasks: { events: [], warnings: [], stats: { filesRead: 0, filesSkipped: 0, skippedLines: 0, truncatedFiles: 0 } },
      },
    };
  }
  const store = new CursorStore(opts.cursorPath ?? cursorFilePath(opts));
  const traces = collectTraces(opts, store);
  const transcripts = collectTranscripts(opts, store);
  const tasks = collectTasks(opts, store);
  const saved = store.save();
  if (!saved) warnings.push(`游标文件保存失败（HS_HOME 不可写？）：${opts.cursorPath ?? cursorFilePath(opts)} —— 幂等性受损`);
  warnings.push(...traces.warnings, ...transcripts.warnings, ...tasks.warnings);
  const events: TraceEvent[] = [...traces.events, ...transcripts.events, ...tasks.events];
  return {
    events,
    warnings,
    stats: {
      filesRead: traces.stats.filesRead + transcripts.stats.filesRead + tasks.stats.filesRead,
      filesSkipped: traces.stats.filesSkipped + transcripts.stats.filesSkipped + tasks.stats.filesSkipped,
      skippedLines: traces.stats.skippedLines + transcripts.stats.skippedLines + tasks.stats.skippedLines,
      truncatedFiles: traces.stats.truncatedFiles + transcripts.stats.truncatedFiles + tasks.stats.truncatedFiles,
    },
    channels: { traces, transcripts, tasks },
  };
}
