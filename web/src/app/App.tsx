import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../lib/api-client'
import { cancelJob, getJobEvents, getJobLogs, listJobs, retryJob, type Job } from '../features/jobs/api'
import { deleteServer, listServers, rotateWebhookSecret, testServer, type Server } from '../features/servers/api'

const tenantId = localStorage.getItem('msm_tenant_id') || ''

function ServerPanel() {
  const queryClient = useQueryClient()
  const servers = useQuery({ queryKey: ['servers', tenantId], queryFn: () => listServers(tenantId), enabled: Boolean(tenantId) })
  const [message, setMessage] = useState('')
  async function run(action: () => Promise<unknown>, success: string) {
    try { const result = await action(); setMessage(typeof result === 'object' && result && 'webhook_secret' in result ? `新 Secret：${(result as { webhook_secret: string }).webhook_secret}` : success); await queryClient.invalidateQueries({ queryKey: ['servers', tenantId] }) } catch (error) { setMessage(String(error)) }
  }
  return <section className="panel"><div className="panel-title"><h2>Plex 服务器</h2><span>{servers.data?.length || 0} 个</span></div>{message && <p>{message}</p>}{servers.isError && <p className="error">{String(servers.error)}</p>}{servers.isLoading && <p>加载中…</p>}<div className="rows">{servers.data?.map((server: Server) => <div className="row" key={server.id}><div><strong>{server.name}</strong><small>{server.address}</small></div><div><span className={server.enabled ? 'pill ok' : 'pill'}>{server.enabled ? '已启用' : '已停用'}</span><button onClick={() => run(() => testServer(tenantId, server.id), '连接测试任务已创建')}>测试</button><button onClick={() => run(() => rotateWebhookSecret(tenantId, server.id), 'Webhook Secret 已轮换')}>轮换 Secret</button><button onClick={() => run(() => deleteServer(tenantId, server.id), '服务器已删除')}>删除</button></div></div>)}</div></section>
}

function JobPanel() {
  const queryClient = useQueryClient()
  const jobs = useQuery({ queryKey: ['jobs', tenantId], queryFn: () => listJobs(tenantId), enabled: Boolean(tenantId), refetchInterval: 5000 })
  const [selected, setSelected] = useState<string | null>(null)
  const logs = useQuery({ queryKey: ['job-logs', tenantId, selected], queryFn: () => getJobLogs(tenantId, selected!), enabled: Boolean(selected) })
  const events = useQuery({ queryKey: ['job-events', tenantId, selected], queryFn: () => getJobEvents(tenantId, selected!), enabled: Boolean(selected), refetchInterval: 2000 })
  async function mutate(action: () => Promise<unknown>) { await action(); await queryClient.invalidateQueries({ queryKey: ['jobs', tenantId] }) }
  return <section className="panel"><div className="panel-title"><h2>任务队列</h2><span>{jobs.data?.length || 0} 条</span></div>{jobs.isError && <p className="error">{String(jobs.error)}</p>}<div className="rows">{jobs.data?.map((job: Job) => <div className="row" key={job.id} onClick={() => setSelected(job.id)}><div><strong>{job.type}</strong><small>{job.id}</small></div><div><span className="pill">{job.status}</span>{['queued', 'running', 'cancelling'].includes(job.status) && <button onClick={event => { event.stopPropagation(); void mutate(() => cancelJob(tenantId, job.id)) }}>取消</button>}{['failed', 'cancelled', 'interrupted'].includes(job.status) && <button onClick={event => { event.stopPropagation(); void mutate(() => retryJob(tenantId, job.id)) }}>重试</button>}</div></div>)}</div>{selected && <div className="job-detail"><h3>任务详情</h3><div className="rows">{logs.data?.map(log => <p key={log.id}>{log.created_at} [{log.level}] {log.message}</p>)}</div><div className="rows">{events.data?.map(event => <p key={event.id}>{event.created_at} {event.event_type}</p>)}</div></div>}</section>
}

export function App() {
  const health = useQuery({ queryKey: ['health'], queryFn: () => api<{ status: string }>('/healthz') })
  const [active, setActive] = useState<'overview' | 'servers' | 'jobs'>('overview')
  return <main className="shell"><header><div><strong>Media Server Manager</strong><span className="tag">多租户控制台</span></div><span className="status">API {health.isLoading ? '连接中…' : health.data?.status === 'ok' ? '在线' : '离线'}</span></header><nav><button onClick={() => setActive('overview')}>概览</button><button onClick={() => setActive('servers')}>服务器</button><button onClick={() => setActive('jobs')}>任务</button></nav>{active === 'overview' && <section className="hero"><p className="eyebrow">TENANT OPERATIONS</p><h1>管理 Plex 媒体基础设施</h1><p>服务器和任务已接入租户隔离的 FastAPI、PostgreSQL、Redis 和 Celery 闭环。</p><div className="cards"><article><h2>租户隔离</h2><p>每个请求通过 JWT、成员关系和数据库策略解析租户上下文。</p></article><article><h2>异步任务</h2><p>任务先持久化，再通过 outbox 投递到对应 Celery 队列。</p></article><article><h2>安全边界</h2><p>服务器 Token 和 Webhook Secret 永远不会出现在列表响应中。</p></article></div></section>}{active === 'servers' && <ServerPanel />}{active === 'jobs' && <JobPanel />}</main>
}
