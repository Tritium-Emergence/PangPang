// Server-truth shapes consumed verbatim from /api/v1/snapshot. These mirror the
// backend projection (pangpang/attention.py, service.effective_capabilities,
// onboarding.status). The client NEVER re-derives attention/readiness — it only reads
// these fields (correctness gate).

export type AttentionState =
  | 'needs_attention' | 'blocked' | 'working' | 'done_unread' | 'settled' | 'unknown';

export interface Work {
  id: string;
  owner_bot_id: string;
  goal?: string;
  status?: string;
  cancel_requested?: boolean; // Work-level durable "stopping" truth — not derived from tasks
  kind?: string;
  parent_work_id?: string | null;
  attention?: AttentionState; // server-projected — read, never compute
  created?: number;
  checkpoint?: Record<string, unknown>;
  continued_from?: string;
  project_id?: string;
  routine_id?: string;
}

export interface AttentionSummary {
  needs_attention: number;
  working: number;
  done_unread: number;
  blocked: number;
  scheduled: number;
  indeterminate: number;
  complete: boolean;
}

export interface Bot {
  id: string;
  name: string;
  role?: string;
  kind?: string;
  lifecycle?: string;
  instructions?: string;
  model_profile_id?: string;
  revision?: number;
}

export interface Task {
  id: string;
  status: string;
  work_id?: string;
  owner_bot_id?: string;
  installation_id?: string;
  host_id?: string;
  prompt?: string;
  spec_digest?: string;
  approved_at?: number;
  installation_authorization?: string;
  authorization_digest?: string;
  duration?: number;
  device_kb?: null;
  run_preview?: {
    host_id?: string;
    host_name?: string;
    command?: string[];
    cwd?: string;
    allowed_roots?: string[];
    supervision?: string | null;
    process_group_consent?: string | null;
    credential_ids?: string[];
    truncated?: boolean;
  };
  created?: number;
  started?: number;
  required_outputs?: string[];
  activity?: {
    status?: string;
    last_log_at?: number | null;
    observation_capped?: 'maybe' | 'no';
    recent?: {tool: string; path?: string}[];
    present_outputs?: {path: string; present: boolean}[];
  };
  route?: unknown;
  inputs?: unknown;
  outputs?: unknown;
  receipt?: unknown;
  operator_evidence?: string;
  error?: string;
}

export interface Turn {
  id: string;
  bot_id?: string;
  work_id?: string;
  status?: string;
  error?: string;
  created?: number;
  recovery_dispatched?: string | false;
}

export interface Artifact {
  id: string;
  path: string;
  host_id?: string;
  size?: number | null;
  source?: string;
  work_id?: string | null;
}

export interface Routine {
  id: string;
  owner_bot_id?: string;
  instruction?: string;
  approval_state?: string;
  active?: boolean;
  approval_digest?: string;
  authority?: {grant_write?: boolean; required_host_id?: string; required_installation_id?: string};
  owner_policy?: {name?: string; role?: string; instructions?: string};
  schedule?: {type?: string; time?: string; weekdays?: string[]; minutes?: number; at?: string};
  timezone?: string;
  next_run_at?: number;
  last_run_at?: number;
  last_manual_run_at?: number;
}

export interface Notification {
  id: string;
  bot_id?: string;
  kind?: string;
  title?: string;
  read?: boolean;
  work_id?: string;
  created?: number;
}

export interface Capability {
  capability: string;
  state: 'ready' | 'unconfigured' | 'blocked' | 'unknown';
  reason?: string | null;
  provider?: string | null;
  operator_action_code?: string | null;
  operator_action?: string | null;
  restart_required?: boolean;
  detail?: Record<string, unknown>;
}

export interface Setup {
  model: {endpoint: string; model: string; vision: boolean; configured: boolean; has_secret: boolean; provider?: string; api?: string; reasoning_level?: string; thinking_format?: string; thinking_budget_field?: string; thinking_budget?: number; max_tokens_field?: string; output_tokens?: number; context_window_tokens?: number; has_legacy_generation_settings?: boolean};
  model_max_concurrency?: number;
  model_concurrency_restart_required?: boolean;
  capabilities: Capability[]; // effective capability truth — read, never guess readiness
  coordinator_env?: {name: string; configured: boolean; sealed: boolean}[];
  requests: SetupRequest[];
  inventory: unknown[];
}

export interface SetupRequest {
  id: string;
  status: string;
  host_id?: string;
  proposal?: Record<string, unknown>;
}

export interface Project {id: string; title: string; owner_bot_id?: string; goal?: string}
export interface Exchange {
  id: string; status?: string; source_bot_id?: string; target_bot_id?: string;
  source_work_id?: string; target_work_id?: string; work_id?: string; summary?: string; request?: string;
}
export interface Host {id: string; name?: string; os?: string; transport?: string; execution_level?: string}
export interface Device {id: string; online?: boolean; revoked?: boolean; last_seen?: number}
export interface Installation {id: string; host_id?: string; [k: string]: unknown}
export interface Probe {id: string; available?: boolean; version?: string; reason?: string}
export interface Model {id: string; model?: string; configured?: boolean; context_window_tokens?: number}
export interface Claim {id: string; key?: string; status?: string}

export interface Snapshot {
  setup: Setup;
  bots: Bot[];
  work: Work[];
  tasks: Task[];
  turns: Turn[];
  artifacts: Artifact[];
  claims: Claim[];
  projects: Project[];
  routines: Routine[];
  routine_runs: unknown[];
  notifications: Notification[];
  exchanges: Exchange[];
  attention_summary: AttentionSummary; // server projection — read verbatim
  devices: Device[];
  hosts: Host[];
  installations: Installation[];
  models: Model[];
  probes: Probe[];
}

export interface Message {
  id: string;
  bot_id?: string;
  work_id?: string;
  kind: string;
  from?: string;
  to?: string;
  content: string | Record<string, unknown>;
  created?: number;
  turn_id?: string;
  interim?: boolean;
  artifact_refs?: string[];
  exchange_id?: string;
  consumed_by_delivery_id?: string;
  consumed_by_turn_id?: string;
}
