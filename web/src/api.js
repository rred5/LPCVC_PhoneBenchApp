// Calls to the PhoneBench API (docs/API.md), with the sign-in token attached.
import { getToken } from "./auth.js";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = {};
  const token = await getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = `The server answered ${response.status}`;
    try {
      const error = (await response.json()).error;
      code = error.code;
      message = error.message;
    } catch {
      // Not one of the server's JSON errors.
    }
    throw new ApiError(response.status, code, message);
  }
  return raw ? response : response.json();
}

// Fetch a file (following a redirect to storage for large ones) and save it.
export async function download(path, filename) {
  const response = await api(path, { raw: true });
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
