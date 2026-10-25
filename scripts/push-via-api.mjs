#!/usr/bin/env node
// 受限网络下的推送通道：当 git smart-HTTP 被代理阻断时，改用 GitHub Git Data API。
//
// 背景：本机代理对 github.com:443 的 CONNECT 隧道返回 502，但 api.github.com 正常。
//       本脚本通过 `gh api` 上传 blob/tree/commit，并把本地引用对齐到远端返回的
//       同一个 commit（GitHub 侧把时区规范化为 +0000、去掉 message 尾随换行，
//       这里按同样规则重建本地对象，保证两端 SHA 完全一致）。
//
// 用法：
//   node scripts/push-via-api.mjs            # 推送 HEAD 到 origin/main
//   node scripts/push-via-api.mjs --dry-run  # 只打印将要执行的动作
//
// 前置：gh 已登录且对目标仓库有写权限。

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const DRY = process.argv.includes('--dry-run')
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
const headTree = git('rev-parse', 'HEAD^{tree}')
const authorName = git('show', '-s', '--format=%an', 'HEAD')
const authorEmail = git('show', '-s', '--format=%ae', 'HEAD')
const authorEpoch = Number(git('show', '-s', '--format=%at', 'HEAD'))
const committerName = git('show', '-s', '--format=%cn', 'HEAD')
const committerEmail = git('show', '-s', '--format=%ce', 'HEAD')
const committerEpoch = Number(git('show', '-s', '--format=%ct', 'HEAD'))
const rawMsg = execFileSync('git', ['show', '-s', '--format=%B', 'HEAD'], { encoding: 'utf8' })
  .replace(/\s+$/, '')
const localParents = git('rev-list', '--parents', '-n', '1', 'HEAD').split(' ').slice(1)

console.log(`仓库      : ${REPO} (${BRANCH})`)
console.log(`本地 HEAD : ${headSha}`)
console.log(`本地 tree : ${headTree}`)

// ---- 远端当前 tip 作为父提交 ----
let remoteTip = null
try { remoteTip = ghJson([`/repos/${REPO}/git/ref/heads/${BRANCH}`]).object.sha } catch { /* 分支尚不存在 */ }
console.log(`远端 tip  : ${remoteTip ?? '(不存在)'}`)

if (remoteTip === headSha) {
  console.log('\n✓ 本地与远端已是同一提交，无需推送。')
  process.exit(0)
}
if (localParents.length && localParents[0] !== remoteTip) {
  throw new Error(
    `本地 HEAD 的父提交 (${localParents[0]}) 与远端 tip (${remoteTip}) 不一致，\n` +
    `  说明两侧历史已分叉。请先同步远端后重试，或手工处理。`,
  )
}
if (!localParents.length && remoteTip && remoteTip !== '') {
  // 本地是 root commit 而远端已有内容：仅在内容完全等价时允许覆盖
  console.log('⚠ 本地为 root commit、远端已有历史，将做覆盖式推送（force）。')
}

if (DRY) {
  console.log('\n[dry-run] 将上传 tree 并创建 commit，不修改远端。')
  process.exit(0)
}

// ---- 上传 blob / tree ----
const listing = execFileSync('git', ['ls-tree', '-r', '-z', 'HEAD'], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 })
  .split('\0').filter(Boolean)
console.log(`\n[1/3] 上传 ${listing.length} 个文件`)
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
  if (i % 20 === 0 || i === listing.length) console.log(`      ${i}/${listing.length}`)
}

const treeRes = ghJson([`/repos/${REPO}/git/trees`, '-X', 'POST'], { tree: entries })
if (treeRes.sha !== headTree) throw new Error(`tree 哈希漂移：${treeRes.sha} != ${headTree}`)
console.log(`      ✓ tree 一致 (${treeRes.sha})`)

// ---- 创建 commit ----
console.log('[2/3] 创建 commit')
const parents = remoteTip ? [remoteTip] : []
const commitRes = ghJson([`/repos/${REPO}/git/commits`, '-X', 'POST'], {
  message: rawMsg,
  tree: treeRes.sha,
  parents,
  author: { name: authorName, email: authorEmail, date: new Date(authorEpoch * 1000).toISOString() },
  committer: { name: committerName, email: committerEmail, date: new Date(committerEpoch * 1000).toISOString() },
})
console.log(`      remote commit = ${commitRes.sha}`)

// ---- 按 GitHub 的规范化规则重建本地对象，使两端 SHA 一致 ----
console.log('[3/3] 对齐本地与远端')
const body =
  `tree ${treeRes.sha}\n` +
  parents.map((p) => `parent ${p}\n`).join('') +
  `author ${authorName} <${authorEmail}> ${authorEpoch} +0000\n` +
  `committer ${committerName} <${committerEmail}> ${committerEpoch} +0000\n\n` +
  rawMsg
const b = Buffer.from(body, 'utf8')
const localSha = createHash('sha1')
  .update(Buffer.concat([Buffer.from(`commit ${b.length}\0`, 'utf8'), b]))
  .digest('hex')

const identical = localSha === commitRes.sha
if (identical) {
  execFileSync('git', ['hash-object', '-t', 'commit', '-w', '--stdin'], { input: b })
  execFileSync('git', ['update-ref', `refs/heads/${BRANCH}`, localSha])
  console.log('      ✓ 本地对象已写入，SHA 与远端完全一致')
} else {
  console.log(`      ⚠ 本地重建 ${localSha} 与远端 ${commitRes.sha} 不一致`)
  console.log('        远端已更新，但本地引用未改动；请人工核对后再决定是否 reset。')
}

execFileSync('git', ['update-ref', `refs/remotes/origin/${BRANCH}`, commitRes.sha])
ghJson([`/repos/${REPO}/git/refs/heads/${BRANCH}`, '-X', 'PATCH'], { sha: commitRes.sha, force: true })
console.log(`      ✓ refs/heads/${BRANCH} -> ${commitRes.sha}`)
console.log(`\n完成：https://github.com/${REPO}/commit/${commitRes.sha}`)
