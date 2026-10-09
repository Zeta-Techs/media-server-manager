export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Accept': 'application/json', ...(init?.headers || {}) } })
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`)
  return response.json() as Promise<T>
}
