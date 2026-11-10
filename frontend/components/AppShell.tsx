"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";
import {
  ArrowUpRight,
  ChartNoAxesCombined,
  CircleHelp,
  KeyRound,
  LockKeyhole,
  MessageSquareText,
  Radio,
  ShieldCheck,
  Sparkles,
  Unplug,
} from "lucide-react";
import { useSession } from "@/components/SessionProvider";

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const session = useSession();
  const [candidate, setCandidate] = useState("");
  async function connect(event: FormEvent) {
    event.preventDefault();
    await session.connect(candidate);
    setCandidate("");
  }
  const dashboard = pathname.startsWith("/dashboard");
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="FinBrain home">
          <span className="brand-symbol">
            <ChartNoAxesCombined size={25} />
          </span>
          <span>
            finbrain<span className="brand-dot">.</span>
          </span>
        </Link>
        <div className="workspace-label">
          PERSONAL WORKSPACE <LockKeyhole size={12} />
        </div>
        <nav aria-label="Main navigation" className="navigation">
          <Link
            href="/"
            aria-current={!dashboard ? "page" : undefined}
            className={!dashboard ? "nav-link active" : "nav-link"}
          >
            <MessageSquareText size={19} /> Chat assistant{" "}
            <span className="nav-shortcut">01</span>
          </Link>
          <Link
            href="/dashboard"
            aria-current={dashboard ? "page" : undefined}
            className={dashboard ? "nav-link active" : "nav-link"}
          >
            <ChartNoAxesCombined size={19} /> Trading dashboard{" "}
            <span className="nav-shortcut">02</span>
          </Link>
        </nav>
        <section className="sidebar-note">
          <span className="eyebrow">A LITTLE ROOM TO EXPERIMENT</span>
          <Sparkles size={23} />
          <h2>
            Your ideas.
            <br />
            Virtual capital.
          </h2>
          <p>
            Explore your finances and test market decisions in your own
            workspace.
          </p>
          <Link href="/dashboard">
            Open your portfolio <ArrowUpRight size={15} />
          </Link>
        </section>
        <div className="connection-panel">
          <div className="connection-heading">
            <span className={`status-dot ${session.key ? "online" : ""}`} />
            {session.key ? "Backend connected" : "Connect your backend"}
          </div>
          {session.key ? (
            <>
              <p>
                {session.status?.offline_mode
                  ? "Offline mode · Local inference"
                  : "Local API · Market access enabled"}
              </p>
              <button
                className="subtle-button"
                onClick={session.disconnect}
                disabled={session.busy || !!session.pending}
              >
                <Unplug size={13} /> Disconnect
              </button>
            </>
          ) : (
            <form onSubmit={connect}>
              <label className="sr-only" htmlFor="api-key">
                Backend API key
              </label>
              <div className="key-input">
                <KeyRound size={14} />
                <input
                  id="api-key"
                  type="password"
                  autoComplete="off"
                  placeholder="Paste your API key"
                  value={candidate}
                  onChange={(event) => setCandidate(event.target.value)}
                  required
                />
              </div>
              <button
                className="connect-button"
                disabled={session.connecting || !candidate.trim()}
              >
                {session.connecting ? "Connecting…" : "Connect securely"}
                <ArrowUpRight size={14} />
              </button>
              {session.connectionError && (
                <p className="connection-error" role="alert">
                  {session.connectionError}
                </p>
              )}
            </form>
          )}
          <p className="key-hint">
            <ShieldCheck size={12} /> Key stays in this tab’s memory
          </p>
        </div>
        <div className="sidebar-footer">
          <span className="avatar">P</span>
          <div>
            Personal account<small>Self-hosted workspace</small>
          </div>
          <CircleHelp size={17} />
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <span>/</span>
            <strong>
              {dashboard ? "Trading dashboard" : "Chat assistant"}
            </strong>
          </div>
          <div className="topbar-badges">
            <span className="paper-badge">PAPER TRADING</span>
            <span className="private-badge">
              <ShieldCheck size={14} /> Private workspace
            </span>
          </div>
        </header>
        <main id="main-content">{children}</main>
        <footer className="page-footer">
          <span>
            <LockKeyhole size={12} /> Self-hosted. Yours to control.
          </span>
          <span>
            <Radio size={12} />{" "}
            {session.key
              ? "Connected to your local API"
              : "Waiting for a backend connection"}
          </span>
        </footer>
      </div>
    </div>
  );
}
