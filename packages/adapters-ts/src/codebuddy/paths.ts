// src/codebuddy/paths.ts — 文件通道根路径解析（T03）
// 三条通道位于 CODEBUDDY_HOME 下；生产环境用环境变量，缺省回退 ~/.codebuddy。
// 游标状态存 $HS_HOME/state/file-cursors.json（缺省回退 ~/.harness_scope）。
import { homedir } from 'node:os';
import { join } from 'node:path';

export interface CodebuddyPaths {
  /** CODEBUDDY_HOME 根目录 */
  home: string;
  /** 通道 A：traces/<workerPid>/trace_<traceId>.jsonl */
  tracesDir: string;
  /** 通道 B：projects/<cwd-slug>/<sessionId>.jsonl */
  projectsDir: string;
  /** 通道 C：tasks/<sessionId>/<taskId>.json */
  tasksDir: string;
}

export interface PathResolveOptions {
  /** 测试注入：覆盖 CODEBUDDY_HOME */
  home?: string;
  /** 环境变量源，缺省 process.env */
  env?: NodeJS.ProcessEnv;
}

/** 解析三通道根路径。不检查存在性（存在性由调用方负责并降级为 warning）。 */
export function codebuddyPaths(opts: PathResolveOptions = {}): CodebuddyPaths {
  const env = opts.env ?? process.env;
  const home = opts.home ?? env.CODEBUDDY_HOME ?? join(homedir(), '.codebuddy');
  return {
    home,
    tracesDir: join(home, 'traces'),
    projectsDir: join(home, 'projects'),
    tasksDir: join(home, 'tasks'),
  };
}

/** HS_HOME：本框架自己的状态根（游标文件等）。缺省 ~/.harness_scope。 */
export function hsHome(opts: PathResolveOptions = {}): string {
  const env = opts.env ?? process.env;
  return opts.home ?? env.HS_HOME ?? join(homedir(), '.harness_scope');
}

/** 游标文件路径：$HS_HOME/state/file-cursors.json */
export function cursorFilePath(opts: PathResolveOptions = {}): string {
  return join(hsHome(opts), 'state', 'file-cursors.json');
}
