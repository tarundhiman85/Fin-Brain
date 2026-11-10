"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  CircleAlert,
  FileText,
  GitBranch,
  LoaderCircle,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Terminal,
  Wallet,
} from "lucide-react";
import { useSession, type Exchange } from "@/components/SessionProvider";
import {
  timestamp,
  type RoutingMode,
  type Source,
  type ToolStep,
} from "@/services/api";

function Sources({ steps }: { steps: ToolStep[] }) {
  const sources = new Map<string, Source>();
  for (const step of steps)
    if (
      step.status === "succeeded" &&
      step.observation?.ok &&
      step.tool === "search_financial_kb"
    ) {
      for (const source of step.observation.data?.results || [])
        sources.set(source.id, source);
    }
  if (!sources.size) return null;
  return (
    <details className="sources">
      <summary>
        <FileText size={15} /> Financial sources <span>{sources.size}</span>
        <ChevronDown size={14} />
      </summary>
      <div className="source-list">
        {[...sources.values()].map((source) => (
          <article key={source.id}>
            <div className="source-title">
              <FileText size={14} />
              <strong>
                {String(source.metadata.source || "Financial document")}
              </strong>
            </div>
            <code>Chunk {source.id}</code>
            <p>{source.content}</p>
            <details>
              <summary>Source metadata</summary>
              <pre>{JSON.stringify(source.metadata, null, 2)}</pre>
            </details>
          </article>
        ))}
      </div>
    </details>
  );
}
function Trace({ exchange }: { exchange: Exchange }) {
  const steps = exchange.response?.steps || exchange.progress?.steps || [];
  const events =
    exchange.response?.routing_events ||
    exchange.progress?.routing_events ||
    [];
  const route = exchange.response?.routing || exchange.progress?.routing;
  return (
    <>
      <details
        className="tool-trace"
        open={!exchange.response && !exchange.error}
      >
        <summary>
          <Terminal size={15} /> Tool activity{" "}
          <span className="count-badge">{steps.length}</span>
          <span className="trace-route">
            {route ? `${route.provider} / ${route.model}` : "Awaiting route"}
          </span>
          <ChevronDown size={14} />
        </summary>
        <div className="trace-content">
          {!steps.length && (
            <p className="muted">
              Waiting for the first saved tool checkpoint…
            </p>
          )}
          {steps.map((step) => {
            const duration = step.completed_at
              ? Math.max(
                  0,
                  new Date(step.completed_at).getTime() -
                    new Date(step.started_at).getTime(),
                )
              : null;
            return (
              <details className="tool-step" key={step.call_id}>
                <summary>
                  {step.status === "pending" ? (
                    <LoaderCircle className="spin" size={15} />
                  ) : step.status === "error" ? (
                    <CircleAlert size={15} />
                  ) : (
                    <Check size={15} />
                  )}
                  <code>{step.tool}</code>
                  <span>
                    {duration == null
                      ? "Running"
                      : `${duration.toLocaleString()} ms`}
                  </span>
                </summary>
                <div className="tool-step-body">
                  <small>Started {timestamp(step.started_at)}</small>
                  <h4>Arguments</h4>
                  <pre>{JSON.stringify(step.arguments, null, 2)}</pre>
                  <h4>Observation</h4>
                  <pre>{JSON.stringify(step.observation, null, 2)}</pre>
                </div>
              </details>
            );
          })}
          {events.length > 0 && (
            <details className="inference-events">
              <summary>Inference latency · {events.length} calls</summary>
              {events.map((event, index) => (
                <p key={`${event.started_at}-${index}`}>
                  <span>
                    {event.provider} · {event.model}
                  </span>
                  <span>
                    {Math.round(event.duration_ms).toLocaleString()} ms ·{" "}
                    {event.status}
                  </span>
                </p>
              ))}
            </details>
          )}
        </div>
      </details>
      <Sources steps={steps} />
    </>
  );
}
const suggestions = [
  {
    icon: Search,
    label: "Understand my spending",
    text: "Find my recent dining transactions and summarize the evidence. Cite the financial source chunks.",
  },
  {
    icon: Wallet,
    label: "Review my paper portfolio",
    text: "Check my virtual cash balance and current paper holdings. Do not execute any trades.",
  },
  {
    icon: GitBranch,
    label: "Explore a market idea",
    text: "Compare AAPL and MSFT market risks. Do not execute any trades.",
  },
];
export default function ChatWindow() {
  const session = useSession();
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<RoutingMode>("auto");
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const [follow, setFollow] = useState(true);
  useEffect(() => {
    if (follow)
      end.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [session.exchanges, follow]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || !session.key || session.busy || session.pending)
      return;
    const message = draft;
    setDraft("");
    setFollow(true);
    await session.send(message, mode);
  }
  const cloudUnavailable =
    !session.status?.cloud_enabled ||
    !session.status?.cloud_configured ||
    session.status?.offline_mode;
  return (
    <div className="chat-page">
      <div className="section-heading">
        <div>
          <span className="eyebrow">YOUR FINANCIAL COPILOT</span>
          <h1>Chat assistant</h1>
        </div>
        <button
          className="secondary-button"
          disabled={session.busy || !!session.pending}
          onClick={session.newConversation}
        >
          <Plus size={16} /> New conversation
        </button>
      </div>
      <div className="chat-layout">
        <section className="chat-card" aria-label="FinBrain conversation">
          <header className="chat-card-header">
            <span className="assistant-mark">
              <Sparkles size={18} />
            </span>
            <div>
              <strong>FinBrain</strong>
              <small>Your private finance & market assistant</small>
            </div>
            <span className="local-label">
              <span className={`status-dot ${session.key ? "online" : ""}`} />
              {session.key ? "Ready" : "Connect to start"}
            </span>
          </header>
          <div
            className="message-feed"
            role="log"
            aria-label="Conversation messages"
            onScroll={(event) => {
              const target = event.currentTarget;
              setFollow(
                target.scrollHeight - target.scrollTop - target.clientHeight <
                  150,
              );
            }}
          >
            {!session.exchanges.length ? (
              <div className="chat-welcome">
                <div className="welcome-orbit">
                  <Sparkles size={31} />
                </div>
                <span className="eyebrow">A CLEARER PICTURE STARTS HERE</span>
                <h2>Make sense of your money.</h2>
                <p>
                  Ask about your expenses, explore an investment idea,
                  <br className="desktop-break" /> or put a paper trading
                  strategy to the test.
                </p>
                <div className="suggestion-grid">
                  {suggestions.map(({ icon: Icon, label, text }) => (
                    <button
                      key={label}
                      onClick={() => {
                        setDraft(text);
                        input.current?.focus();
                      }}
                    >
                      <Icon size={19} />
                      <span>{label}</span>
                      <ArrowUp size={15} />
                    </button>
                  ))}
                </div>
                <div className="welcome-privacy">
                  <ShieldCheck size={14} /> Personal financial context stays on
                  your local route.
                </div>
              </div>
            ) : (
              session.exchanges.map((exchange) => (
                <div className="exchange" key={exchange.request.request_id}>
                  <div className="user-message">
                    <span className="message-avatar user-avatar">Y</span>
                    <div>
                      <div className="message-author">
                        You{" "}
                        <small>
                          {exchange.request.routing_mode.replaceAll("_", " ")}
                        </small>
                      </div>
                      <p>{exchange.request.message}</p>
                    </div>
                  </div>
                  <div className="assistant-message">
                    <span className="message-avatar">
                      <Sparkles size={16} />
                    </span>
                    <div className="assistant-body">
                      <div className="message-author">
                        FinBrain{" "}
                        <small>
                          {exchange.response?.status ||
                            (exchange.error
                              ? "Request interrupted"
                              : "Working")}
                        </small>
                      </div>
                      {exchange.response ? (
                        <div className="markdown">
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                            skipHtml
                            components={{
                              img: () => null,
                              a: ({ children }) => (
                                <span className="citation-link">
                                  {children}
                                </span>
                              ),
                            }}
                          >
                            {exchange.response.answer}
                          </ReactMarkdown>
                        </div>
                      ) : (
                        !exchange.error && (
                          <p className="thinking">
                            <LoaderCircle className="spin" size={14} /> Reading
                            the available context and tool observations…
                          </p>
                        )
                      )}
                      {exchange.error && (
                        <p className="error-banner" role="alert">
                          {exchange.error}
                        </p>
                      )}
                      {exchange.response &&
                        exchange.response.status !== "completed" && (
                          <p className="warning-banner">
                            This run is{" "}
                            {exchange.response.status.replaceAll("_", " ")}.
                            Review its observations and confirmed receipts.
                          </p>
                        )}
                      {exchange.streamWarning && (
                        <p className="warning-banner">
                          Live progress interrupted. The final chat response
                          will still provide the saved trace.
                        </p>
                      )}
                      <Trace exchange={exchange} />
                      <details className="request-details">
                        <summary>Request details</summary>
                        <p>
                          Conversation{" "}
                          <code>{exchange.request.conversation_id}</code>
                        </p>
                        <p>
                          Request <code>{exchange.request.request_id}</code>
                        </p>
                        {exchange.response && (
                          <p>
                            Completed{" "}
                            {timestamp(exchange.response.completed_at)}
                            {exchange.response.replayed
                              ? " · Saved result replayed"
                              : ""}
                          </p>
                        )}
                      </details>
                    </div>
                  </div>
                </div>
              ))
            )}
            <div ref={end} />
          </div>
          {!follow && session.exchanges.length > 0 && (
            <button
              className="jump-button"
              onClick={() => {
                setFollow(true);
                end.current?.scrollIntoView({ behavior: "smooth" });
              }}
            >
              <ArrowDown size={14} /> Latest activity
            </button>
          )}
          <div className="composer-area">
            {session.pending && !session.busy && (
              <div className="warning-banner">
                Execution status needs recovery. Resume the same request to
                retrieve its saved result.
                <button className="secondary-button" onClick={session.resume}>
                  Resume request
                </button>
              </div>
            )}
            <form onSubmit={submit}>
              <label className="sr-only" htmlFor="message">
                Ask FinBrain
              </label>
              <div className="composer">
                <textarea
                  ref={input}
                  id="message"
                  rows={2}
                  maxLength={8000}
                  placeholder={
                    session.key
                      ? "Ask about your finances or a paper trading idea…"
                      : "Connect your backend in the sidebar to start…"
                  }
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  disabled={session.busy || !!session.pending}
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
                <div className="composer-bottom">
                  <div
                    className="route-switch"
                    role="group"
                    aria-label="LLM routing mode"
                  >
                    {(
                      [
                        ["auto", "Auto"],
                        ["local_only", "Local only"],
                        ["cloud_only", "Cloud only"],
                      ] as const
                    ).map(([value, label]) => (
                      <button
                        type="button"
                        key={value}
                        aria-pressed={mode === value}
                        className={mode === value ? "selected" : ""}
                        disabled={
                          session.busy ||
                          !!session.pending ||
                          (value === "cloud_only" && cloudUnavailable)
                        }
                        onClick={() => setMode(value)}
                      >
                        {value === "auto" && <Sparkles size={12} />}
                        {label}
                      </button>
                    ))}
                  </div>
                  <button
                    className="send-button"
                    type="submit"
                    aria-label="Send message"
                    disabled={
                      !session.key ||
                      !draft.trim() ||
                      session.busy ||
                      !!session.pending
                    }
                  >
                    {session.busy ? (
                      <LoaderCircle className="spin" size={18} />
                    ) : (
                      <ArrowUp size={20} />
                    )}
                  </button>
                </div>
              </div>
            </form>
            <p className="composer-note">
              <ShieldCheck size={12} />{" "}
              {mode === "cloud_only"
                ? "Cloud access is restricted to approved public-market context."
                : "Private finance stays local. Paper orders use virtual money."}
              <span>{draft.length}/8,000</span>
            </p>
          </div>
        </section>
        <aside className="context-rail">
          <section>
            <span className="eyebrow">BUILT AROUND YOUR PRIVACY</span>
            <h3>
              A workspace
              <br />
              you can trust.
            </h3>
            <div className="rail-feature">
              <ShieldCheck size={18} />
              <div>
                <strong>Local-first intelligence</strong>
                <p>
                  Sensitive statements and account context stay with your local
                  model.
                </p>
              </div>
            </div>
            <div className="rail-feature">
              <FileText size={18} />
              <div>
                <strong>Answers with evidence</strong>
                <p>
                  Open financial sources to see exactly what the assistant
                  retrieved.
                </p>
              </div>
            </div>
            <div className="rail-feature">
              <GitBranch size={18} />
              <div>
                <strong>Every tool call, visible</strong>
                <p>
                  Follow execution checkpoints, observations and model latency.
                </p>
              </div>
            </div>
          </section>
          <div className="rail-tip">
            <span className="eyebrow">TRY ASKING</span>
            <p>
              “Check Apple’s price, verify my virtual cash, and buy 10 shares if
              I have enough.”
            </p>
            <button
              onClick={() => {
                setDraft(
                  "Check Apple stock price, verify my virtual cash balance, and buy 10 shares if I have enough paper money.",
                );
                input.current?.focus();
              }}
            >
              Use this prompt <ArrowUp size={14} />
            </button>
            <small>
              Review the draft before sending. Sending authorizes a simulated
              order.
            </small>
          </div>
        </aside>
      </div>
    </div>
  );
}
