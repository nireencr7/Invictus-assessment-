import { useState } from "react";
import { changePassword, type Session } from "../data/api";

const MIN_PASSWORD = 10; // same rule as the API

/** Change your own password. The API signs out your other sessions; this one stays. */
export function ChangePassword({ session, onClose, onDone }: {
  session: Session;
  onClose: () => void;
  onDone: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    if (next.length < MIN_PASSWORD) return setError(`The new password needs at least ${MIN_PASSWORD} characters.`);
    if (next !== confirm) return setError("The two new passwords don't match.");
    setBusy(true);
    try {
      await changePassword(session, current, next);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <form className="card modal" role="dialog" aria-modal="true" aria-labelledby="change-password-title"
            onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <h2 id="change-password-title">Change password</h2>
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          Signed in as <b>{session.account?.email}</b>. Any other place you're signed in will be signed out.
        </p>
        <label className="field">
          Current password
          <input className="input" type="password" value={current} onChange={(e) => setCurrent(e.target.value)}
                 autoComplete="current-password" autoFocus required />
        </label>
        <label className="field">
          New password (at least {MIN_PASSWORD} characters)
          <input className="input" type="password" value={next} onChange={(e) => setNext(e.target.value)}
                 autoComplete="new-password" required />
        </label>
        <label className="field">
          New password again
          <input className="input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
                 autoComplete="new-password" required />
        </label>
        {error ? <div className="banner bad" role="alert">{error}</div> : null}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy}>{busy ? "Saving…" : "Change password"}</button>
        </div>
      </form>
    </>
  );
}
