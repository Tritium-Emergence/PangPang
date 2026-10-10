// Single HTTP boundary. Mirrors the old app.js api(): same-origin credentials,
// Idempotency-Key on mutations, 401 surfaces as a typed error so the shell can show
// login. The server is the only reality source; this module never caches domain truth.

export class ApiError extends Error {
  status: number;
  constructor(code: string, status: number) { super(code); this.status = status; }
}

export async function api<T = any>(path: string, body?: unknown, requestKey?: string): Promise<T> {
  const options: RequestInit = {credentials: 'same-origin', headers: {}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {
      'Content-Type': 'application/json',
      'Idempotency-Key': requestKey || crypto.randomUUID(),
    };
    options.body = JSON.stringify(body);
  }
  const r = await fetch('/api/v1/' + path, options);
  const value = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(value.code || r.statusText, r.status);
  return value as T;
}
