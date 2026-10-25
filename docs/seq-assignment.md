# seq-assignment.md — seq 作用域决策与赋号纯函数（T05a E2）

## 决策

**scope = `run`（run 内全局单调），非 per-session。**

理由：

1. **契约文字如此**：`trace-event.schema.json` 的 `seq` 描述是「run 内单调自增」，本卡职责是消除契约与实现的表述不一致，而不是单方面改契约语义（改结构需走 `schema_change_request`）。
2. **消费方是全局排序**：T05b 的幂等重放与断点续跑、T06 的跨 session 时序归并（同一 run 内多 agent 并发），都需要一个 run 级全序键。per-session seq 只能给出 session 内全序，跨 session 还得引入第二排序键，得不偿失。
3. **通道现实**：通道 A 的 session_id 是 traceId 占位（T03 偏差 D5），A↔B/C 归并尚是遗留问题——per-session 作用域会把归并问题放大成 seq 分配问题；run 级赋号对 session 归并不敏感。
4. **断点续跑**：run 级 0..n-1 重排意味着增量摄入时已入库事件的 seq 可能变化。这不是本卡缺陷而是本卡**显式声明**的边界：`assign_seq` 是批内纯函数，**T05b 落 SQLite 时需要为已赋号区间加游标锁**（或改用「只追加、不重排」的分配策略），已登记进 result.json 的 open_questions。

## 纯函数签名与语义

```python
# engine/hs_core/collector.py
def assign_seq(events: list[dict]) -> list[dict]:
    """run 内全局单调赋号。输入顺序无关、输出确定。

    排序键 = (ts 毫秒, event_id)
    - ts 毫秒：ISO-8601 解析，失败记 0（排最前，不丢事件）
    - event_id：确定性 ULID = 48bit 时间戳(ms) + 80bit sha256(白名单键 + 通道稳定标识)
      的 Crockford-Base32 编码 —— 白名单键显式列举为
      {ts, session_id, type, source, payload, agent_id, parent_agent_id} + stable_key，
      不含 raw_ref/行号/字节偏移（R1-1）；同内容必同 id，同 ts 不同内容由内容摘要决定次序
    输出：按全序排列的事件列表，seq 被覆盖写为 0..n-1。
    """
```

配套的 `make_event_id(ts, content)` 保证：同一批事件无论以什么顺序、重复摄入多少次，`event_id` 与 `seq` 完全一致——这是幂等（G3）与确定性（G4）的共同基础。

## 与通道适配器占位值的关系

- T02 `normalize.ts` 写 `seq: 0`：进程间无共享状态，全局自增必然引入锁（hook-contract §5.3）。本卡把注释改为「占位值仅在摄入前有效，由 assign_seq 统一赋号」。
- T03 文件通道同样写 0。通道侧永远不该赋真值。
- `trace-event.schema.json` 的 `seq.description` 已同步更新（仅 description，`npm run check-gen` 绿，结构零改动）。

## 实测证据（G4，R1 更正）

> **口径更正（R1-2）**：旧版此处声明的哈希 `931166d0…` 仅由 `assign_seq` 纯函数产生
> （构造已带 event_id 的事件对象、打乱后只调纯函数），被错误写成「管线性质」，
> 验收实测端到端乱序后 603/603 event_id 全变，**该哈希作废**。

**端到端证据（新）**：真实样本 `probe/rc3c/raw/e1c-events.ndjson`（1646 行）乱序 3 次
（seed 1/2/3）跑完整 `collect()`（摄入 → 映射 → event_id → 去重 → seq → 落盘），
整文件 sha256 三次全等：

```
80aa646831041da2a767f257cedc57eb7b1c5f1410d7635c45300ab2413b8fed（3 次全等）
```

该哈希覆盖端到端管线，非仅纯函数。成立前提（R1-1/R1-2 落地）：

1. `event_id` 摘要为白名单键 + 通道稳定标识，`raw_ref`（含行号）不入哈希，
   且 raw_ref 本身改为「相对路径 + 稳定锚点」（`#span=<spanId>`、`#ledger=<sid>:<seq>` 等），
   文件追加/重写后不漂移；
2. `message.recv` 的 inbox 多源观测按固定优先级（claimed > inserted）+ min(ts) 确定性归并；
3. dsh 直挂/镜像双计按最小 ts 确定性归并。

复现：`engine/tests/test_collector.py::test_pipeline_is_order_independent`
（须带 `--basetemp=.tmp-pytest`）。

幂等重放（G3）：同一输入两次摄入，`events.normalized.ndjson` sha256 相同（R1 后重测）：

```
0133629e22a3afe5da18b17026391af3463bf45f7a2fff881756118243fe2f81（两次全等）
```

复现：`engine/tests/test_collector.py::test_idempotent_replay` / `::test_event_id_stable_across_append`
（后者验证：原文件尾部追加无关新行后重新摄入，原事件 event_id 不变）。
