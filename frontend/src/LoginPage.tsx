import { type FormEvent, useState } from "react";
import { ApiError, login, type Session } from "./api/client";

type Props = { onLoggedIn: (session: Session) => void };

export function LoginPage({ onLoggedIn }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault(); // stay on the page; we submit with fetch instead
    setError(null);
    setSubmitting(true);
    try {
      onLoggedIn(await login(email, password));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login">
      <form className="card login-card" onSubmit={handleSubmit} noValidate>
        <h1 className="brand">Operations Copilot</h1>
        <p className="muted">Sign in to ask questions about the company data and documents you can access.</p>

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />

        {/* role="alert" makes screen readers announce the error when it appears */}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" disabled={submitting || !email || !password}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
        <p className="fine-print">Demo environment with synthetic data.</p>
      </form>
    </main>
  );
}
