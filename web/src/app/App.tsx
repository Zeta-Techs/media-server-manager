import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api-client'

export function App() {
  const health = useQuery({ queryKey: ['health'], queryFn: () => api<{status: string}>('/healthz') })
  return <main className="shell"><header><div><strong>Media Server Manager</strong><span className="tag">多租户控制台</span></div><span className="status">API {health.isLoading ? '连接中…' : health.data?.status === 'ok' ? '在线' : '离线'}</span></header><section className="hero"><p className="eyebrow">MODULAR PLATFORM</p><h1>管理 Plex 媒体基础设施</h1><p>React 前端、FastAPI API、PostgreSQL、Redis 和 Celery 已准备好按租户扩展。</p></section><section className="cards"><article><h2>租户隔离</h2><p>每个业务请求通过 JWT 和成员关系解析租户上下文。</p></article><article><h2>异步任务</h2><p>长时间运行的 Plex、TMDB 和媒体库操作进入可扩展队列。</p></article><article><h2>兼容迁移</h2><p>现有 Flask/SQLite 入口保留，新模块可以逐步接管功能。</p></article></section></main>
}
