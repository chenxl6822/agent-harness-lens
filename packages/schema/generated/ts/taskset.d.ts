// AUTO-GENERATED, DO NOT EDIT

/**
 * 评测任务集 YAML 结构（架构文档 §2.4.4）。
 */
export interface TaskSet {
  /**
   * 契约版本，固定为 SCHEMA_VERSION '1.0.0'
   */
  schema_version: '1.0.0';
  /**
   * 任务集 ID（kebab-case）
   */
  taskset_id: string;
  /**
   * 任务集中文名
   */
  name_zh: string;
  defaults?: {
    /**
     * 单任务默认超时（秒）
     */
    timeout_s?: number;
    /**
     * 单任务默认预算（人民币）
     */
    budget_cny?: number;
    /**
     * pass^k 的重复次数，默认 3，上限 10
     */
    k?: number;
    /**
     * 被测平台
     */
    platform?: 'codebuddy' | 'dsh';
    /**
     * A/B 实验中的固定变量：模型名
     */
    model?: string;
    team?: {
      /**
       * 团队规模（仅 codebuddy Agent Teams 需要）
       */
      size?: number;
      /**
       * 角色列表，如 [lead, impl, review]
       *
       * @minItems 1
       */
      roles?: [string, ...string[]];
    };
  };
  /**
   * @minItems 1
   */
  tasks: [Task, ...Task[]];
}
export interface Task {
  /**
   * 任务 ID（snake_case）
   */
  task_id: string;
  /**
   * 下发给被测智能体的任务描述
   */
  prompt: string;
  verdict: Verdict;
  /**
   * 预期产物路径列表
   */
  artifacts_expect?: string[];
  /**
   * 任务标签（difficulty / category 等），开放结构
   */
  labels?: {
    [k: string]: unknown;
  };
}
export interface Verdict {
  /**
   * 判定器类型
   */
  type: 'assert' | 'script' | 'regex';
  /**
   * type=script 时的判定命令
   */
  run?: string;
  /**
   * type=regex 时的匹配正则
   */
  pattern?: string;
  /**
   * type=assert 时的断言列表
   */
  assertions?: string[];
  /**
   * 判定器超时（秒），默认 60
   */
  timeout_s?: number;
  /**
   * type=script 时的期望退出码，默认 0
   */
  expect_exit_code?: number;
}
