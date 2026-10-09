export type ApiError = { error?: { code?: string; message?: string }; detail?: string; request_id?: string }

const requestId = () => `req_${crypto.randomUUID()}`

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem('msm_access_token')
  const response = await fetch(path, { ...init, headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Request-ID': requestId(), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init.headers || {}) } })
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as ApiError
    throw new Error(error.error?.message || error.detail || `HTTP ${response.status}`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function idempotentApi<T>(path: string, key: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Idempotency-Key', key)
  return api<T>(path, { ...init, headers })
}

export function eventStream(path: string, onMessage: (value: unknown) => void): () => void {
  const token = localStorage.getItem('msm_access_token')
  const source = new EventSource(token ? `${path}${path.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(token)}` : path)
  source.onmessage = event => onMessage(JSON.parse(event.data))
  return () => source.close()
}
