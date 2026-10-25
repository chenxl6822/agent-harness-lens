#!/usr/bin/env node
// 受限网络下的推送通道：当 git smart-HTTP 被代理阻断时，改用 GitHub Git Data API。
//
// 背景：本机代理对 github.com:443 的 CONNECT 隧道返回 502，但 api.github.com 正常。
//       本脚本通过 `gh api` 上传 blob/tree/commit，并把本地引用对齐到远端返回的
//       同一个 commit（GitHub 侧把时区规范化为 +0000、去掉 message 尾随换行，
//       这里按同样规则重建本地对象，保证两端 SHA 完全一致）。
//
// 用法：
//   node scripts/push-via-api.mjs            # 把 origin/main 之后的所有本地提交按序推上去
//   node scripts/push-via-api.mjs --dry-run  # 只打印将要执行的动作
//   node scripts/push-via-api.mjs --depth 1  # 只推最近 1 个提交（调试用）
//
// 前置：gh 已登录且对目标仓库有写权限。
//
// 为什么需要「按序推多个」：GitHub 存储时会把时区规范化为 +0000 并去掉 message 尾随换行，
// 所以远端 commit 的 SHA 与本地（+0800）不同。单个提交时脚本在本地下重建同一 SHA 即可对齐；
// 但若本地积压了 N 个提交，链式 parent 会逐级错位，必须从最早的未推提交开始，
// 用远端新 SHA 作为下一提交的 parent，最后一次性把本地分支重指到重写后的链上。
// （trees 完全相同，所以重指 ref 不会改动工作区）

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const DRY = process.argv.includes('--dry-run')
const depthArg = process.argv.indexOf('--depth')
const DEPTH = depthArg > -1 ? Number(process.argv[depthArg + 1]) : Infinity
const TMP = path.join(os.tmpdir(), 'hs-push')
fs.mkdirSync(TMP, { recursive: true })

const git = (...a) =>
  execFileSync('git', a, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 }).trim()

function ghBin() {
  const cands = [
    process.env.HS_GH_BIN,
    'C:\\Program Files\\GitHub CLI\\gh.exe',
    'gh',
  ].filter(Boolean)
  for (const c of cands) {
    try {
      execFileSync(c, ['--version'], { encoding: 'utf8' })
      return c
    } catch { /* 试下一个 */ }
  }
  throw new Error('未找到 gh CLI，请安装 GitHub CLI 或设置 HS_GH_BIN')
}

const GH = ghBin()

function ghJson(args, body) {
  const a = ['api', ...args]
  let f
  if (body !== undefined) {
    f = path.join(TMP, `b-${Math.random().toString(36).slice(2)}.json`)
    fs.writeFileSync(f, JSON.stringify(body))
    a.push('--input', f)
  }
  try {
    return JSON.parse(execFileSync(GH, a, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 }))
  } finally { if (f) fs.unlinkSync(f) }
}

// ---- 解析目标仓库 ----
const remoteUrl = git('remote', 'get-url', 'origin')
const m = remoteUrl.match(/github\.com[:/]([^/]+)\/([^/.]+?)(?:\.git)?$/)
if (!m) throw new Error(`无法从 origin 解析 GitHub 仓库：${remoteUrl}`)
const [, owner, repoName] = m
const REPO = `${owner}/${repoName}`
const BRANCH = process.env.HS_PUSH_BRANCH || 'main'

const headSha = git('rev-parse', 'HEAD')
console.log(`仓库      : ${REPO} (${BRANCH})`)
console.log(`本地 HEAD : ${headSha}`)

// ---- 远端当前 tip ----
let remoteTip = null
try { remoteTip = ghJson([`/repos/${REPO}/git/ref/heads/${BRANCH}`]).object.sha } catch { /* 分支尚不存在 */ }
console.log(`远端 tip  : ${remoteTip ?? '(不存在)'}`)

if (remoteTip === headSha) {
  console.log('\n✓ 本地与远端已是同一提交，无需推送。')
  process.exit(0)
}

// ---- 待推提交列表（最早在前）----
let pending = []
if (remoteTip) {
  try {
    pending = git('rev-list', '--reverse', `${remoteTip}..HEAD`).split('\n').filter(Boolean)
  } catch {
    throw new Error(`远端 tip ${remoteTip} 不在本地历史中，两侧已分叉。请先同步远端。`)
  }
} else {
  // 远端为空：把所有提交都推上去
  pending = git('rev-list', '--reverse', 'HEAD').split('\n').filter(Boolean)
}
if (pending.length > DEPTH) pending = pending.slice(pending.length - DEPTH)

if (!pending.length) {
  console.log('\n✓ 没有待推送提交。')
  process.exit(0)
}
console.log(`待推      : ${pending.length} 个提交`)
pending.forEach((s, i) => console.log(`  ${i + 1}. ${s.slice(0, 7)}  ${git('show', '-s', '--format=%s', s).slice(0, 60)}`))

if (DRY) {
  console.log('\n[dry-run] 将依次上传 tree 并创建 commit，不修改远端与本地。')
  process.exit(0)
}

