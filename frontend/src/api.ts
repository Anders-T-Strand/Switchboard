// Typed wrappers around the Switchboard API.

export type Effort = "low" | "medium" | "high" | "xhigh" | "max";

export type Classification = {
  task_type: string;
  complexity: number;
  reasoning: number;
  reason: string;
  source: "model" | "heuristic";
  model: string | null;
  ms: number;
};

export type Neighbor = {
  request_id: number;
  score: number;
  conversation_id: number | null;
  turn: number | null;
  title: string | null;
  prompt: string;
  model_key: string | null;
  effort: Effort | null;
  rating: number | null;
  created_at: string;
};

export type MemoryRef = {
  request_id: number;
  conversation_id: number;
  turn: number;
  title: string | null;
  score: number;
};

export type Learned = { from: string; to: string; direction: "up" | "down"; reason: string };

export type RoutePlan = {
  mode: "auto" | "forced";
  chosen: string;
  routed: string;
  rule: string | null;
  classification: Classification | null;
  notes: string[];
  loaded: string[];
  route_effort: Effort | null;
  effort_source: "forced" | "rule" | "reasoning" | null;
  effort: Effort | null;
  think: boolean | null;
  neighbors: Neighbor[];
  learned: Learned | null;
  chosen_label: string;
  routed_label: string;
};

export type RequestRow = {
  id: number;
  created_at: string;
  task_id: number | null;
  task_title: string | null;
  conversation_id: number | null;
  conversation_title: string | null;
  turn: number | null;
  rating: number | null;
  prompt_preview: string;
  status: "ok" | "error";
  error: string | null;
  route_mode: "auto" | "forced";
  task_type: string | null;
  complexity: number | null;
  reasoning: number | null;
  effort: Effort | null;
  effort_source: "forced" | "rule" | "reasoning" | null;
  think: number | null;
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
  context_turns: number | null;
  neighbors: Neighbor[];
  memory: MemoryRef[];
  routing?: RoutePlan;
};

export type ConversationSummary = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
  turns: number;
  memory: number;
  last_model: string | null;
  last_status: "ok" | "error" | null;
  match?: "meaning" | "words" | "both";
};

export type Conversation = {
  id: number;
  title: string;
  memory: number;
  created_at: string;
  updated_at: string;
  messages: RequestDetail[];
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
  efforts: Record<string, number>;
  thinking: number;
  think_known: number;
  rated_good: number;
  rated_bad: number;
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
  reasoning: number[];
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
  index: IndexStatus;
};

export type IndexStatus = {
  enabled: boolean;
  ok?: boolean | null;
  reason?: string | null;
  checked_at?: string | null;
  where?: string;
  collection?: string;
  embed_model?: string;
  dim?: number | null;
  turns?: number;
  indexed?: number;
  pending?: number;
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
  min_reasoning: number | null;
  max_reasoning: number | null;
  effort: Effort | null;
  min_effort: Effort | null;
  max_effort: Effort | null;
};

export type ConfigView = {
  models: Record<
    string,
    { provider: string; model: string; label: string | null; think: boolean | null; max_effort: Effort | null }
  >;
  routing: {
    classifier: { model: string; use_loaded_model: boolean; think: boolean | null; timeout_s: number; max_prompt_chars: number };
    prefer_loaded_local: boolean;
    fallback_order: string[];
    effort: { by_reasoning: Record<string, Effort> };
    learn: { enabled: boolean; ladder: string[]; neighbors: number; min_similarity: number; min_votes: number };
    rules: Rule[];
  };
  pricing: {
    reference_model: string | null;
    models: Record<string, { match: string; input: number; output: number; cache_read: number; cache_write: number }>;
  };
  efforts: Effort[];
  index: {
    enabled: boolean;
    qdrant: { url: string | null; path: string | null; collection: string };
    embedding: { model: string; max_chars: number; instructions: boolean };
    search_min_score: number;
  };
  memory: { max_items: number; min_score: number; max_chars: number };
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

  prompt: (body: {
    prompt: string;
    model: string;
    effort: string;
    task_id: number | null;
    conversation_id: number | null;
    memory?: boolean;
    system_prompt?: string | null;
  }) =>
    request<RequestDetail>("/api/prompt", { method: "POST", body: json(body) }),
  preview: (body: { prompt: string; model: string; effort: string; conversation_id: number | null }) =>
    request<RoutePlan>("/api/route/preview", { method: "POST", body: json(body) }),

  requests: (q: { limit?: number; offset?: number; task_id?: number | null; model?: string; status?: string }) => {
    const params = new URLSearchParams();
    Object.entries(q).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    });
    return request<{ total: number; items: RequestRow[] }>(`/api/requests?${params}`);
  },
  request: (id: number) => request<RequestDetail>(`/api/requests/${id}`),

  conversations: (q = "", limit = 100) =>
    request<{ total: number; items: ConversationSummary[]; search: "meaning and words" | "words" | null }>(
      `/api/conversations?${new URLSearchParams({ q, limit: String(limit) })}`,
    ),
  conversation: (id: number) => request<Conversation>(`/api/conversations/${id}`),
  renameConversation: (id: number, title: string) =>
    request<Conversation>(`/api/conversations/${id}`, { method: "PATCH", body: json({ title }) }),
  setMemory: (id: number, memory: boolean) =>
    request<Conversation>(`/api/conversations/${id}`, { method: "PATCH", body: json({ memory }) }),
  rebuildIndex: () => request<IndexStatus>("/api/index/rebuild", { method: "POST" }),
  deleteConversation: (id: number) => request<void>(`/api/conversations/${id}`, { method: "DELETE" }),
  rate: (id: number, rating: -1 | 0 | 1) =>
    request<RequestDetail>(`/api/requests/${id}/rating`, { method: "PUT", body: json({ rating }) }),

  tasks: () => request<Task[]>("/api/tasks"),
  createTask: (body: { title: string; notes?: string; status?: TaskStatus }) =>
    request<Task>("/api/tasks", { method: "POST", body: json(body) }),
  updateTask: (id: number, body: Partial<Pick<Task, "title" | "notes" | "status">>) =>
    request<Task>(`/api/tasks/${id}`, { method: "PATCH", body: json(body) }),
  deleteTask: (id: number) => request<void>(`/api/tasks/${id}`, { method: "DELETE" }),
};
