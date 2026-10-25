// scripts/gen_ts.mjs — JSON Schema -> TypeScript (.d.ts)
// 单向生成：packages/schema/*.schema.json -> packages/schema/generated/ts/*.d.ts
// 用法：node scripts/gen_ts.mjs [--check]
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..');
const schemaDir = join(repoRoot, 'packages', 'schema');
const outDir = join(schemaDir, 'generated', 'ts');
mkdirSync(outDir, { recursive: true });
const check = process.argv.includes('--check');

const kinds = ['trace-event', 'metric-set', 'taskset', 'capability'];
const banner = '// AUTO-GENERATED, DO NOT EDIT';
const options = {
  bannerComment: banner,
  style: { singleQuote: true, semi: true },
};

let failed = false;
for (const kind of kinds) {
  const schemaPath = join(schemaDir, `${kind}.schema.json`);
  const outPath = join(outDir, `${kind}.d.ts`);
  const schema = JSON.parse(readFileSync(schemaPath, 'utf-8'));
  const generated = await compile(schema, toPascalCase(kind), options);

  if (check) {
    const current = readFileSync(outPath, 'utf-8');
    if (current !== generated) {
      console.error(`CHECK FAIL: ${rel(outPath)} 与 schema 不一致（请运行 make gen）`);
      failed = true;
    } else {
      console.log(`CHECK OK: ${rel(outPath)}`);
    }
  } else {
    writeFileSync(outPath, generated, 'utf-8');
    console.log(`GEN: ${rel(outPath)}`);
  }
}
if (failed) process.exit(1);

function toPascalCase(s) {
  return s.replace(/(^|-)([a-z])/g, (_, __, c) => c.toUpperCase());
}
function rel(p) {
  return relative(repoRoot, p).replaceAll('\\', '/');
}
