import { useCallback, useEffect, useState } from "react";
import { currentSession, logout, type Session } from "./api/client";
import { HomePage } from "./HomePage";
import { LoginPage } from "./LoginPage";

/**
 * Top-level switch: checking → signed out (login page) → signed in (home page).
 * On load we ask the backend "who am I?" (/api/auth/me), so a page refresh keeps you signed in
 * for as long as the server-side session is valid.
 */
type State =
  | { status: "checking" }
  | { status: "signed-out"; notice?: string }
  | { status: "signed-in"; session: Session }
  | { status: "error"; message: string };

export function App() {
  const [state, setState] = useState<State>({ status: "checking" });

  useEffect(() => {
    currentSession()
      .then((session) => setState(session ? { status: "signed-in", session } : { status: "signed-out" }))
      .catch((e: unknown) => setState({ status: "error", message: e instanceof Error ? e.message : String(e) }));
  }, []);

  const handleSessionExpired = useCallback(
    () => setState({ status: "signed-out", notice: "Your session has ended. Please sign in again." }),
    [],
  );

  async function handleSignOut() {
    try {
      await logout();
    } finally {
      setState({ status: "signed-out" });
    }
  }

  switch (state.status) {
    case "checking":
      return <p className="centered muted">Loading…</p>;
    case "error":
      return (
        <p className="centered error" role="alert">
          {state.message}
        </p>
      );
    case "signed-out":
      return (
        <>
          {state.notice && (
            <p className="banner" role="status">
              {state.notice}
            </p>
          )}
          <LoginPage onLoggedIn={(session) => setState({ status: "signed-in", session })} />
        </>
      );
    case "signed-in":
      return <HomePage session={state.session} onSignOut={handleSignOut} onSessionExpired={handleSessionExpired} />;
  }
}
