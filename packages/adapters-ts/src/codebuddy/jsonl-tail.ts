// src/codebuddy/jsonl-tail.ts — NDJSON 增量尾部读取器（T03）
// 职责：按游标增量读取追加型 jsonl 文件，满足 T03 §3.2 五条硬性要求：
//   1. 游标持久化 (path, ino, dev, size, offset, lastLineHash)
//   2. 半行保护：文件尾无换行符不消费该行，offset 停在最后一个 \n 之后
//   3. 截断检测：size < offset（或 inode 变化）→ offset 归零重读，由调用方发 error 事件
//   4. 坏行容错：单行 JSON.parse 失败不抛出，由调用方计数 skippedLines
//   5. 幂等：同一 offset 区间重复调用返回空数组
// 大文件安全：分块 readSync 逐块取完整行，绝不 readFileSync 整体载入。
import { createHash } from 'node:crypto';
import { closeSync, existsSync, fstatSync, mkdirSync, openSync, readSync, readFileSync, writeFileSync, renameSync, unlinkSync } from 'node:fs';
import { dirname } from 'node:path';

// ---------- 游标存储 ----------

export interface FileCursor {
  path: string;
  ino: number;
  dev: number;
  size: number;
  offset: number;
  /** 最后一条已消费行的 sha256（hex），用于辅助判断文件是否被整体重写 */
  lastLineHash: string;
}

/** 每文件一条游标的持久化仓库。保存失败降级为内存态（不影响采集，只影响幂等）。 */
export class CursorStore {
  private data: Record<string, unknown> = {};
  private dirty = false;

  constructor(readonly filePath: string) {
    if (existsSync(filePath)) {
      try {
        const parsed: unknown = JSON.parse(readFileSync(filePath, 'utf8'));
        if (parsed !== null && typeof parsed === 'object') {
          this.data = parsed as Record<string, unknown>;
        }
      } catch {
        this.data = {}; // 游标文件损坏 → 全量重读（安全侧），不抛
      }
    }
  }

  get(key: string): unknown {
    return this.data[key];
  }

  set(key: string, value: unknown): void {
    this.data[key] = value;
    this.dirty = true;
  }

  /** 原子落盘（tmp + rename）。失败静默：幂等性受损但不影响正确性。 */
  save(): boolean {
    if (!this.dirty) return true;
    try {
      mkdirSync(dirname(this.filePath), { recursive: true });
      const tmp = `${this.filePath}.tmp`;
      writeFileSync(tmp, `${JSON.stringify(this.data, null, 1)}\n`, 'utf8');
      try {
        unlinkSync(this.filePath);
      } catch {
        /* 首次写入时目标不存在 */
      }
      renameSync(tmp, this.filePath);
      this.dirty = false;
      return true;
    } catch {
      return false;
    }
  }
}

// ---------- 增量读取 ----------

export interface TailCursor {
  offset: number;
  ino?: number;
  dev?: number;
}

export interface TailOutcome {
  /** 完整行（已去掉行尾 \n / \r\n；不含未完成的半行） */
  lines: string[];
  /** lines[i] 在文件中的起始字节偏移（与 lines 一一对应） */
  lineStarts: number[];
  /** 新 offset：停在最后一个已消费 \n 之后 */
  newOffset: number;
  /** 文件被截断/重写（size < offset 或 inode 变化）→ 已归零重读 */
  truncated: boolean;
  resetReason?: 'shrunk' | 'inode-changed';
  /** 本次消费的最后一行的 sha256（无新行时 undefined） */
  lastLineHash?: string;
}

const CHUNK = 64 * 1024;

/**
 * 增量读取一个追加型 NDJSON 文件的完整行。
 * 只消费以 \n 结尾的字节区间；行内容按 UTF-8 解码（offset 恒在 \n 之后，解码边界安全）。
 */
export function tailJsonl(filePath: string, prev?: TailCursor): TailOutcome {
  let fd: number;
  try {
    fd = openSync(filePath, 'r');
  } catch {
    // 文件消失（目录被清理）：按截断处理，游标归零，返回空
    return { lines: [], lineStarts: [], newOffset: 0, truncated: prev !== undefined && prev.offset > 0, resetReason: 'shrunk' };
  }
  try {
    const st = fstatSync(fd);
    const prevOffset = prev?.offset ?? 0;
    let truncated = false;
    let resetReason: TailOutcome['resetReason'];
    let start = prevOffset;

    if (prev && prevOffset > 0) {
      if (st.size < prevOffset) {
        truncated = true;
        resetReason = 'shrunk';
        start = 0;
      } else if (prev.ino !== undefined && prev.ino !== 0 && st.ino !== 0 && st.ino !== prev.ino) {
        truncated = true;
        resetReason = 'inode-changed';
        start = 0;
      }
    }
    if (start > st.size) start = st.size;

    const lines: string[] = [];
    const lineStarts: number[] = [];
    let pending: Buffer[] = []; // 当前未完行（自上一个 \n 起）
    let pendingLen = 0;
    let pendingStart = start; // pending 行的起始绝对偏移
    let consumedTo = start; // 已读取（未必消费）到的绝对位置
    let lastCompleteEnd = start; // 最后一个 \n 之后的位置

    const buf = Buffer.allocUnsafe(CHUNK);
    const pushPendingLine = (): void => {
      if (pendingLen === 0) return;
      const line = Buffer.concat(pending, pendingLen).toString('utf8');
      lines.push(line.endsWith('\r') ? line.slice(0, -1) : line);
      lineStarts.push(pendingStart);
      pending = [];
      pendingLen = 0;
    };

    while (consumedTo < st.size) {
      const toRead = Math.min(CHUNK, st.size - consumedTo);
      const n = readSync(fd, buf, 0, toRead, consumedTo);
      if (n <= 0) break;
      let chunkStart = 0;
      for (let i = 0; i < n; i++) {
        if (buf[i] === 0x0a) {
          // \n：取 pending + buf[chunkStart..i] 组成完整行
          const seg = buf.subarray(chunkStart, i);
          if (pendingLen === 0) {
            const line = seg.toString('utf8');
            lines.push(line.endsWith('\r') ? line.slice(0, -1) : line);
            lineStarts.push(consumedTo + chunkStart);
          } else {
            pending.push(Buffer.from(seg)); // copy：buf 会被复用
            pendingLen += seg.length;
            pushPendingLine();
          }
          chunkStart = i + 1;
          lastCompleteEnd = consumedTo + i + 1;
        }
      }
      if (chunkStart < n) {
        if (pendingLen === 0) pendingStart = consumedTo + chunkStart;
        const rest = buf.subarray(chunkStart, n);
        pending.push(Buffer.from(rest));
        pendingLen += rest.length;
      }
      consumedTo += n;
    }

    const hash = createHash('sha256');
    for (const line of lines) hash.update(line);
    return {
      lines,
      lineStarts,
      newOffset: lines.length > 0 ? lastCompleteEnd : start,
      truncated,
      ...(resetReason !== undefined ? { resetReason } : {}),
      ...(lines.length > 0 ? { lastLineHash: hash.digest('hex') } : {}),
    };
  } finally {
    closeSync(fd);
  }
}
