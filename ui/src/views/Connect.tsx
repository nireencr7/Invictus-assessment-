import { useState } from "react";
import { devSession, login, whoAmI, type Session } from "../data/api";
import { parseTokens } from "../lib/tokens";

// Dev credentials only exist on a local API (DEV_MODE=true); don't offer them anywhere else.
const LOCAL = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

/**
 * Staff sign in with their email and password. Static shop tokens still work, folded away under
 * "Other ways to sign in", for scripts, tools and the reviewers' demo.
 */
export function Connect({ apiBase, onConnected, onUseSnapshot, reason }: {
  apiBase: string;
  onConnected: (s: Session) => void;
  onUseSnapshot: (() => void) | null;
  reason?: string;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tokens, setTokens] = useState("");
  const [admin, setAdmin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const attempt = async (work: () => Promise<Session>) => {
    setBusy(true);
    setError(null);
    try {
      onConnected(await work());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const signIn = () =>
    attempt(async () => {
      if (!email.trim() || !password) throw new Error("Enter your email and password.");
      return login(apiBase, email.trim(), password, admin.trim() || undefined);
    });

  const signInWithTokens = () =>
    attempt(async () => {
      const list = parseTokens(tokens);
      if (!list.length) throw new Error("Paste at least one staff token.");
      const shops = [];
      for (const token of list) shops.push({ ...(await whoAmI(apiBase, token)), token });
      return { apiBase, shops, adminToken: admin.trim() || undefined };
    });

  const useDev = async () => {
    setBusy(true);
    setError(null);
    try {
      onConnected(await devSession(apiBase));
    } catch (e) {
      setError(`Dev credentials aren't available (${e instanceof Error ? e.message : String(e)}). They exist only when the API runs with DEV_MODE=true.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="connect">
      <form className="card" onSubmit={(e) => { e.preventDefault(); void signIn(); }}>
        <div style={{ display: "grid", gap: 4 }}>
          <span className="mono" style={{ fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--accent)" }}>
            Riverbend Cycles
          </span>
          <h1>Work order console</h1>
          <p className="muted" style={{ margin: 0 }}>Sign in with your work email and password.</p>
        </div>
        {reason ? <div className="banner">{reason}</div> : null}
        <label className="field">
          Email
          <input id="connect-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                 autoComplete="username" autoFocus required />
        </label>
        <label className="field">
          Password
          <input id="connect-password" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                 autoComplete="current-password" required />
        </label>
        {error ? <div className="banner bad" role="alert">{error}</div> : null}
        <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          Forgotten your password, or need access to another shop? Ask your manager: accounts are set up by an admin.
          {LOCAL ? <> Local dev accounts: <code>manager@riverbend.test</code> (all shops) or <code>downtown@riverbend.test</code>, password <code>riverbend-dev</code>.</> : null}
        </p>

        <details className="other-signin">
          <summary>Other ways to sign in</summary>
          <div className="stack">
            <label className="field">
              Staff token(s)
              <textarea id="connect-tokens" className="input mono" value={tokens} onChange={(e) => setTokens(e.target.value)}
                        placeholder={"one per line, or the riverbend-staff-tokens secret\nas gcloud prints it"}
                        autoComplete="off" spellCheck={false} />
            </label>
            <button type="button" className="btn" disabled={busy} onClick={() => void signInWithTokens()}>Sign in with tokens</button>
            <label className="field">
              Admin token (optional, for changing spend caps; used with either sign-in)
              <input id="connect-admin" className="input mono" type="password" value={admin} onChange={(e) => setAdmin(e.target.value)} autoComplete="off" />
            </label>
          </div>
        </details>

        {LOCAL || onUseSnapshot ? <div className="divider">or</div> : null}
        <div className="row">
          {LOCAL ? (
            <button type="button" className="btn" disabled={busy} onClick={() => void useDev()}>Use local dev credentials</button>
          ) : null}
          {onUseSnapshot ? <button type="button" className="btn" onClick={onUseSnapshot}>View the demo snapshot</button> : null}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          API: <code>{apiBase || "this origin (proxied)"}</code>. You stay signed in until you sign out, close this tab, or the session expires.
        </p>
      </form>
    </div>
  );
}
