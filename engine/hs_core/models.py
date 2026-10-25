# AUTO-GENERATED, DO NOT EDIT
# Source of truth: packages/schema/*.schema.json (single source of truth)
# Regenerate with: make gen (json-schema -> datamodel-code-generator -> pydantic v2)

from __future__ import annotations
from enum import StrEnum
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, conint, constr
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, constr
from pydantic import BaseModel, ConfigDict, Field
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, conint, constr
from typing import Any, Literal
from typing import Literal

# ---- from trace-event.schema.json ----
class Platform(StrEnum):
    codebuddy = 'codebuddy'
    dsh = 'dsh'
    generic = 'generic'

class Channel(StrEnum):
    hook = 'hook'
    file = 'file'
    otlp = 'otlp'
    sdk = 'sdk'
    cli = 'cli'

class AgentRole(StrEnum):
    lead = 'lead'
    teammate = 'teammate'
    subagent = 'subagent'
    system = 'system'

class DegradedLevel(StrEnum):
    L1 = 'L1'
    L2 = 'L2'
    L3 = 'L3'

class EventType(StrEnum):
    run_start = 'run.start'
    run_end = 'run.end'
    session_start = 'session.start'
    session_end = 'session.end'
    agent_spawn = 'agent.spawn'
    agent_stop = 'agent.stop'
    turn_start = 'turn.start'
    turn_end = 'turn.end'
    step_start = 'step.start'
    step_end = 'step.end'
    llm_request = 'llm.request'
    llm_response = 'llm.response'
    tool_call = 'tool.call'
    tool_result = 'tool.result'
    tool_error = 'tool.error'
    task_create = 'task.create'
    task_claim = 'task.claim'
    task_update = 'task.update'
    task_complete = 'task.complete'
    message_send = 'message.send'
    message_recv = 'message.recv'
    artifact_write = 'artifact.write'
    artifact_ref = 'artifact.ref'
    context_compact = 'context.compact'
    error = 'error'
    metric_custom = 'metric.custom'

