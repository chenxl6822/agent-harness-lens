// src/redact.ts — 脱敏：正则特征库 + 高熵串检测（Shannon >= 4.0 且长度 >= 24）
// 命中即替换为 [REDACTED:<kind>]，并返回 redacted 标记。

export interface RedactResult {
  text: string;
  redacted: boolean;
}

interface RedactRule {
  kind: string;
  pattern: RegExp;
}

// 顺序即优先级：先长块（私钥块），再具体 token，最后泛化参数。
const RULES: RedactRule[] = [
  {
    kind: 'private-key',
    pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
  },
  { kind: 'api-key', pattern: /\bsk-[A-Za-z0-9]{16,}\b/g },
  { kind: 'github-token', pattern: /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b/g },
  { kind: 'aws-access-key', pattern: /\bAKIA[0-9A-Z]{16}\b/g },
  { kind: 'bearer-token', pattern: /Bearer\s+[A-Za-z0-9._\-]{8,}/g },
  {
    kind: 'api-key',
    pattern: /\b(?:api[_-]?key|apikey|access[_-]?token)\b\s*[=:]\s*["']?[A-Za-z0-9._\-]{8,}["']?/gi,
  },
];

/** 高熵候选 token：可被视为机密的连续串 */
const HIGH_ENTROPY_TOKEN = /[A-Za-z0-9+/_=\-.]{24,}/g;

/** Shannon 熵（以 2 为底，字符集归一） */
export function shannonEntropy(s: string): number {
  if (s.length === 0) return 0;
  const freq = new Map<string, number>();
  for (const ch of s) freq.set(ch, (freq.get(ch) ?? 0) + 1);
  let h = 0;
  for (const count of freq.values()) {
    const p = count / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}

function redactHighEntropy(text: string): string {
  return text.replace(HIGH_ENTROPY_TOKEN, (token) => {
    // 排除常见低风险形态：纯路径（含 / 或 \ 且分段短）、小数、版本号
    if (/^(?:\.{0,2}\/|~\/|[A-Za-z]:\\)/.test(token)) return token;
    if (/^\d+(\.\d+)+$/.test(token)) return token;
    if (shannonEntropy(token) >= 4.0) return '[REDACTED:high-entropy]';
    return token;
  });
}

/** 对任意字符串做脱敏；无命中时原样返回（redacted=false）。 */
export function redact(text: string): RedactResult {
  let redacted = false;
  let out = text;
  for (const rule of RULES) {
    if (rule.pattern.test(out)) {
      out = out.replace(rule.pattern, `[REDACTED:${rule.kind}]`);
      redacted = true;
    }
    rule.pattern.lastIndex = 0; // g 标志复用安全
  }
  const after = redactHighEntropy(out);
  if (after !== out) redacted = true;
  return { text: after, redacted };
}

/** 把 payload 中所有 string 值递归脱敏；返回是否发生过替换。 */
export function redactDeep(value: unknown): { value: unknown; redacted: boolean } {
  if (typeof value === 'string') {
    const r = redact(value);
    return { value: r.text, redacted: r.redacted };
  }
  if (Array.isArray(value)) {
    let hit = false;
    const arr = value.map((item) => {
      const r = redactDeep(item);
      if (r.redacted) hit = true;
      return r.value;
    });
    return { value: arr, redacted: hit };
  }
  if (value !== null && typeof value === 'object') {
    let hit = false;
    const obj: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      const r = redactDeep(v);
      if (r.redacted) hit = true;
      obj[k] = r.value;
    }
    return { value: obj, redacted: hit };
  }
  return { value, redacted: false };
}
