export interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface Dataset { id: number; name: string; description: string; revision: number; enabled_cases: number; created_at: string; updated_at: string }
export interface DatasetInput { name: string; description: string }
export interface Preconditions { device_ref: string | null; order_id: number | null; prior_turns: string[] }
export interface ToolAssertions { sequence?: string[]; max_calls?: Record<string, number> }
export interface CaseInput {
  case_key: string; question: string; expected_answer: string; expected_tools: string[] | null;
  tool_match_mode: 'contains' | 'exact'; tool_assertions?: ToolAssertions; required_metrics: string[]; tags: string[]; enabled: boolean;
  preconditions: Preconditions; source: Record<string, unknown>; license: string; seen_regression: boolean;
}
export interface TestCase extends CaseInput { id: number; dataset_id: number; revision: number; created_at: string; updated_at: string }
export type AdapterType = 'MOCK' | 'HTTP' | 'PRINTER_HTTP'
export type SourceType = 'MOCK' | 'PROTOCOL_DOUBLE' | 'REAL_EXTERNAL'
export interface CredentialRefs { username_ref: string | null; password_ref: string | null; bearer_ref: string | null }
export interface AgentInput {
  name: string; adapter_type: AdapterType; base_url: string; timeout_ms: number;
  credential_refs: CredentialRefs; expected_version: Record<string, string>; enabled: boolean; source_type: SourceType;
}
export interface AgentConfig extends AgentInput { id: number; revision: number; credential_status: Record<string, 'RESOLVED' | 'UNRESOLVED'>; created_at: string; updated_at: string }
export interface FieldError { field: string; code: string; message?: string; value?: string }
export interface ApiProblem { code: string; message: string; trace_id?: string; fields: FieldError[] }

export interface ExperimentInput { name: string; dataset_id: number; agent_config_id: number; version_label: string; purpose: string }
export interface Experiment extends ExperimentInput { id: number; created_at: string }
export interface Progress { total: number; executed?: number; evaluated: number; remaining: number; complete: boolean }
export interface Average { mean: number | null; valid_count: number; total_count: number }
export interface UsageSummary { availability: string; input_tokens: number | null; output_tokens: number | null; total_tokens: number | null; usage_records: number; applicable_records: number; not_requested_records: number; known_input_tokens_records: number; known_output_tokens_records: number; known_total_tokens_records: number; known_part_only: boolean; cost: number | null; cost_currency: string | null; cost_reason: string | null; known_cost_by_currency: Record<string, number> }
export interface Summary {
  N: number; P: number; F: number; U: number; provisional: boolean; interrupted: boolean; run_status: string;
  effective_pass_rate: number | null; determinate_coverage: number | null; metric_averages: Record<string, Average>;
  agent: { status_counts: Record<string, number>; executed_count: number; total_count: number; success_duration_mean_ms: number | null; success_duration_valid_count: number; timeout_count: number; timeout_rate: number | null; timeout_denominator: number };
  judge: { status_counts: Record<string, number> };
  tools: { normal_completion: number; business_refusal: number; technical_failure: number; unknown: number; unknown_source: number; event_count: number; denominator: number; success_rate: number | null; trajectory_counts: Record<string, number>; sources: string[]; reason: string | null };
  usage: { Agent: UsageSummary; Judge: UsageSummary }; source_scope: Record<string, unknown>;
}
export interface Run { id: string; experiment_id: number; status: string; total_cases: number; created_at: string; started_at?: string | null; ended_at?: string | null; score_set_id: string; source_type: SourceType; snapshot: Record<string, unknown>; snapshot_sha256: string; progress?: Progress; summary?: Summary; error?: Record<string, unknown> | null }
export interface ScoreSet { id: string; run_id: string; kind: string; parent_score_set_id: string | null; status: string; fingerprint: string; created_at: string; judge_snapshot: Record<string, unknown>; rule_snapshot: Record<string, unknown>; progress?: Progress }
export interface Metric { metric: string; status: string; score: number | null; required: boolean; reason: string; evidence: Record<string, unknown> }
export interface Evaluation { id: string; run_id: string; case_key: string; question: string; agent_status: string | null; conclusion: string | null; source_type: SourceType; automatic_tags: string[]; evaluation_issues: string[]; annotation?: Annotation }
export interface Annotation { manual_tags: string[]; note: string; updated_at?: string | null }
export interface CaseRun { id: string; run_id: string; case_key: string; case_snapshot: CaseInput; phase: string; agent_status: string | null; answer: string | null; raw_agent: Record<string, unknown> | null; trace_id: string | null; observations: Record<string, unknown> | null; usage: Record<string, unknown> | null; timing: Record<string, unknown> | null; error: Record<string, unknown> | null }
export interface CaseDetail { case: CaseRun; score_set: ScoreSet; score_sets: ScoreSet[]; scoring: { evaluation: { status: string; conclusion: string; completed_at: string } | null; metrics: Metric[]; judge_attempts: { attempt: number; status: string; request: unknown; response: unknown; usage: unknown; duration_ms: number; error: unknown }[] }; traces: { event_id: string; sequence: number; name: string; source: string; status: string; duration_ms: number | null; completeness: string; payload?: unknown; result?: unknown }[]; annotation: Annotation; automatic_tags: string[]; evaluation_issues: string[]; version_verification: VersionVerification }
export interface VersionVerification { candidate_label: string; status: string; dimensions: { dimension: string; expected: unknown; observed: unknown[]; status: string; verification: string; expected_match: boolean | null; verified_count: number; observed_count: number; total_count: number; reason: string }[]; limitations: string[] }
export interface PairedMetric { metric: string; paired_count: number; left_valid_count: number; right_valid_count: number; mean_scope: string; left_mean: number | null; right_mean: number | null; delta: number | null; unpaired_reasons: Record<string, number>; pairs: { case_key: string; left_case_run_id: string; right_case_run_id: string; left_value: number; right_value: number; delta: number }[] }
export interface Comparison { comparable: boolean; limitations: string[]; left: { run: Run; score_set: ScoreSet; summary: Summary }; right: { run: Run; score_set: ScoreSet; summary: Summary }; metrics: PairedMetric[]; cases: { case_key: string; left_case_run_id?: string; right_case_run_id?: string; left_conclusion: string | null; right_conclusion: string | null; content_match: boolean; reason: string | null }[]; version_comparison: { verified_change: boolean; status: string; dimensions: unknown[]; limitations: string[] } }
export interface ChartDatum { name: string; value: number | null; case_run_ids: string[]; link: string; right_case_run_ids?: string[]; right_link?: string; left_value?: number | null; right_value?: number | null; paired_count?: number; left_valid_count?: number; right_valid_count?: number; mean_scope?: string; reason?: string | null }
export interface ReportChart { title: string; available: boolean; reason: string | null; data: ChartDatum[] }
export interface Report { run: Run; score_set: ScoreSet; score_sets: ScoreSet[]; summary: Summary; source_scope: Record<string, unknown>; version_verification: VersionVerification; charts: Record<'experiment_comparison' | 'metric_changes' | 'badcase_categories' | 'latency_distribution', ReportChart>; comparison?: Comparison }
export interface Dashboard { source_scope: unknown; recent_runs: Run[]; summary: Summary | null; selected_run_id: string | null }
