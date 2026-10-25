// AUTO-GENERATED, DO NOT EDIT

/**
 * 指标结果模型（架构文档 §2.4.3）。每个 MetricValue 必须可下钻到 event_id。
 */
export interface MetricSet {
  /**
   * 评测执行单元 ID
   */
  run_id: string;
  /**
   * 契约版本，固定为 SCHEMA_VERSION '1.0.0'
   */
  schema_version: '1.0.0';
  /**
   * 产生本指标集的引擎版本
   */
  engine_version: string;
  /**
   * 计算完成时间，ISO 8601 UTC
   */
  computed_at: string;
  groups: {
    /**
     * 完成率 / pass^k / 判定结论
     */
    result: MetricValue[];
    /**
     * 工具正确率 / 轨迹效率 / 重试率 / 冗余循环
     */
    process: MetricValue[];
    /**
     * 6 项多智能体协作指标
     */
    collaboration: MetricValue[];
    /**
     * tokens / ¥ / 墙钟 P50 P95
     */
    cost: MetricValue[];
  };
  /**
   * 因数据缺失而 N/A（value=null）的指标 key 列表
   */
  degraded_metrics: string[];
}
export interface MetricValue {
  /**
   * 指标 key，如 'collab.claim_conflict_rate'
   */
  key: string;
  /**
   * 中文名（报告用）
   */
  label_zh: string;
  /**
   * 指标值；null = 数据不足（N/A），此时 key 必须写入 degraded_metrics
   */
  value: number | boolean | null;
  unit?: 'ratio' | 'count' | 'ms' | 'cny' | 'tokens' | 'score';
  /**
   * 指标方向
   */
  higher_is_better?: boolean;
  /**
   * 样本量不足时标 low（样本量 < 20）
   */
  confidence?: 'high' | 'medium' | 'low';
  /**
   * 必填（可空数组但需在 note 说明原因）；每个数字可下钻到 event_id
   */
  evidence: EvidenceRef[];
}
export interface EvidenceRef {
  /**
   * 支撑本指标值的事件 ID，至少一个
   *
   * @minItems 1
   */
  event_ids: [string, ...string[]];
  span_ids?: string[];
  agent_ids?: string[];
  task_ids?: string[];
  /**
   * 人类可读解释
   */
  note?: string;
}
