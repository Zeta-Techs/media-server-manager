import { api } from '../../lib/api-client'

export type Server = { id: number; tenant_id: string; name: string; address: string; enabled: boolean; token_configured: boolean; pinyin_mode: string; auth_source: string }
export const listServers = (tenantId: string) => api<Server[]>(`/api/v1/tenants/${tenantId}/servers`)
export const createServer = (tenantId: string, value: Record<string, unknown>) => api<Server>(`/api/v1/tenants/${tenantId}/servers`, { method: 'POST', body: JSON.stringify(value) })
export const deleteServer = (tenantId: string, serverId: number) => api<void>(`/api/v1/tenants/${tenantId}/servers/${serverId}`, { method: 'DELETE' })
export const updateServer = (tenantId: string, serverId: number, value: Record<string, unknown>) => api<Server>(`/api/v1/tenants/${tenantId}/servers/${serverId}`, { method: 'PATCH', body: JSON.stringify(value) })
export const testServer = (tenantId: string, serverId: number) => api<{ id: string; type: string; status: string }>(`/api/v1/tenants/${tenantId}/servers/${serverId}/test`, { method: 'POST' })
export const rotateWebhookSecret = (tenantId: string, serverId: number) => api<{ ok: boolean; webhook_secret: string }>(`/api/v1/tenants/${tenantId}/servers/${serverId}/webhook-secret/rotate`, { method: 'POST' })
