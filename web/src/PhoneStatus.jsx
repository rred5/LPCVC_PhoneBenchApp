import { useEffect, useState } from "react";
import { api } from "./api.js";
import { usePolling } from "./usePolling.js";

const LABELS = {
  idle: "idle",
  downloading: "downloading a model",
  running: "running",
  paused: "paused",
  error: "has a problem",
  offline: "offline",
};

export default function PhoneStatus() {
  const [device, setDevice] = useState(undefined);
  const [now, setNow] = useState(Date.now());

  usePolling(10_000, async () => {
    const { devices } = await api("/api/v1/device/status");
    setDevice(devices[0] ?? null);
  });
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  if (device === undefined) return null;
  if (device === null) return <span className="phone offline">No phone set up</span>;
  const state = device.state ?? "offline";
  const seen = device.last_seen ? Math.max(0, Math.round(now / 1000 - device.last_seen)) : null;
  return (
    <span className={`phone ${state}`} title={seen === null ? "Never seen" : `Last seen ${seen} s ago`}>
      <span className="dot" /> Phone {LABELS[state] ?? state}
      {state === "offline" && seen !== null && <span className="muted"> · last seen {ago(seen)}</span>}
    </span>
  );
}

function ago(seconds) {
  if (seconds < 120) return `${seconds} s ago`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}