class Source(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    platform: Platform
    version: str | None = Field(None, description='被测平台版本号（如 dsh 0.2.1-rc.2）')
    channel: Channel
    degraded: DegradedLevel | None = None

class TraceEvent(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: Literal['1.0.0'] = Field(
        ..., description="契约版本，固定为 SCHEMA_VERSION '1.0.0'"
    )
    event_id: constr(pattern=r'^[0-9A-HJKMNP-TV-Z]{26}$') = Field(
        ..., description='ULID，字典序即时间序'
    )
    run_id: constr(min_length=1) = Field(..., description='评测执行单元 ID')
    seq: conint(ge=0) = Field(..., description='run 内单调自增序号（断点续跑用）')
    ts: AwareDatetime = Field(
        ..., description='事件发生时间，ISO 8601 UTC，毫秒精度（墙钟）'
    )
    ts_mono_ms: conint(ge=0) | None = Field(
        None,
        description='同机单调时钟毫秒值（clock_gettime MONOTONIC / performance.now 来源），用于同机精确耗时计算，跨机不可比',
    )
    session_id: constr(min_length=1) = Field(..., description='被测平台会话 ID')
    agent_id: str | None = Field(
        None, description='产生者智能体 ID（Lead / Teammate / Subagent）'
    )
    agent_role: AgentRole | None = None
    parent_agent_id: str | None = Field(None, description='委托链上的父智能体 ID')
    trace_id: str | None = Field(
        None, description='OpenTelemetry trace id（GenAI 语义约定对齐）'
    )
    span_id: str | None = Field(None, description='OpenTelemetry span id')
    parent_span_id: str | None = Field(None, description='OpenTelemetry 父 span id')
    type: EventType
    source: Source
    labels: dict[str, Any] = Field(
        ...,
        description='维度标签（variant / model / taskset / task_id / team 等），开放结构',
    )
    payload: dict[str, Any] = Field(
        ..., description='事件主体（已脱敏；未知字段原样透传），开放结构'
    )
    raw_ref: str | None = Field(
        None, description='原始记录归档指针（file:line 或 jsonl offset）'
    )
    redacted: bool | None = Field(None, description='本事件是否命中脱敏规则')


# ---- from metric-set.schema.json ----
class Unit(StrEnum):
    ratio = 'ratio'
    count = 'count'
    ms = 'ms'
    cny = 'cny'
    tokens = 'tokens'
    score = 'score'

class Confidence(StrEnum):
    high = 'high'
    medium = 'medium'
    low = 'low'

class EvidenceRef(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    event_ids: list[str] = Field(
        ..., description='支撑本指标值的事件 ID，至少一个', min_length=1
    )
    span_ids: list[str] | None = None
    agent_ids: list[str] | None = None
    task_ids: list[str] | None = None
    note: str | None = Field(None, description='人类可读解释')

class MetricValue(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    key: constr(pattern=r'^(result|process|collab|cost)\.[a-z][a-z0-9_]*$') = Field(
        ..., description="指标 key，如 'collab.claim_conflict_rate'"
    )
    label_zh: constr(min_length=1) = Field(..., description='中文名（报告用）')
    value: float | bool | None = Field(
        ...,
        description='指标值；null = 数据不足（N/A），此时 key 必须写入 degraded_metrics',
    )
    unit: Unit | None = None
    higher_is_better: bool | None = Field(None, description='指标方向')
    confidence: Confidence | None = Field(
        None, description='样本量不足时标 low（样本量 < 20）'
    )
    evidence: list[EvidenceRef] = Field(
        ...,
        description='必填（可空数组但需在 note 说明原因）；每个数字可下钻到 event_id',
    )

class Groups(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    result: list[MetricValue] = Field(..., description='完成率 / pass^k / 判定结论')
    process: list[MetricValue] = Field(
        ..., description='工具正确率 / 轨迹效率 / 重试率 / 冗余循环'
    )
    collaboration: list[MetricValue] = Field(..., description='6 项多智能体协作指标')
    cost: list[MetricValue] = Field(..., description='tokens / ¥ / 墙钟 P50 P95')

class MetricSet(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    run_id: constr(min_length=1) = Field(..., description='评测执行单元 ID')
    schema_version: Literal['1.0.0'] = Field(
        ..., description="契约版本，固定为 SCHEMA_VERSION '1.0.0'"
    )
    engine_version: constr(min_length=1) = Field(
        ..., description='产生本指标集的引擎版本'
    )
    computed_at: AwareDatetime = Field(..., description='计算完成时间，ISO 8601 UTC')
    groups: Groups
    degraded_metrics: list[str] = Field(
        ..., description='因数据缺失而 N/A（value=null）的指标 key 列表'
    )


# ---- from taskset.schema.json ----
class Team(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    size: conint(ge=1) | None = Field(
        None, description='团队规模（仅 codebuddy Agent Teams 需要）'
    )
    roles: list[str] | None = Field(
        None, description='角色列表，如 [lead, impl, review]', min_length=1
    )

class Defaults(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    timeout_s: conint(ge=1) | None = Field(None, description='单任务默认超时（秒）')
    budget_cny: PositiveFloat | None = Field(
        None, description='单任务默认预算（人民币）'
    )
    k: conint(ge=1, le=10) | None = Field(
        None, description='pass^k 的重复次数，默认 3，上限 10'
    )
    platform: Platform | None = Field(None, description='被测平台')
    model: constr(min_length=1) | None = Field(
        None, description='A/B 实验中的固定变量：模型名'
    )
    team: Team | None = None

class Type(StrEnum):
    assert_ = 'assert'
    script = 'script'
    regex = 'regex'

class Verdict(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    type: Type = Field(..., description='判定器类型')
    run: str | None = Field(None, description='type=script 时的判定命令')
    pattern: str | None = Field(None, description='type=regex 时的匹配正则')
    assertions: list[str] | None = Field(None, description='type=assert 时的断言列表')
    timeout_s: conint(ge=1) | None = Field(
        None, description='判定器超时（秒），默认 60'
    )
    expect_exit_code: int | None = Field(
        None, description='type=script 时的期望退出码，默认 0'
    )

class Task(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    task_id: constr(pattern=r'^[a-z][a-z0-9_]*$') = Field(
        ..., description='任务 ID（snake_case）'
    )
    prompt: constr(min_length=1) = Field(..., description='下发给被测智能体的任务描述')
    verdict: Verdict
    artifacts_expect: list[str] | None = Field(None, description='预期产物路径列表')
    labels: dict[str, Any] | None = Field(
        None, description='任务标签（difficulty / category 等），开放结构'
    )

class TaskSet(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: Literal['1.0.0'] = Field(
        ..., description="契约版本，固定为 SCHEMA_VERSION '1.0.0'"
    )
    taskset_id: constr(pattern=r'^[a-z][a-z0-9-]*$') = Field(
        ..., description='任务集 ID（kebab-case）'
    )
    name_zh: constr(min_length=1) = Field(..., description='任务集中文名')
    defaults: Defaults | None = None
    tasks: list[Task] = Field(..., min_length=1)


# ---- from capability.schema.json ----
class Telemetry(StrEnum):
    ok = 'ok'
    degraded = 'degraded'
    none = 'none'

class SupportedEvent(StrEnum):
    run_start = 'run.start'
    run_end = 'run.end'
    session_start = 'session.start'
    session_end = 'session.end'
    agent_spawn = 'agent.spawn'
    agent_stop = 'agent.stop'
    turn_start = 'turn.start'
    turn_end = 'turn.end'
    step_start = 'step.start'
    step_end = 'step.end'
    llm_request = 'llm.request'
    llm_response = 'llm.response'
    tool_call = 'tool.call'
    tool_result = 'tool.result'
    tool_error = 'tool.error'
    task_create = 'task.create'
    task_claim = 'task.claim'
    task_update = 'task.update'
    task_complete = 'task.complete'
    message_send = 'message.send'
    message_recv = 'message.recv'
    artifact_write = 'artifact.write'
    artifact_ref = 'artifact.ref'
    context_compact = 'context.compact'
    error = 'error'
    metric_custom = 'metric.custom'

class Status(StrEnum):
    ok = 'ok'
    degraded = 'degraded'
    none = 'none'

class Degraded(StrEnum):
    L1 = 'L1'
    L2 = 'L2'
    L3 = 'L3'

class ChannelStatus(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    channel: Channel = Field(..., description='采集通道')
    status: Status = Field(..., description='该通道可用性')
    degraded: Degraded | None = Field(
        None,
        description='降级等级（dsh 三重降级链：L1 逐条轨迹 / L2 黑盒结果 / L3 仅元数据）',
    )
    note: str | None = Field(None, description='该通道说明')

class CapabilityReport(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    platform: Platform = Field(..., description='被测平台')
    version: str | None = Field(None, description='探测到的平台版本号')
    telemetry: Telemetry = Field(..., description='遥测能力总体状态')
    channels: list[ChannelStatus] = Field(
        ..., description='各采集通道逐项探测结果', min_length=1
    )
    supported_events: list[SupportedEvent] = Field(
        ..., description='该平台实际可采集的事件类型（TraceEvent.type 的子集）'
    )
    notes: list[str] | None = Field(None, description='降级原因、缺失能力说明')

