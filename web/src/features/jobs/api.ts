import { api, idempotentApi } from '../../lib/api-client'

export type Job = { id: string; type: string; server_id: number | null; status: string; payload: Record<string, unknown>; error: string; created_at: string | null }
export const listJobs = (tenantId: string) => api<Job[]>(`/api/v1/tenants/${tenantId}/jobs`)
export const cancelJob = (tenantId: string, jobId: string) => api<Job>(`/api/v1/tenants/${tenantId}/jobs/${jobId}/cancel`, { method: 'POST' })
export const retryJob = (tenantId: string, jobId: string) => api<Job>(`/api/v1/tenants/${tenantId}/jobs/${jobId}/retry`, { method: 'POST' })
export const createJob = (tenantId: string, value: Record<string, unknown>, idempotencyKey: string) => idempotentApi<Job>(`/api/v1/tenants/${tenantId}/jobs`, idempotencyKey, { method: 'POST', body: JSON.stringify(value) })
export const getJobLogs = (tenantId: string, jobId: string) => api<Array<{ id: number; message: string; level: string; created_at: string }>>(`/api/v1/tenants/${tenantId}/jobs/${jobId}/logs`)
export const getJobEvents = (tenantId: string, jobId: string) => api<Array<{ id: number; event_type: string; payload: Record<string, unknown>; created_at: string }>>(`/api/v1/tenants/${tenantId}/jobs/${jobId}/events`)
