import { useState } from "react";
import { devSession, whoAmI, type Session } from "../data/api";
import { parseTokens } from "../lib/tokens";

// Dev credentials only exist on a local API (DEV_MODE=true); don't offer them anywhere else.
const LOCAL = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

/** Sign in with one or more staff tokens; each token only ever opens its own shop. */
export function Connect({ apiBase, onConnected, onUseSnapshot, reason }: {
  apiBase: string;
  onConnected: (s: Session) => void;
  onUseSnapshot: (() => void) | null;
  reason?: string;
}) {
  const [tokens, setTokens] = useState("");
  const [admin, setAdmin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const signIn = async () => {
    setBusy(true);
    setError(null);
    try {
      const list = parseTokens(tokens);
      if (!list.length) throw new Error("Paste at least one staff token.");
      const shops = [];
      for (const token of list) shops.push({ ...(await whoAmI(apiBase, token)), token });
      onConnected({ apiBase, shops, adminToken: admin.trim() || undefined });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

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
          <p className="muted" style={{ margin: 0 }}>
            Sign in with your shop's staff token. A token opens one shop only; paste several to see more than one.
          </p>
        </div>
        {reason ? <div className="banner">{reason}</div> : null}
        <label className="field">
          Staff token(s)
          <textarea id="connect-tokens" className="input mono" value={tokens} onChange={(e) => setTokens(e.target.value)}
                    placeholder={"one per line, or the riverbend-staff-tokens secret\nas gcloud prints it"}
                    autoComplete="off" spellCheck={false} />
        </label>
        <label className="field">
          Admin token (optional, for changing spend caps)
          <input id="connect-admin" className="input mono" type="password" value={admin} onChange={(e) => setAdmin(e.target.value)} autoComplete="off" />
        </label>
        {error ? <div className="banner bad">{error}</div> : null}
        <button className="btn primary" disabled={busy}>Sign in</button>
        <div className="divider">or</div>
        <div className="row">
          {LOCAL ? (
            <button type="button" className="btn" disabled={busy} onClick={() => void useDev()}>Use local dev credentials</button>
          ) : null}
          {onUseSnapshot ? <button type="button" className="btn" onClick={onUseSnapshot}>View the demo snapshot</button> : null}
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>
          API: <code>{apiBase || "this origin (proxied)"}</code>. Tokens stay in this tab only.
        </p>
      </form>
    </div>
  );
}
