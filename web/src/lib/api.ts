/**
 * The one fetch wrapper (R1/R5). Every request in the app goes through
 * `apiFetch` -- no component calls `fetch()` directly, so auth headers,
 * error shape, and JSON handling live in exactly one place.
 *
 * Auth: the backend sets an httpOnly session cookie on /setup and
 * /auth/login (see server/api/auth.py's `set_session_cookie`), and the dev
 * proxy in vite.config.ts keeps every request same-origin, so
 * `credentials: 'include'` is all that's needed -- no token to manage here.
 */

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

/** FastAPI's own `detail` shape varies by failure kind: a plain string for a
 * hand-raised HTTPException (`server/api/*.py`'s own `raise HTTPException(...,
 * detail="...")` calls), but an ARRAY of `{loc, msg, type}` objects for a
 * Pydantic request-validation failure (422s FastAPI raises itself, before any
 * route code runs -- e.g. `SetupRequest`'s `min_length` constraints). A bare
 * `String(detail)` on that array stringifies each object via its default
 * `Object.prototype.toString`, producing the literal text "[object Object]"
 * instead of the validation message -- this is the one place either shape is
 * turned into text, so it has to handle both. */
function errorDetailText(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          const loc = (item as { loc?: unknown }).loc
          const field = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : ''
          const msg = String((item as { msg: unknown }).msg)
          return field ? `${field}: ${msg}` : msg
        }
        return String(item)
      })
      .join('; ')
  }
  return String(detail)
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (response.status === 204) {
    return undefined as T
  }

  const isJson = response.headers.get('content-type')?.includes('application/json')
  const body = isJson ? await response.json() : await response.text()

  if (!response.ok) {
    const detail =
      isJson && body && typeof body === 'object' && 'detail' in body
        ? errorDetailText((body as { detail: unknown }).detail)
        : String(body)
    throw new ApiError(response.status, detail || response.statusText)
  }

  return body as T
}

export function apiGet<T>(path: string): Promise<T> {
  return apiFetch<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
}

export function apiDelete(path: string): Promise<void> {
  return apiFetch<void>(path, { method: 'DELETE' })
}

/** Encode an object as a querystring, JSON-stringifying any object/array
 * values -- how `GET /records/{entity}` expects `filter`/`sort`/`select`. */
export function withQuery(path: string, params: Record<string, unknown>): string {
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    usp.set(key, typeof value === 'string' ? value : JSON.stringify(value))
  }
  const qs = usp.toString()
  return qs ? `${path}?${qs}` : path
}
