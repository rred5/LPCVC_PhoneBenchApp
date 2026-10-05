// Saved job-setting profiles, kept in the browser's localStorage under the
// signed-in email. Nothing here touches the page or throws: the storage is
// passed in, bad stored data is dropped, and a failed write comes back as an
// `error` message to show.
//
// A profile holds the eight settings the job form can carry between jobs
// (models, backends, prompts, mode, repetitions, restSeconds, maxTokens,
// maxEdge). The job name and the images belong to one job and are never saved.

export const BACKENDS = ["CPU", "GPU", "NPU"];
export const DEFAULT_ID = "default";
export const DEFAULT_NAME = "Default";
export const MAX_PROFILES = 30;
export const MAX_NAME = 40;

const KEY_PREFIX = "phonebench.profiles.v1:";
const MODES = ["warm", "cold"];
const NUMBER_KEYS = ["repetitions", "restSeconds", "maxTokens", "maxEdge"];
const MAX_LIST = 50; // longest stored list of models or prompts
const MAX_TEXT = 10_000; // longest stored string

export function storageKey(email) {
  return KEY_PREFIX + String(email ?? "").trim().toLowerCase();
}

// What is stored for this person: { profiles, selected }. `selected` is the id
// of the last profile chosen, or DEFAULT_ID.
export function readState(storage, email) {
  let stored = null;
  try {
    stored = JSON.parse(storage?.getItem(storageKey(email)) ?? "null");
  } catch {
    // Missing, unreadable or corrupt: start empty.
  }
  const profiles = cleanProfiles(stored?.profiles);
  const selected = profiles.some((profile) => profile.id === stored?.selected) ? stored.selected : DEFAULT_ID;
  return { profiles, selected };
}

// Every change below reads storage again first, so a change made in another
// tab since this page last looked is kept. Each returns { ok, state } plus an
// `error` when ok is false; `state` is what storage holds afterwards.

export function addProfile(storage, email, rawName, settings, makeId = newId) {
  const state = readState(storage, email);
  const fail = (error) => ({ ok: false, error, state });
  if (state.profiles.length >= MAX_PROFILES) return fail(`You can keep at most ${MAX_PROFILES} profiles; remove one first.`);
  const checked = validateName(rawName, state.profiles);
  if (checked.error) return fail(checked.error);
  const stored = storedSettings(settings);
  if (!stored) return fail("Those settings can't be saved.");
  const id = makeId();
  if (id === DEFAULT_ID || state.profiles.some((profile) => profile.id === id)) return fail("Could not make an id for the profile; try again.");
  const profile = { id, name: checked.name, settings: stored };
  const next = { profiles: [...state.profiles, profile], selected: id };
  const error = write(storage, email, next);
  return error ? fail(error) : { ok: true, state: next, profile };
}

export function removeProfile(storage, email, id) {
  const state = readState(storage, email);
  if (!state.profiles.some((profile) => profile.id === id)) return { ok: true, state };
  const next = {
    profiles: state.profiles.filter((profile) => profile.id !== id),
    selected: state.selected === id ? DEFAULT_ID : state.selected,
  };
  const error = write(storage, email, next);
  return error ? { ok: false, error, state } : { ok: true, state: next };
}

// Remember the last profile chosen. `found` says whether it still exists (it
// may have been removed in another tab); it can be true while ok is false if
// only the write failed.
export function selectProfile(storage, email, id) {
  const state = readState(storage, email);
  if (id !== DEFAULT_ID && !state.profiles.some((profile) => profile.id === id)) {
    return { ok: false, found: false, error: "That profile is no longer saved.", state };
  }
  if (state.selected === id) return { ok: true, found: true, state };
  const next = { profiles: state.profiles, selected: id };
  const error = write(storage, email, next);
  return error ? { ok: false, found: true, error, state } : { ok: true, found: true, state: next };
}

// A name for a new profile: trimmed, up to MAX_NAME characters, not "Default",
// and not used by another profile in any capitalisation. Returns { name } or
// { error }.
export function validateName(raw, profiles) {
  const name = typeof raw === "string" ? raw.trim() : "";
  const key = name.toLowerCase();
  if (!name) return { error: "Give the profile a name." };
  if (name.length > MAX_NAME) return { error: `Names are at most ${MAX_NAME} characters.` };
  if (key === DEFAULT_NAME.toLowerCase()) return { error: `"${DEFAULT_NAME}" is the built-in profile; choose another name.` };
  if (profiles.some((profile) => profile.name.toLowerCase() === key)) return { error: `A profile named "${name}" already exists.` };
  return { name };
}

// The form's settings, as the form keeps them, taken from the job form state.
export function pickSettings(source) {
  return {
    models: [...source.models],
    backends: [...source.backends],
    prompts: source.prompts.map((prompt) => prompt.trim()).filter(Boolean),
    mode: source.mode,
    repetitions: source.repetitions,
    restSeconds: source.restSeconds,
    maxTokens: source.maxTokens,
    maxEdge: source.maxEdge,
  };
}

// The settings in /api/v1/options `defaults` (config/defaults.json), in the
// form's shape.
export function defaultSettings(defaults) {
  const { run, preprocessing } = defaults;
  return {
    models: [...run.models],
    backends: [...run.backends],
    prompts: [...run.prompts],
    mode: run.mode.toLowerCase(),
    repetitions: run.repetitions,
    restSeconds: run.rest_seconds,
    maxTokens: run.max_tokens,
    maxEdge: preprocessing.max_edge,
  };
}

// The built-in profile. It is never stored and cannot be removed.
export function builtInProfile(options) {
  return { id: DEFAULT_ID, name: DEFAULT_NAME, settings: defaultSettings(options.defaults) };
}

