export type SessionStatus = 'running' | 'success' | 'failed' | 'max_turns' | 'max_spend';
export type ApprovalDecision = 'accept' | 'reject';
export type PermissionTier = 'none' | 'theorem_translation' | 'stepwise';

export interface PendingApproval {
  type: 'approval_requested';
  approval_id: string;
  tier: 'theorem_translation';
  candidate: number;
  lean_code: string;
  theorem_name?: string | null;
  check_result?: string | null;
  session_id?: string;
  run_id?: string;
}

export interface ApprovalEvent extends PendingApproval {
  id: string;
  session_id?: string;
  run_id?: string;
  decision?: 'accept' | 'reject' | string | null;
  feedback?: string | null;
  resolved_at?: string | null;
}

export interface SessionSummary {
  id: string;
  project_id?: string | null;
  project_slug?: string | null;
  project_title?: string | null;
  project_path?: string | null;
  title: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  message_count: number;
  run_count: number;
  models: string[];
  primary_model?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_seconds: number;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  run_id?: string | null;
  role: 'user' | 'assistant' | 'system';
  content: string;
  kind?: 'assistant' | 'chat' | string;
  created_at: string;
  is_live_terminal_summary?: boolean;
  live_started_after_assistant_steps?: number;
  live_started_after_code_steps?: number;
}

export interface CodeStep {
  id: string;
  session_id: string;
  run_id: string;
  step_number: number;
  path: string;
  code: string;
  kind?: 'code' | 'no_code';
  summary?: string | null;
  turn?: number | null;
  used_project_formalizations?: ProjectFormalizationUse[];
  used_by_project_formalizations?: ProjectFormalizationUse[];
  created_at: string;
}

export interface StatusEvent {
  id: string;
  session_id?: string;
  run_id?: string;
  step_number?: number | null;
  status?: string | null;
  message: string;
  created_at: string;
}

export interface UsageUpdatedEvent {
  session_id: string;
  run_id: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  delta_input_tokens: number;
  delta_output_tokens: number;
  delta_cost_usd: number;
}

export interface UsageBreakdownRow {
  id: string;
  session_id?: string;
  run_id?: string;
  run_number: number;
  ordinal: number;
  phase: 'theorem_translation' | 'proof_turn' | 'unattributed' | string;
  label: string;
  turn?: number | null;
  candidate?: number | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  event_count: number;
  created_at?: string;
}

export interface SessionDetail extends SessionSummary {
  messages: ChatMessage[];
  code_steps: CodeStep[];
  status_events: StatusEvent[];
  approval_events: ApprovalEvent[];
  usage_breakdown: UsageBreakdownRow[];
  active_run?: {
    id: string;
    status: string;
    pending_approval?: PendingApproval | null;
  } | null;
  project?: Project | null;
  project_theorem?: ProjectTheoremEntry | null;
}

export interface Project {
  id: string;
  slug: string;
  title: string;
  path: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectTheoremEntry {
  name: string;
  proof_path: string;
  module_name?: string | null;
}

export interface ProjectFormalizationUse extends ProjectTheoremEntry {
  project_id?: string | null;
  project_slug?: string | null;
  project_title?: string | null;
  project_path?: string | null;
}

export interface ProjectUnassignmentMove {
  from_path: string;
  to_path: string;
  from_module?: string | null;
  to_module?: string | null;
}

export interface ProjectUnassignmentCheck {
  status: 'safe';
  theorem: ProjectTheoremEntry;
  planned_move: ProjectUnassignmentMove;
}

export interface ProjectUnassignmentResult {
  status: 'unassigned';
  theorem: ProjectTheoremEntry;
  move: ProjectUnassignmentMove;
  code_step?: CodeStep | null;
  affected_session_ids?: string[];
}

export interface ProjectAssignmentMove {
  from_path: string;
  to_path: string;
  from_module?: string | null;
  to_module?: string | null;
}

export interface ProjectAssignmentCheck {
  status: 'safe';
  theorem: ProjectTheoremEntry;
  planned_move: ProjectAssignmentMove;
  entry_action: string;
}

export interface ProjectAssignmentResult {
  status: 'assigned';
  theorem: ProjectTheoremEntry;
  planned_move: ProjectAssignmentMove;
  entry_action: string;
  code_step: CodeStep;
}

export interface UsageSessionSummary extends SessionSummary {}

export interface UsageGlobalStats {
  session_count: number;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  average_tokens_per_session: number;
  average_cost_per_session: number;
  average_messages_per_session: number;
}

export interface UsageDailyPoint {
  day: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  run_count: number;
  session_count: number;
}

export interface UsageModelStats {
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  run_count: number;
  session_count: number;
}

export interface UsageStats {
  sessions: UsageSessionSummary[];
  global: UsageGlobalStats;
  daily: UsageDailyPoint[];
  models: UsageModelStats[];
}

export interface ModelOption {
  value: string;
  label: string;
  family: 'openai' | 'anthropic' | 'google' | string;
}

export interface ApiKeyStatus {
  configured: boolean;
  last4?: string | null;
}

export interface AppSettings {
  model: string;
  permission_tier: PermissionTier;
  theorem_translation_max_retries: number;
  max_turns?: number | null;
  max_spend_usd?: number | null;
  current_spend_usd: number;
  api_keys: Record<'openai' | 'anthropic' | 'google', ApiKeyStatus>;
  model_options: ModelOption[];
  permission_tiers: { value: PermissionTier; label: string }[];
}

export interface SettingsUpdate {
  model?: string;
  permission_tier?: PermissionTier;
  theorem_translation_max_retries?: number;
  max_turns?: number | null;
  max_spend_usd?: number | null;
  api_keys?: Partial<Record<'openai' | 'anthropic' | 'google', { value?: string; clear?: boolean }>>;
}

export async function listSessions(): Promise<SessionSummary[]> {
  const response = await fetch('/api/sessions');
  if (!response.ok) {
    throw new Error(`Failed to load sessions: ${response.statusText}`);
  }
  const data = await response.json();
  return data.sessions;
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(`/api/sessions/${sessionId}`);
  if (!response.ok) {
    throw new Error(`Failed to load session: ${response.statusText}`);
  }
  return response.json();
}

export async function getUsageStats(): Promise<UsageStats> {
  const response = await fetch('/api/stats');
  if (!response.ok) {
    throw new Error(`Failed to load statistics: ${response.statusText}`);
  }
  return response.json();
}

export async function listProjects(): Promise<Project[]> {
  const response = await fetch('/api/projects');
  if (!response.ok) {
    throw new Error(`Failed to load projects: ${response.statusText}`);
  }
  const data = await response.json();
  return data.projects;
}

export async function createProject(input: { slug?: string; title?: string; path?: string }): Promise<Project> {
  const response = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Failed to create project: ${response.statusText}`);
  }
  return response.json();
}

export async function checkProjectTheoremUnassignment(
  projectId: string,
  theoremName: string,
): Promise<ProjectUnassignmentCheck> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/theorems/${encodeURIComponent(theoremName)}/unassignment-check`,
    { method: 'POST' },
  );
  if (!response.ok) {
    throw new Error(await errorMessage(response, `Failed to check project unassignment: ${response.statusText}`));
  }
  return response.json();
}

