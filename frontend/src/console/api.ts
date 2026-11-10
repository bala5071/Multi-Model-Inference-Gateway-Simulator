import type { ErrorBody } from "./types";

/** The CSRF header the gateway checks on every state change (docs/27). */
export const CSRF_HEADER = "X-CSRF-Token";

/** A refusal from the gateway, with its status and error code; or a network failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface Api {
  get<T>(path: string): Promise<T>;
  send<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T>;
}

function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = value.error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  );
}

async function answer<T>(response: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON: reported by its status below.
  }
  if (response.ok) return body as T;
  if (isErrorBody(body)) throw new ApiError(response.status, body.error.code, body.error.message);
  throw new ApiError(
    response.status,
    "unexpected_answer",
    `The gateway answered ${response.status}.`,
  );
}

/**
 * The console's one way to the gateway. The session is the HttpOnly cookie, which the browser
 * sends and no script can read; the CSRF token is read from `csrf` at each call, from memory
 * only, and sent on every state change. Nothing is cached and nothing is stored.
 */
export function makeApi(csrf: () => string | null, fetcher: typeof fetch = fetch): Api {
  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      const token = csrf();
      if (token !== null) headers[CSRF_HEADER] = token;
    }
    let response: Response;
    try {
      response = await fetcher(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      throw new ApiError(0, "unreachable", "The gateway could not be reached.");
    }
    return answer<T>(response);
  }
  return {
    get: (path) => call("GET", path),
    send: (method, path, body) => call(method, path, body),
  };
}

/**
 * The API of one console session, and how its CSRF token is set: held in this closure, in
 * memory only, never in storage.
 */
export function sessionApi(fetcher?: typeof fetch): {
  api: Api;
  setCsrf: (token: string | null) => void;
} {
  let token: string | null = null;
  return {
    api: makeApi(() => token, fetcher),
    setCsrf: (next) => {
      token = next;
    },
  };
}

export type WriteProbe = "allowed" | "forbidden" | "unknown";

/**
 * Whether the gateway lets this user change the settings at `base`, asked of the gateway
 * itself rather than worked out here: a dry run of an empty change (`validate`, which writes
 * nothing and is not audited) answers 200 to someone who may write and 403 to anyone else.
 * Anything else is `unknown`, and the console then shows no write control.
 */
export async function probeWrite(api: Api, base: string, version: number): Promise<WriteProbe> {
  try {
    await api.send("POST", `${base}/validate`, { expected_version: version, set: {}, unset: [] });
    return "allowed";
  } catch (error) {
    if (error instanceof ApiError && error.status === 403 && error.code === "forbidden") {
      return "forbidden";
    }
    return "unknown";
  }
}

/** The organization's admin prefix, its id escaped. */
export function orgPath(organizationId: string): string {
  return `/v1/admin/orgs/${encodeURIComponent(organizationId)}`;
}

export function applicationSettingsPath(organizationId: string, applicationId: string): string {
  return `${orgPath(organizationId)}/applications/${encodeURIComponent(applicationId)}/settings`;
}

/**
 * Whether the gateway lets this user make the write at `path`, for a write that has no dry run
 * of its own (C2, docs/49): a body the route's own model refuses (a JSON array), which the
 * gateway checks only after it has checked who may write. Nothing is written or audited: 422
 * `invalid_request` to someone who may, 403 `forbidden` to anyone else. Anything else is
 * `unknown`, and the console then shows no write control.
 */
export async function probeRefusedBody(api: Api, path: string): Promise<WriteProbe> {
  try {
    await api.send("POST", path, []);
    // The gateway took an array as a body: not the answer this probe relies on.
    return "unknown";
  } catch (error) {
    if (!(error instanceof ApiError)) return "unknown";
    if (error.status === 422 && error.code === "invalid_request") return "allowed";
    if (error.status === 403 && error.code === "forbidden") return "forbidden";
    return "unknown";
  }
}

export function applicationPath(organizationId: string, applicationId: string): string {
  return `${orgPath(organizationId)}/applications/${encodeURIComponent(applicationId)}`;
}

export function channelPath(organizationId: string, channelId: string): string {
  return `${orgPath(organizationId)}/alert-channels/${encodeURIComponent(channelId)}`;
}

/** A recipient id the confirm probe names: the route resolves only the channel (docs/48). */
export const PROBE_RECIPIENT = "rcp_probe";
