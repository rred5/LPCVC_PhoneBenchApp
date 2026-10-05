import { useEffect, useRef } from "react";

// Run `load` now and every `ms` while the tab is visible; also when it becomes
// visible again. `deps` restart it. Errors are passed to `onError`.
export function usePolling(ms, load, deps = [], onError = () => {}) {
  const loadRef = useRef(load);
  const errorRef = useRef(onError);
  loadRef.current = load;
  errorRef.current = onError;

  useEffect(() => {
    let stopped = false;
    let running = false;
    let timer = null;
    const tick = async () => {
      clearTimeout(timer);
      // A load in progress schedules the next one itself.
      if (stopped || running) return;
      running = true;
      if (document.visibilityState === "visible") {
        try {
          await loadRef.current();
        } catch (error) {
          errorRef.current(error);
        }
      }
      running = false;
      if (!stopped) timer = setTimeout(tick, ms);
    };
    const onVisible = () => document.visibilityState === "visible" && tick();
    document.addEventListener("visibilitychange", onVisible);
    tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}
