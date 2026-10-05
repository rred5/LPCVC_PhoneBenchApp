import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api.js";
import { getSession, initAuth, onSessionChange, signIn, signOut } from "./auth.js";
import Jobs from "./Jobs.jsx";
import NewJob from "./NewJob.jsx";
import PhoneStatus from "./PhoneStatus.jsx";

export default function App() {
  const [config, setConfig] = useState(null);
  const [session, setSession] = useState(undefined);
  const [me, setMe] = useState(null);
  const [problem, setProblem] = useState(null);
  const [jobsVersion, setJobsVersion] = useState(0);

  useEffect(() => {
    let stop = () => {};
    initAuth()
      .then(async (cfg) => {
        setConfig(cfg);
        setSession(cfg.cloud ? await getSession() : null);
        stop = onSessionChange(setSession);
      })
      .catch((error) => setProblem(error.message));
    return () => stop();
  }, []);

  const signedIn = config && (!config.cloud || session);
  useEffect(() => {
    if (!signedIn) {
      setMe(null);
      return;
    }
    api("/api/v1/me")
      .then((who) => {
        setMe(who);
        setProblem(null);
      })
      .catch((error) => setProblem(error instanceof ApiError && error.code === "not_a_member" ? error.message : `Could not load your account: ${error.message}`));
  }, [signedIn, session?.user?.id]);

  const submitted = useCallback(() => setJobsVersion((version) => version + 1), []);

  if (!config || session === undefined) {
    return <Shell>{problem ? <p className="error">{problem}</p> : <p className="muted">Loading…</p>}</Shell>;
  }
  if (config.cloud && !session) {
    return (
      <Shell>
        <section className="card signin">
          <h2>Sign in</h2>
          <p>PhoneBench runs vision-language model benchmarks on the team's Snapdragon phone. Sign in with the Google account on the member list.</p>
          <button className="primary" onClick={() => signIn().catch((error) => setProblem(error.message))}>
            Sign in with Google
          </button>
          {problem && <p className="error">{problem}</p>}
        </section>
      </Shell>
    );
  }
  if (!me) {
    return (
      <Shell user={session?.user?.email} onSignOut={config.cloud ? signOut : null}>
        {problem ? <p className="error">{problem}</p> : <p className="muted">Loading…</p>}
      </Shell>
    );
  }
  return (
    <Shell user={me.email} onSignOut={config.cloud ? signOut : null} phone={<PhoneStatus />}>
      <NewJob onSubmitted={submitted} />
      <Jobs me={me} version={jobsVersion} />
    </Shell>
  );
}

function Shell({ user, onSignOut, phone, children }) {
  return (
    <>
      <header className="topbar">
        <h1>
          <img src="/favicon.svg" alt="" width="22" height="22" /> LPCVC PhoneBench
        </h1>
        <div className="topbar-right">
          {phone}
          {user && <span className="muted user">{user}</span>}
          {onSignOut && (
            <button className="link" onClick={onSignOut}>
              Sign out
            </button>
          )}
        </div>
      </header>
      <main>{children}</main>
    </>
  );
}
