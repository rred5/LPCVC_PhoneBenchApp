import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_ID,
  MAX_NAME,
  MAX_PROFILES,
  addProfile,
  builtInProfile,
  cleanSettings,
  pickSettings,
  readState,
  removeProfile,
  resolveProfile,
  sameSettings,
  selectProfile,
  storageKey,
  summarize,
} from "./profiles.js";

function browserStorage() {
  try {
    return window.localStorage;
  } catch {
    return null; // blocked by the browser's privacy settings
  }
}

// The signed-in person's saved profiles. The list follows changes made in other
// tabs; which profile is selected belongs to this tab, so another tab never
// changes what is in this tab's form. The page using this should be keyed by
// the email, so a different person starts fresh.
export function useProfiles(email) {
  const [initial] = useState(() => readState(browserStorage(), email));
  const [profiles, setProfiles] = useState(initial.profiles);
  const [chosen, setChosen] = useState(initial.selected);
  const selectedId = profiles.some((profile) => profile.id === chosen) ? chosen : DEFAULT_ID;

  useEffect(() => {
    const key = storageKey(email);
    const refresh = (event) => {
      if (event.key === null || event.key === key) setProfiles(readState(browserStorage(), email).profiles);
    };
    window.addEventListener("storage", refresh);
    return () => window.removeEventListener("storage", refresh);
  }, [email]);

  const add = (name, settings) => {
    const result = addProfile(browserStorage(), email, name, settings);
    setProfiles(result.state.profiles);
    if (result.ok) setChosen(result.profile.id);
    return result;
  };
  const remove = (id) => {
    const result = removeProfile(browserStorage(), email, id);
    setProfiles(result.state.profiles);
    if (result.ok && id === selectedId) setChosen(DEFAULT_ID);
    return result;
  };
  const select = (id) => {
    const result = selectProfile(browserStorage(), email, id);
    setProfiles(result.state.profiles);
    if (result.found) setChosen(id);
    return result;
  };

  return { profiles, selectedId, add, remove, select };
}

// The profile picker above the New job card. `form` is the job form's state and
// `onApply` receives settings to put into it.
export function ProfileBar({ store, options, form, onApply }) {
  const [managing, setManaging] = useState(false);
  const [notice, setNotice] = useState(null);

  const selected = resolveProfile(store.profiles, store.selectedId, options);
  const effective = cleanSettings(selected.settings, options);
  const modified = !sameSettings(pickSettings(form), effective);

  const choose = (id) => {
    const result = store.select(id);
    setNotice(result.ok ? null : result.error);
    if (result.found) onApply(cleanSettings(resolveProfile(result.state.profiles, id, options).settings, options));
  };
  const add = (name) => {
    const result = store.add(name, pickSettings(form));
    if (result.ok) setNotice(null);
    return result;
  };
  const remove = (id) => {
    const result = store.remove(id);
    if (result.ok && id === store.selectedId) onApply(cleanSettings(builtInProfile(options).settings, options));
    return result;
  };

  return (
    <div className="profile-bar">
      <label className="profile-pick">
        Profile
        <select value={store.selectedId} onChange={(event) => choose(event.target.value)}>
          <option value={DEFAULT_ID}>Default</option>
          {store.profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name}
            </option>
          ))}
        </select>
      </label>
      {modified && (
        <>
          <span className="badge modified">Modified</span>
          <button type="button" className="link" onClick={() => onApply(effective)}>
            Reset
          </button>
        </>
      )}
      <button type="button" className="manage" onClick={() => setManaging(true)}>
        Manage profiles
      </button>
      {notice && <p className="error small profile-notice">{notice}</p>}
      {managing && <ManageDialog store={store} options={options} onAdd={add} onRemove={remove} onClose={() => setManaging(false)} />}
    </div>
  );
}

function ManageDialog({ store, options, onAdd, onRemove, onClose }) {
  const dialog = useRef(null);
  const pressedBackdrop = useRef(false);
  const [name, setName] = useState("");
  const [error, setError] = useState(null);

  // Opened once on mount. StrictMode runs this twice in development, and
  // showModal() on a dialog that is already open throws.
  useEffect(() => {
    if (dialog.current && !dialog.current.open) dialog.current.showModal();
  }, []);

  const add = (event) => {
    event.preventDefault();
    const result = onAdd(name);
    setError(result.ok ? null : result.error);
    if (result.ok) setName("");
  };
  const remove = (profile) => {
    const inUse = profile.id === store.selectedId;
    const question = `Remove the profile "${profile.name}"?` + (inUse ? " The job form goes back to the Default settings." : "");
    if (!window.confirm(question)) return;
    const result = onRemove(profile.id);
    setError(result.ok ? null : result.error);
  };

  const rows = [builtInProfile(options), ...store.profiles];
  return (
    <dialog
      ref={dialog}
      className="profiles-dialog"
      aria-labelledby="profiles-title"
      onClose={onClose}
      // The dialog has no padding of its own, so a click that lands on the
      // dialog element itself is a click on the backdrop. Both the press and the
      // release must be there, so selecting text and letting go outside stays open.
      onMouseDown={(event) => {
        pressedBackdrop.current = event.target === event.currentTarget;
      }}
      onClick={(event) => {
        if (pressedBackdrop.current && event.target === event.currentTarget) event.currentTarget.close();
      }}
    >
      <div className="dialog-body">
        <h2 id="profiles-title">Manage profiles</h2>
        <p className="muted small">
          A profile saves the models, compute units, mode, prompts and numbers in the form. The job name and images are not saved. Profiles are
          kept in this browser only.
        </p>

        <form className="profile-add" onSubmit={add}>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Name for the current settings"
            aria-label="Profile name"
            maxLength={MAX_NAME}
          />
          <button type="submit" className="primary">
            Add profile
          </button>
        </form>
        {error && <p className="error small">{error}</p>}

        <ul className="profile-list">
          {rows.map((profile) => (
            <li key={profile.id}>
              <div className="profile-info">
                <div>
                  <strong>{profile.name}</strong>
                  {profile.id === DEFAULT_ID && <span className="muted small"> built in</span>}{" "}
                  {profile.id === store.selectedId && <span className="badge">in use</span>}
                </div>
                <div className="muted small profile-summary" title={summarize(profile.settings, options.models)}>
                  {summarize(profile.settings, options.models)}
                </div>
              </div>
              {profile.id !== DEFAULT_ID && (
                <button type="button" className="danger" onClick={() => remove(profile)}>
                  Remove
                </button>
              )}
            </li>
          ))}
        </ul>
        <p className="muted small">
          {store.profiles.length} of {MAX_PROFILES} saved.
        </p>

        <div className="dialog-actions">
          <button type="button" onClick={() => dialog.current?.close()}>
            Close
          </button>
        </div>
      </div>
    </dialog>
  );
}
