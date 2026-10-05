// Sign-in through Supabase Auth (Google). The server says which Supabase
// project to use at /api/v1/auth-config; a local server has no sign-in.
import { createClient } from "@supabase/supabase-js";

let supabase = null;

export async function initAuth() {
  const response = await fetch("/api/v1/auth-config");
  if (!response.ok) throw new Error(`The server is not answering (HTTP ${response.status})`);
  const config = await response.json();
  if (config.cloud) {
    if (!config.supabase_url || !config.supabase_key) {
      throw new Error("The server is missing SUPABASE_PUBLISHABLE_KEY, so nobody can sign in");
    }
    supabase = createClient(config.supabase_url, config.supabase_key, {
      auth: { flowType: "pkce", persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
  }
  return config;
}

export async function getSession() {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  if (data.session && new URLSearchParams(window.location.search).has("code")) {
    // Drop the one-time sign-in code from the address bar.
    window.history.replaceState(null, "", window.location.pathname);
  }
  return data.session;
}

export async function getToken() {
  const session = await getSession();
  return session?.access_token ?? null;
}

export function onSessionChange(callback) {
  if (!supabase) return () => {};
  const { data } = supabase.auth.onAuthStateChange((_event, session) => callback(session));
  return () => data.subscription.unsubscribe();
}

export async function signIn() {
  const { error } = await supabase.auth.signInWithOAuth({
    provider: "google",
    // With the trailing slash, so it matches the "<site>/**" redirect URL in Supabase.
    options: { redirectTo: `${window.location.origin}/` },
  });
  if (error) throw error;
}

export async function signOut() {
  if (supabase) await supabase.auth.signOut();
}
