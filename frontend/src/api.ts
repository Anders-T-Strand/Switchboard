// Typed wrappers around the Switchboard API.

export type Classification = {
  task_type: string;
  complexity: number;
  reason: string;
  source: "model" | "heuristic";
  model: string | null;
  ms: number;
};

export type RoutePlan = {
  mode: "auto" | "forced";
  chosen: string;
  routed: string;
  rule: string | null;
  classification: Classification | null;
  notes: string[];
  loaded: string[];
  chosen_label: string;
  routed_label: string;
};

export type RequestRow = {
  id: number;
  created_at: string;
  task_id: number | null;
  task_title: string | null;
  prompt_preview: string;
  status: "ok" | "error";
  error: string | null;
  route_mode: "auto" | "forced";
  task_type: string | null;
  complexity: number | null;
  classifier_source: string | null;
  rule: string | null;
  routed_model: string | null;
  routed_label: string | null;
  model_key: string | null;
  model_label: string | null;
  provider: "ollama" | "claude_cli" | null;
  fell_back: number;
  route_note: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  latency_ms: number | null;
  cost_usd: number;
  avoided_usd: number;
};

export type RequestDetail = Omit<RequestRow, "prompt_preview"> & {
  prompt: string;
  system_prompt: string | null;
  response: string | null;
  classifier_model: string | null;
  classifier_reason: string | null;
  classifier_ms: number | null;
  provider_model: string | null;
  load_ms: number | null;
  routing?: RoutePlan;
};

export type Task = {
  id: number;
  title: string;
  notes: string;
  status: TaskStatus;
  created_at: string;
  updated_at: string;
  requests: number;
  tokens: number;
  cost_usd: number;
  last_request_at: string | null;
};
export type TaskStatus = "todo" | "doing" | "done";

export type ModelStats = {
  model_key: string;
  label: string;
  provider: string | null;
  requests: number;
  errors: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  avoided_usd: number;
  median_latency_ms: number | null;
};

export type CcBucket = {
  messages: number;
  sessions: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
};

export type Summary = {
  days: number;
  since: string;
  requests: {
    total: number;
    ok: number;
    errors: number;
    fallbacks: number;
    local: number;
    claude: number;
    local_share: number | null;
  };
  tokens: { local_in: number; local_out: number; claude_in: number; claude_out: number };
  cost: { claude_api_equivalent_usd: number; avoided_usd: number };
  by_model: ModelStats[];
  by_task_type: { task_type: string; total: number; models: Record<string, number> }[];
  complexity: number[];
  claude_code: {
    interactive: CcBucket;
    router: CcBucket;
    by_model: { model: string; messages: number; output_tokens: number; cost_usd: number }[];
  };
};

export type Row = { date: string } & Record<string, number | string>;

export type Timeseries = {
  dates: string[];
  router_keys: string[];
  router_labels: Record<string, string>;
  router_tokens: Row[];
  router_requests: Row[];
  cc_keys: string[];
  cc_cost: Row[];
};

export type ModelStatus = {
  key: string;
  label: string;
  provider: "ollama" | "claude_cli";
  model: string;
  available: boolean;
  loaded: boolean | null;
};

export type Status = {
  ollama: { reachable: boolean; base_url: string; version: string | null; loaded: string[]; installed: string[] };
  claude: { found: boolean; path: string | null; version: string | null; use_subscription: boolean; logs_root: string };
  sync: SyncState;
  models: ModelStatus[];
};

export type SyncState = {
  at: string | null;
  ok: boolean | null;
  files: number;
  entries: number;
  reason: string | null;
  root: string | null;
};

export type Rule = {
  name: string;
  model: string;
  task_types: string[] | null;
  min_complexity: number | null;
  max_complexity: number | null;
};

export type ConfigView = {
  models: Record<string, { provider: string; model: string; label: string | null; think: boolean | null }>;
  routing: {
    classifier: { model: string; use_loaded_model: boolean; think: boolean | null; timeout_s: number; max_prompt_chars: number };
    prefer_loaded_local: boolean;
    fallback_order: string[];
    rules: Rule[];
  };
  pricing: {
    reference_model: string | null;
    models: Record<string, { match: string; input: number; output: number; cache_read: number; cache_write: number }>;
  };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep the status line
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const json = (body: unknown) => JSON.stringify(body);

export const api = {
  summary: (days: number) => request<Summary>(`/api/stats/summary?days=${days}`),
  timeseries: (days: number) => request<Timeseries>(`/api/stats/timeseries?days=${days}`),
  status: () => request<Status>("/api/status"),
  config: () => request<ConfigView>("/api/config"),
  sync: () => request<SyncState>("/api/claude-code/sync", { method: "POST" }),

  prompt: (body: { prompt: string; model: string; task_id: number | null; system_prompt?: string | null }) =>
    request<RequestDetail>("/api/prompt", { method: "POST", body: json(body) }),
  preview: (body: { prompt: string; model: string }) =>
    request<RoutePlan>("/api/route/preview", { method: "POST", body: json(body) }),

  requests: (q: { limit?: number; offset?: number; task_id?: number | null; model?: string; status?: string }) => {
    const params = new URLSearchParams();
    Object.entries(q).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    });
    return request<{ total: number; items: RequestRow[] }>(`/api/requests?${params}`);
  },
  request: (id: number) => request<RequestDetail>(`/api/requests/${id}`),

  tasks: () => request<Task[]>("/api/tasks"),
  createTask: (body: { title: string; notes?: string; status?: TaskStatus }) =>
    request<Task>("/api/tasks", { method: "POST", body: json(body) }),
  updateTask: (id: number, body: Partial<Pick<Task, "title" | "notes" | "status">>) =>
    request<Task>(`/api/tasks/${id}`, { method: "PATCH", body: json(body) }),
  deleteTask: (id: number) => request<void>(`/api/tasks/${id}`, { method: "DELETE" }),
};