export async function unassignProjectTheorem(
  projectId: string,
  theoremName: string,
): Promise<ProjectUnassignmentResult> {
  const response = await fetch(
    `/api/projects/${encodeURIComponent(projectId)}/theorems/${encodeURIComponent(theoremName)}/unassign`,
    { method: 'POST' },
  );
  if (!response.ok) {
    throw new Error(await errorMessage(response, `Failed to unassign project theorem: ${response.statusText}`));
  }
  return response.json();
}

export async function checkProjectAssignment(
  sessionId: string,
  projectId: string,
): Promise<ProjectAssignmentCheck> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/project-assignment-check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId }),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response, `Failed to check project assignment: ${response.statusText}`));
  }
  return response.json();
}

export async function assignProject(
  sessionId: string,
  projectId: string,
): Promise<ProjectAssignmentResult> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/assign-project`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId }),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response, `Failed to assign project: ${response.statusText}`));
  }
  return response.json();
}

export async function getSettings(): Promise<AppSettings> {
  const response = await fetch('/api/settings');
  if (!response.ok) {
    throw new Error(`Failed to load settings: ${response.statusText}`);
  }
  const settings = await response.json();
  return validateSettingsPayload(settings);
}

export async function saveSettings(update: SettingsUpdate): Promise<AppSettings> {
  const response = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const message =
      typeof detail.detail === 'string'
        ? detail.detail
        : detail.detail?.message || `Failed to save settings: ${response.statusText}`;
    const error = new Error(message);
    if (detail.detail?.field) {
      (error as Error & { field?: string }).field = detail.detail.field;
    }
    throw error;
  }
  const settings = await response.json();
  return validateSettingsPayload(settings);
}

export async function createRun(message: string, sessionId?: string, projectId?: string): Promise<{
  session_id: string;
  run_id: string;
  message: ChatMessage;
}> {
  const response = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId, project_id: projectId }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Failed to create run: ${response.statusText}`);
  }
  return response.json();
}

export async function submitApproval(
  runId: string,
  approvalId: string,
  decision: ApprovalDecision,
  feedback?: string,
): Promise<void> {
  const body: { decision: ApprovalDecision; feedback?: string } = { decision };
  if (feedback !== undefined) {
    body.feedback = feedback;
  }
  const response = await fetch(`/api/runs/${runId}/approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Failed to submit approval: ${response.statusText}`);
  }
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  const detail = await response.json().catch(() => ({}));
  if (typeof detail.detail === 'string') {
    return detail.detail;
  }
  if (detail.detail?.message) {
    return detail.detail.message;
  }
  return fallback;
}

function validateSettingsPayload(settings: AppSettings): AppSettings {
  if (!Number.isInteger(settings.theorem_translation_max_retries) || settings.theorem_translation_max_retries < 1) {
    throw new Error(
      'Settings API did not return theorem translation retry configuration. Restart the local backend and try again.',
    );
  }
  return settings;
}
