// AUTO-GENERATED, DO NOT EDIT

/**
 * 能力探测报告（版本隔离层输出）。先探测再决定通道，绝不假设 API 存在。
 */
export interface CapabilityReport {
  /**
   * 被测平台
   */
  platform: 'codebuddy' | 'dsh' | 'generic';
  /**
   * 探测到的平台版本号
   */
  version?: string;
  /**
   * 遥测能力总体状态
   */
  telemetry: 'ok' | 'degraded' | 'none';
  /**
   * 各采集通道逐项探测结果
   *
   * @minItems 1
   */
  channels: [ChannelStatus, ...ChannelStatus[]];
  /**
   * 该平台实际可采集的事件类型（TraceEvent.type 的子集）
   */
  supported_events: (
    | 'run.start'
    | 'run.end'
    | 'session.start'
    | 'session.end'
    | 'agent.spawn'
    | 'agent.stop'
    | 'turn.start'
    | 'turn.end'
    | 'step.start'
    | 'step.end'
    | 'llm.request'
    | 'llm.response'
    | 'tool.call'
    | 'tool.result'
    | 'tool.error'
    | 'task.create'
    | 'task.claim'
    | 'task.update'
    | 'task.complete'
    | 'message.send'
    | 'message.recv'
    | 'artifact.write'
    | 'artifact.ref'
    | 'context.compact'
    | 'error'
    | 'metric.custom'
  )[];
  /**
   * 降级原因、缺失能力说明
   */
  notes?: string[];
}
export interface ChannelStatus {
  /**
   * 采集通道
   */
  channel: 'hook' | 'file' | 'otlp' | 'sdk' | 'cli';
  /**
   * 该通道可用性
   */
  status: 'ok' | 'degraded' | 'none';
  /**
   * 降级等级（dsh 三重降级链：L1 逐条轨迹 / L2 黑盒结果 / L3 仅元数据）
   */
  degraded?: 'L1' | 'L2' | 'L3';
  /**
   * 该通道说明
   */
  note?: string;
}
