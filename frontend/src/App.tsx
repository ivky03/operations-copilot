import { useEffect, useState } from "react";

type Check = { status: string; [key: string]: string };
type Health = { status: string; checks: Record<string, Check> };

// M0 placeholder: proves the browser → API → Postgres/Floci path works end to end.
export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/health")
      .then((r) => r.json() as Promise<Health>)
      .then(setHealth)
      .catch((e: unknown) => setError(String(e)));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "48px auto", padding: "0 16px" }}>
      <h1>Operations Copilot</h1>
      <p>Synthetic-data portfolio project. Skeleton build (M0).</p>
      <h2>System health</h2>
      {error && <p role="alert">Could not reach API: {error}</p>}
      {!health && !error && <p>Checking…</p>}
      {health && (
        <ul>
          <li>Overall: <strong>{health.status}</strong></li>
          {Object.entries(health.checks).map(([name, c]) => (
            <li key={name}>
              {name}: {c.status}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
