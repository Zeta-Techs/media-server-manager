import { api, escapeHtml } from "../api.js";
import { state } from "../state.js";

const $ = (selector) => document.querySelector(selector);

export function bindOverview() {
  $("#refresh-overview").addEventListener("click", loadOverview);
}

export async function loadOverview() {
  const [overview, media] = await Promise.all([
    api("/api/overview"),
    api("/api/overview/media?limit=12").catch(() => ({ items: [], warnings: [] })),
  ]);
  state.overview = overview;
  state.overviewMedia = media;
  renderOverview();
}

function renderOverview() {
  const overview = state.overview;
  if (!overview) return;
  const workerOnline = Boolean(overview.worker?.online);
  const workerStatus = $("#topbar-worker");
  if (workerStatus) {
    workerStatus.className = `topbar-status ${workerOnline ? "online" : "offline"}`;
    workerStatus.innerHTML = `<span class="status-dot" aria-hidden="true"></span>Worker ${workerOnline ? "在线" : "离线"}`;
  }
  $("#overview-cards").innerHTML = `
    <div class="metric"><span>服务器</span><strong>${overview.servers.enabled}/${overview.servers.total}</strong></div>
    <div class="metric"><span>运行中</span><strong>${overview.jobs.running}</strong></div>
    <div class="metric"><span>排队</span><strong>${overview.jobs.queued}</strong></div>
    <div class="metric"><span>Webhook 事件</span><strong>${overview.automation.webhook_events}</strong></div>
    <div class="metric"><span>启用定时</span><strong>${overview.automation.active_schedules}</strong></div>
    <div class="metric"><span>通知渠道</span><strong>${overview.automation.notification_channels}</strong></div>
    <div class="metric"><span>任务 Worker</span><strong>${workerOnline ? "在线" : "离线"}</strong></div>
  `;
  renderMediaShelf();
  $("#overview-recent-jobs").innerHTML = overview.jobs.recent.length
    ? overview.jobs.recent.map(renderJobMini).join("")
    : '<div class="item">暂无任务。</div>';
  $("#overview-alerts").innerHTML = overview.jobs.failed.length
    ? overview.jobs.failed.map(renderJobMini).join("")
    : '<div class="item">当前没有失败任务。</div>';
}

function renderMediaShelf() {
  const shelf = $("#overview-media");
  if (!shelf) return;
  const items = state.overviewMedia?.items || [];
  if (!items.length) {
    shelf.innerHTML = '<div class="media-empty">暂无可展示的最近媒体。配置 Plex 服务器后会显示海报。</div>';
    return;
  }
  shelf.innerHTML = items
    .map((item) => {
      const title = escapeHtml(item.title || "未命名媒体");
      const initial = escapeHtml((item.title || "?").trim().slice(0, 1).toUpperCase());
      const details = [item.year, item.type].filter(Boolean).map(escapeHtml).join(" · ");
      return `
        <article class="media-card">
          <div class="media-poster">
            <div class="media-fallback" ${item.image_url ? "hidden" : ""} aria-hidden="true">${initial}</div>
            ${item.image_url ? `<img loading="lazy" src="${escapeHtml(item.image_url)}" alt="${title} 海报" data-media-fallback>` : ""}
          </div>
          <div class="media-meta"><strong title="${title}">${title}</strong><span>${escapeHtml(details || item.server_name || "Plex 媒体")}</span></div>
        </article>`;
    })
    .join("");
  shelf.querySelectorAll("img[data-media-fallback]").forEach((image) => {
    image.addEventListener("error", () => {
      image.hidden = true;
      const fallback = image.parentElement?.querySelector(".media-fallback");
      if (fallback) fallback.hidden = false;
    }, { once: true });
  });
}

function renderJobMini(job) {
  return `<div class="item"><strong>#${job.id} ${escapeHtml(job.type)} · ${escapeHtml(job.status)}</strong><div class="meta">${escapeHtml(job.server_name)} · ${escapeHtml(job.created_at)}</div></div>`;
}
