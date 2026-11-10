"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  api,
  ApiError,
  errorMessage,
  streamProgress,
  type ChatRequest,
  type ChatResponse,
  type Progress,
  type RoutingMode,
  type ServerStatus,
} from "@/services/api";

export interface Exchange {
  request: ChatRequest;
  progress?: Progress;
  response?: ChatResponse;
  error?: string;
  streamWarning?: string;
}
interface Session {
  key: string;
  status: ServerStatus | null;
  connecting: boolean;
  connectionError: string;
  connect: (key: string) => Promise<void>;
  disconnect: () => void;
  conversation: string;
  exchanges: Exchange[];
  pending: ChatRequest | null;
  busy: boolean;
  send: (message: string, mode: RoutingMode) => Promise<void>;
  resume: () => Promise<void>;
  newConversation: () => void;
}
const SessionContext = createContext<Session | null>(null);
export function useSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error("Session provider is missing");
  return session;
}
export function SessionProvider({ children }: { children: ReactNode }) {
  const [key, setKey] = useState("");
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [conversation, setConversation] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [pending, setPending] = useState<ChatRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const active = useRef(false);
  const unresolved = useRef<ChatRequest | null>(null);

  useEffect(() => {
    function protectUnresolvedRequest(event: BeforeUnloadEvent) {
      if (unresolved.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", protectUnresolvedRequest);
    return () =>
      window.removeEventListener("beforeunload", protectUnresolvedRequest);
  }, []);

  async function connect(candidate: string) {
    setConnecting(true);
    setConnectionError("");
    try {
      const server = await api<ServerStatus>("/api/v1/llm/status", candidate);
      setStatus(server);
      setKey(candidate);
      setConversation((current) => current || `web-${crypto.randomUUID()}`);
    } catch (error) {
      setConnectionError(errorMessage(error));
    } finally {
      setConnecting(false);
    }
  }
  function disconnect() {
    if (active.current || unresolved.current) return;
    setKey("");
    setStatus(null);
    setExchanges([]);
    setConversation("");
  }
  function patch(requestId: string, values: Partial<Exchange>) {
    setExchanges((items) =>
      items.map((item) =>
        item.request.request_id === requestId ? { ...item, ...values } : item,
      ),
    );
  }
  async function execute(body: ChatRequest) {
    if (active.current || !key) return;
    active.current = true;
    setBusy(true);
    setPending(body);
    unresolved.current = body;
    patch(body.request_id, { error: undefined, streamWarning: undefined });
    const observer = new AbortController();
    let observedResponse: ChatResponse | null = null;
    const progress = streamProgress(body, key, observer.signal, (snapshot) => {
      if (snapshot.response) observedResponse = snapshot.response;
      patch(body.request_id, {
        progress: snapshot,
        ...(snapshot.response ? { response: snapshot.response } : {}),
      });
    }).catch((error: unknown) => {
      if (!observer.signal.aborted)
        patch(body.request_id, { streamWarning: errorMessage(error) });
    });
    try {
      const result = await api<ChatResponse>("/api/v1/agent/chat", key, {
        method: "POST",
        body: JSON.stringify(body),
      });
      patch(body.request_id, {
        response: result,
        error: undefined,
        streamWarning: undefined,
      });
      setPending(null);
      unresolved.current = null;
    } catch (error) {
      if (observedResponse) {
        patch(body.request_id, {
          response: observedResponse,
          error: undefined,
        });
        setPending(null);
        unresolved.current = null;
      } else {
        patch(body.request_id, { error: errorMessage(error) });
        // A validated 4xx rejected this request before execution. Network/5xx
        // failures remain resumable with the identical UUID and routing options.
        if (
          error instanceof ApiError &&
          [400, 401, 403, 404, 422].includes(error.status)
        ) {
          setPending(null);
          unresolved.current = null;
        }
      }
    } finally {
      observer.abort();
      await progress;
      active.current = false;
      setBusy(false);
    }
  }
  async function send(message: string, mode: RoutingMode) {
    if (active.current || unresolved.current || !key || !message.trim()) return;
    const body: ChatRequest = {
      message: message.trim(),
      conversation_id: conversation,
      request_id: crypto.randomUUID(),
      routing_mode: mode,
    };
    setExchanges((items) => [...items, { request: body }]);
    await execute(body);
  }
  async function resume() {
    if (unresolved.current) await execute(unresolved.current);
  }
  function newConversation() {
    if (active.current || unresolved.current) return;
    setExchanges([]);
    setConversation(`web-${crypto.randomUUID()}`);
  }
  return (
    <SessionContext.Provider
      value={{
        key,
        status,
        connecting,
        connectionError,
        connect,
        disconnect,
        conversation,
        exchanges,
        pending,
        busy,
        send,
        resume,
        newConversation,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}