// ---- 单个提交的元数据 ----
function readMeta(sha) {
  const q = (fmt) => git('show', '-s', `--format=${fmt}`, sha)
  return {
    tree: git('rev-parse', `${sha}^{tree}`),
    authorName: q('%an'),
    authorEmail: q('%ae'),
    authorEpoch: Number(q('%at')),
    committerName: q('%cn'),
    committerEmail: q('%ce'),
    committerEpoch: Number(q('%ct')),
    message: execFileSync('git', ['show', '-s', '--format=%B', sha], { encoding: 'utf8' }).replace(/\s+$/, ''),
    subject: q('%s'),
  }
}

// 上传某提交的完整 tree（blob 内容寻址，重复上传同一 blob 返回同一 sha）
function pushTree(sha, meta, label) {
  const listing = execFileSync('git', ['ls-tree', '-r', '-z', sha], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 })
    .split('\0').filter(Boolean)
  console.log(`  [${label}] 上传 ${listing.length} 个文件`)
  const entries = []
  let i = 0
  for (const line of listing) {
    const mm = line.match(/^(\d+)\s+(\w+)\s+([0-9a-f]{40})\t(.+)$/)
    if (!mm) continue
    const [, mode, type, blobSha, p] = mm
    const buf = execFileSync('git', ['cat-file', 'blob', blobSha], { maxBuffer: 256 * 1024 * 1024 })
    const r = ghJson([`/repos/${REPO}/git/blobs`, '-X', 'POST'], {
      content: buf.toString('base64'), encoding: 'base64',
    })
    if (r.sha !== blobSha) throw new Error(`blob 哈希漂移：${p} (${r.sha} != ${blobSha})`)
    entries.push({ path: p, mode, type, sha: r.sha })
    i++
    if (i % 30 === 0 || i === listing.length) console.log(`        ${i}/${listing.length}`)
  }
  const treeRes = ghJson([`/repos/${REPO}/git/trees`, '-X', 'POST'], { tree: entries })
  if (treeRes.sha !== meta.tree) throw new Error(`tree 哈希漂移：${treeRes.sha} != ${meta.tree}`)
  console.log(`        ✓ tree 一致 (${treeRes.sha})`)
  return treeRes.sha
}

const ident = (name, email, s) => ({ name, email, date: new Date(s * 1000).toISOString() })

// ---- 依次推送每个提交 ----
let parent = remoteTip
const rewritten = []
for (let i = 0; i < pending.length; i++) {
  const sha = pending[i]
  const meta = readMeta(sha)
  console.log(`\n[${i + 1}/${pending.length}] ${sha.slice(0, 7)} ${meta.subject.slice(0, 60)}`)

  const treeSha = pushTree(sha, meta, 'tree')
  const commitRes = ghJson([`/repos/${REPO}/git/commits`, '-X', 'POST'], {
    message: meta.message,
    tree: treeSha,
    parents: parent ? [parent] : [],
    author: ident(meta.authorName, meta.authorEmail, meta.authorEpoch),
    committer: ident(meta.committerName, meta.committerEmail, meta.committerEpoch),
  })

  // 按 GitHub 的规范化规则重建本地 commit 对象，使两端 SHA 一致
  const body =
    `tree ${treeSha}\n` +
    (parent ? `parent ${parent}\n` : '') +
    `author ${meta.authorName} <${meta.authorEmail}> ${meta.authorEpoch} +0000\n` +
    `committer ${meta.committerName} <${meta.committerEmail}> ${meta.committerEpoch} +0000\n\n` +
    meta.message
  const b = Buffer.from(body, 'utf8')
  const localSha = createHash('sha1')
    .update(Buffer.concat([Buffer.from(`commit ${b.length}\0`, 'utf8'), b]))
    .digest('hex')

  if (localSha === commitRes.sha) {
    execFileSync('git', ['hash-object', '-t', 'commit', '-w', '--stdin'], { input: b })
    console.log(`      ✓ 远端 ${commitRes.sha.slice(0, 7)}，本地重建 SHA 一致`)
  } else {
    console.log(`      ⚠ 本地重建 ${localSha.slice(0, 7)} 与远端 ${commitRes.sha.slice(0, 7)} 不一致，本地引用不重指`)
  }
  rewritten.push({ local: localSha, remote: commitRes.sha, identical: localSha === commitRes.sha })
  parent = commitRes.sha
}

// ---- 更新远端 ref ----
const finalSha = parent
ghJson([`/repos/${REPO}/git/refs/heads/${BRANCH}`, '-X', 'PATCH'], { sha: finalSha, force: true })
execFileSync('git', ['update-ref', `refs/remotes/origin/${BRANCH}`, finalSha])

// ---- 全部重建成功时，把本地分支重指到重写后的链上（trees 相同，工作区不变）----
const allIdentical = rewritten.every((r) => r.identical)
if (allIdentical) {
  execFileSync('git', ['update-ref', `refs/heads/${BRANCH}`, finalSha])
  console.log(`\n✓ refs/heads/${BRANCH} 与 refs/remotes/origin/${BRANCH} 均指向 ${finalSha.slice(0, 7)}`)
  console.log(`  工作区未变动（trees 完全一致），原提交对象成为不可达对象，可择机 gc。`)
} else {
  console.log(`\n⚠ 有提交未能重建出同一 SHA，本地分支保持原状以免误伤历史。`)
  console.log(`  远端已更新到 ${finalSha.slice(0, 7)}，请人工核对后再决定是否 reset。`)
}
console.log(`\n完成：https://github.com/${REPO}/commit/${finalSha}`)
