export type RoutingMode = "auto" | "local_only" | "cloud_only";
export interface ToolStep {
  call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  started_at: string;
  completed_at: string | null;
  status: "pending" | "succeeded" | "error";
  observation: {
    ok: boolean;
    data?: { results?: Source[]; [key: string]: unknown };
    error?: string;
  } | null;
}
export interface Source {
  id: string;
  content: string;
  metadata: Record<string, unknown>;
}
export interface RoutingEvent {
  engine: string;
  provider: string;
  model: string;
  duration_ms: number;
  status: string;
  started_at: string;
}
export interface Route {
  engine: string;
  provider: string;
  model: string;
  reason: string;
}
export interface ChatRequest {
  message: string;
  conversation_id: string;
  request_id: string;
  routing_mode: RoutingMode;
}
export interface ChatResponse {
  conversation_id: string;
  request_id: string;
  status: "completed" | "failed" | "limit_reached";
  answer: string;
  steps: ToolStep[];
  started_at: string;
  completed_at: string;
  routing: Route | null;
  routing_events: RoutingEvent[];
  replayed: boolean;
}
export interface Progress {
  steps: ToolStep[];
  routing: Route | null;
  routing_events: RoutingEvent[];
  response: ChatResponse | null;
}
export interface ServerStatus {
  offline_mode: boolean;
  cloud_enabled: boolean;
  cloud_configured: boolean;
  public_tickers: string[];
  default_mode: RoutingMode;
}
export interface Holding {
  ticker: string;
  shares: string;
  buy_price: string;
  cost_basis: string;
  market_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  quote_as_of: string | null;
  quote_stale: boolean | null;
  quote_error: string | null;
}
export interface Portfolio {
  id: number;
  currency: string;
  cash_balance: string;
  equity: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  total_pnl: string | null;
  holdings_cost_basis: string;
  market_value: string | null;
  holdings: Holding[];
  ledger_coverage_complete: boolean;
  valuation_complete: boolean;
  snapshot_at: string;
  updated_at: string;
}
export interface Order {
  id: number;
  ticker: string;
  shares: string;
  fill_price: string;
  order_type: "BUY" | "SELL";
  timestamp: string;
  status: "OPEN" | "CLOSED";
  request_id: string | null;
  settled_notional: string | null;
  quote_as_of: string | null;
}
export interface OrderPage {
  currency: string;
  orders: Order[];
  next_before_id: number | null;
}
export interface Quote {
  ticker: string;
  price: string;
  currency: string;
  as_of: string;
  fetched_at: string;
  age_seconds: number;
  session_open: string;
  session_high: string;
  session_low: string;
  session_volume: number;
  source: string;
  price_basis: string;
}

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/$/, "");
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      typeof body?.detail === "string"
        ? body.detail
        : `API request failed (${response.status})`,
      response.status,
    );
  }
  return response;
}
export async function api<T>(
  path: string,
  key: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await checked(
    await fetch(`${API_BASE}${path}`, {
      ...options,
      cache: "no-store",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": key,
        ...options.headers,
      },
    }),
  );
  return response.json() as Promise<T>;
}

export async function streamProgress(
  body: ChatRequest,
  key: string,
  signal: AbortSignal,
  onProgress: (progress: Progress) => void,
): Promise<void> {
  const response = await checked(
    await fetch(
      `${API_BASE}/api/v1/agent/requests/${body.request_id}/events?conversation_id=${encodeURIComponent(body.conversation_id)}`,
      {
        headers: { "X-API-Key": key },
        signal,
        cache: "no-store",
        credentials: "omit",
      },
    ),
  );
  if (!response.body)
    throw new Error("Streaming is unavailable in this browser");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;
  function consume(frame: string) {
    const lines = frame.split("\n");
    const event = lines
      .find((line) => line.startsWith("event:"))
      ?.slice(6)
      .trim();
    const data = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    if (event === "stream_error")
      throw new Error((JSON.parse(data) as { detail: string }).detail);
    if (event === "completed") completed = true;
    if (event === "progress" || event === "completed")
      onProgress(JSON.parse(data) as Progress);
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        consume(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
      }
      if (done) {
        if (buffer.trim()) consume(buffer);
        break;
      }
    }
    if (!completed && !signal.aborted)
      throw new Error("Live progress ended before a completed checkpoint");
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function money(
  value: string | null | undefined,
  currency = "USD",
): string {
  if (value == null) return "—";
  const match = value.match(/^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/);
  if (!match) return "—";
  const negative = match[1] === "-";
  const fraction = match[3] || "";
  const scale = Number(match[4] || "0") + 2 - fraction.length;
  if (Math.abs(scale) > 1000 || match[2].length > 1000) return "—";
  let cents = BigInt(match[2] + fraction);
  if (scale >= 0) cents *= 10n ** BigInt(scale);
  else {
    const divisor = 10n ** BigInt(-scale);
    cents = (cents + divisor / 2n) / divisor;
  }
  return `${negative && cents !== 0n ? "−" : ""}${currency === "USD" ? "$" : `${currency} `}${new Intl.NumberFormat("en-US").format(cents / 100n)}.${(cents % 100n).toString().padStart(2, "0")}`;
}
export function quantity(value: string) {
  return value.includes(".")
    ? value.replace(/0+$/, "").replace(/\.$/, "")
    : value;
}
export function timestamp(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unavailable";
  // Date stores milliseconds. Retain the ledger's original fractional precision.
  const fraction = value.match(/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/)?.[1];
  return `${date.toISOString().slice(0, 19).replace("T", " ")}${fraction ? `.${fraction}` : ""}Z`;
}
export function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "The local API could not be reached";
}
