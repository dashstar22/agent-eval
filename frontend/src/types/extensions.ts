import type { CaseInput, Comparison, Metric, Summary, VersionVerification } from './index'

export interface Selection { run_id: string; score_set_id?: string }
export type QualityConclusion = 'PASS' | 'FAIL' | 'UNDETERMINED'
export interface ReviewCase {
  blind: boolean; identity_notice: string;
  case: { id: string; run_id: string; case_key: string; question: string; expected_answer: string; answer: string | null; expected_tools: string[] | null; tool_match_mode: string; tool_assertions?: unknown; preconditions: unknown; contexts: unknown; context_scope: string; context_completeness: string };
  score_set: { id: string; kind: string; status: string }; applicable_metrics: string[];
  judge: null | { status: string; conclusion: string; metrics: Metric[] };
}
export interface HumanReview { id: string; run_id: string; case_run_id: string; score_set_id: string; reviewer_name: string; identity_type: string; conclusion: QualityConclusion; scores: Record<string, number>; reason: string; human_confirmed: boolean; created_at: string }
export interface ReviewInput { case_run_id: string; score_set_id: string; reviewer_name: string; conclusion: QualityConclusion; scores: Record<string, number>; reason: string; human_confirmed: true }
export interface ReviewHistory { items: HumanReview[]; latest_reviews: HumanReview[]; total: number; run_id: string; score_set_id: string; identity_notice: string }
export interface Consistency {
  run_id: string; score_set_id: string; review_count: number; latest_review_count: number; identity_notice: string;
  conclusions: { valid_pair_count: number; agreement_count: number; agreement_rate: number | null; disagreements: { case_key: string; case_run_id: string; human_conclusion: string; judge_conclusion: string }[] };
  metrics: { metric: string; valid_pair_count: number; exact_agreement_count: number; exact_agreement_rate: number | null; mean_absolute_difference: number | null; disagreements: { case_key: string; case_run_id: string; human_score: number; judge_score: number }[] }[];
}
export interface RegressionDraft { id: string; run_id: string; case_run_id: string; score_set_id: string; target_dataset_id: number; case_key: string; case_snapshot: CaseInput; reason: string; status: 'PENDING' | 'APPROVED'; approved_by: string | null; approved_at: string | null; test_case_id: number | null; created_at: string }
export interface RegressionPolicy { max_new_failures: number; min_coverage: number; max_pass_rate_drop: number }
export interface RegressionCheck {
  verdict: 'PASS' | 'FAIL' | 'INCONCLUSIVE'; policy: RegressionPolicy;
  conditions: { completed: boolean; comparable: boolean; coverage_sufficient: boolean; limitations: string[]; version_evidence: { baseline: VersionVerification; candidate: VersionVerification; comparison: Comparison['version_comparison'] }; source_scope: { baseline: Summary['source_scope']; candidate: Summary['source_scope'] } };
  checks: { id: string; label: string; status: 'PASS' | 'FAIL' | 'INCONCLUSIVE'; actual: number | null; threshold: number | null; reason: string | null }[];
  new_failures: Comparison['cases']; fixed_cases: Comparison['cases']; unknown_cases: Comparison['cases']; comparison: Comparison;
}
export interface StabilityAnalysis {
  comparable: boolean; limitations: string[]; run_count: number;
  runs: { run_id: string; score_set_id: string; run_status: string; score_set_status: string; kind: string; summary: Summary; source_scope: Summary['source_scope']; version_verification: VersionVerification }[];
  cases: { case_key: string; P: number | null; F: number | null; U: number | null; valid_count: number; total_count: number; flipped: boolean | null; flip_count: number | null; observations: { run_id: string; score_set_id: string; case_run_id: string; conclusion: string; agent_status: string }[]; links: unknown[] }[];
  metrics: { metric: string; valid_count: number; total_count: number; mean: number | null; stddev: number | null; min: number | null; max: number | null; reason: string | null }[];
  conditions: { frozen_conditions_match: boolean; completed: boolean; actual_versions_verified: boolean };
}
export interface RepeatBatch { id: string; status: string; source_run_id: string; repeat_count: number; created_at: string; started_at: string | null; ended_at: string | null; runs: { id: string; ordinal: number; status: string; score_set_id: string }[]; budget: { per_run_max_calls: number | null; total_max_calls: number | null; per_run_timeout_seconds: number; total_timeout_seconds: number; real_execution: boolean }; error: unknown; idempotent_replay?: boolean }