export function resolveProfile(profiles, id, options) {
  return profiles.find((profile) => profile.id === id) ?? builtInProfile(options);
}

// A saved profile fitted to what the server allows now: models it no longer
// offers are dropped, compute units a chosen model can't run are dropped, and
// prompts and numbers are clamped to the current limits. Anything unusable
// falls back to the server's default. Run this when applying a profile and when
// comparing the form with it.
export function cleanSettings(saved, { defaults, limits, models }) {
  const fallback = defaultSettings(defaults);
  const catalog = new Map(models.map((model) => [model.id, model]));
  const chosen = Array.isArray(saved?.models) ? unique(saved.models).filter((id) => catalog.has(id)) : fallback.models;
  const runnable = BACKENDS.filter((backend) => chosen.every((id) => catalog.get(id)?.backends.includes(backend) ?? true));
  const backends = Array.isArray(saved?.backends) ? unique(saved.backends).filter((backend) => runnable.includes(backend)) : fallback.backends;
  const prompts = (Array.isArray(saved?.prompts) ? saved.prompts : fallback.prompts)
    .filter((prompt) => typeof prompt === "string")
    .map((prompt) => prompt.trim().slice(0, limits.max_prompt_chars))
    .filter(Boolean)
    .slice(0, limits.max_prompts);
  return {
    models: chosen,
    backends,
    prompts: prompts.length ? prompts : [""],
    mode: MODES.includes(saved?.mode) ? saved.mode : fallback.mode,
    repetitions: clamp(saved?.repetitions, limits.repetitions, fallback.repetitions),
    restSeconds: clamp(saved?.restSeconds, limits.rest_seconds, fallback.restSeconds),
    maxTokens: clamp(saved?.maxTokens, limits.max_tokens, fallback.maxTokens),
    maxEdge: clamp(saved?.maxEdge, limits.max_edge, fallback.maxEdge),
  };
}

// Whether two sets of settings would run the same job. The order of models,
// compute units and prompts doesn't matter, and blank prompts don't count.
export function sameSettings(a, b) {
  return JSON.stringify(comparable(a)) === JSON.stringify(comparable(b));
}

// One line describing a profile's settings, for the list of profiles.
export function summarize(settings, models = []) {
  const names = new Map(models.map((model) => [model.id, model.display_name]));
  const prompts = settings.prompts.filter((prompt) => prompt.trim()).length;
  return [
    settings.models.map((id) => names.get(id) ?? id).join(", ") || "no models",
    settings.backends.join(", ") || "no compute units",
    settings.mode,
    `${settings.repetitions} ${settings.repetitions === 1 ? "repetition" : "repetitions"}`,
    `${settings.restSeconds} s rest`,
    `${settings.maxTokens} tokens`,
    `${settings.maxEdge} px`,
    `${prompts} ${prompts === 1 ? "prompt" : "prompts"}`,
  ].join(" · ");
}

function comparable(settings) {
  return {
    models: [...settings.models].sort(),
    backends: [...settings.backends].sort(),
    prompts: settings.prompts.map((prompt) => prompt.trim()).filter(Boolean).sort(),
    mode: settings.mode,
    repetitions: Number(settings.repetitions),
    restSeconds: Number(settings.restSeconds),
    maxTokens: Number(settings.maxTokens),
    maxEdge: Number(settings.maxEdge),
  };
}

function cleanProfiles(list) {
  if (!Array.isArray(list)) return [];
  const profiles = [];
  for (const item of list) {
    if (profiles.length >= MAX_PROFILES) break;
    if (!isObject(item)) continue;
    const { id } = item;
    if (typeof id !== "string" || !id || id.length > 100 || id === DEFAULT_ID) continue;
    if (profiles.some((profile) => profile.id === id)) continue;
    const checked = validateName(item.name, profiles);
    const settings = storedSettings(item.settings);
    if (checked.error || !settings) continue;
    profiles.push({ id, name: checked.name, settings });
  }
  return profiles;
}

// Check that settings have the right shape and tidy them for storage; null if
// they don't. This is about shape only. Limits are applied by cleanSettings
// when a profile is used, since they can change.
function storedSettings(raw) {
  if (!isObject(raw)) return null;
  const models = stringList(raw.models);
  const backends = stringList(raw.backends);
  const prompts = stringList(raw.prompts);
  if (!models || !backends || !prompts || !MODES.includes(raw.mode)) return null;
  if (!NUMBER_KEYS.every((key) => Number.isFinite(raw[key]))) return null;
  return {
    models: unique(models),
    backends: unique(backends).filter((backend) => BACKENDS.includes(backend)),
    prompts: prompts.map((prompt) => prompt.trim()).filter(Boolean),
    mode: raw.mode,
    repetitions: Math.round(raw.repetitions),
    restSeconds: Math.round(raw.restSeconds),
    maxTokens: Math.round(raw.maxTokens),
    maxEdge: Math.round(raw.maxEdge),
  };
}

function stringList(value) {
  const ok = Array.isArray(value) && value.length <= MAX_LIST && value.every((item) => typeof item === "string" && item.length <= MAX_TEXT);
  return ok ? value : null;
}

function write(storage, email, state) {
  if (!storage) return "Could not save: this browser does not allow local storage here.";
  try {
    storage.setItem(storageKey(email), JSON.stringify({ profiles: state.profiles, selected: state.selected }));
    return null;
  } catch (failure) {
    const full = failure?.name === "QuotaExceededError";
    return `Could not save: this browser's storage is ${full ? "full" : "blocked or unavailable"}.`;
  }
}

function clamp(value, [low, high], fallback) {
  const number = Number.isFinite(value) ? Math.round(value) : fallback;
  return Math.min(high, Math.max(low, number));
}

function unique(list) {
  return [...new Set(list)];
}

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function newId() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
