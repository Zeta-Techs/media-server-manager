import { api, escapeHtml, setCsrfToken } from "./api.js";
import { state } from "./state.js";
import { bindOverview, loadOverview } from "./views/overview.js";

const MAX_VISIBLE_LOG_LINES = 500;
const JOB_LIST_REFRESH_INTERVAL = 2000;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function showAuth() {
  $("#auth-screen").hidden = false;
  $("#app").hidden = true;
  $("#auth-copy").textContent = state.initialized ? "登录 CLP 管理端。" : "创建首个本地管理员账号。";
}

function showApp() {
  $("#auth-screen").hidden = true;
  $("#app").hidden = false;
}

async function bootstrap() {
  const session = await api("/api/session");
  setCsrfToken(session.csrf_token);
  state.initialized = session.initialized;
  state.authenticated = session.authenticated;
  if (!state.initialized || !state.authenticated) {
    showAuth();
    return;
  }
  showApp();
  await refreshInitial();
  const savedView = window.location.pathname === "/media-library/detail" ? "media-library-detail" : (new URLSearchParams(window.location.search).get("view") || sessionStorage.getItem("clp-active-view") || "overview");
  await switchView(savedView);
}

async function refreshInitial() {
  await Promise.all([loadOverview(), loadServers(), loadJobs()]);
  state.loadedViews.add("overview");
  state.loadedViews.add("servers");
  state.loadedViews.add("jobs");
}

async function refreshAll() {
  await Promise.all([loadOverview(), loadServers(), loadJobs(), loadSchedules(), loadCollectionRules(), loadWebhookData(), loadNotifications()]);
  await loadTags();
  ["overview", "servers", "localization", "jobs", "automation", "tools", "system"].forEach((view) => state.loadedViews.add(view));
}

function setMessage(selector, message, isError = false) {
  const el = $(selector);
  el.textContent = message || "";
  el.className = isError ? "error" : "message";
}

function splitTime(value) {
  const [hour = "22", minute = "30"] = (value || "22:30").split(":");
  return { hour: Number(hour), minute: Number(minute) };
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    const result = await api(state.initialized ? "/api/auth/login" : "/api/setup", {
      method: "POST",
      body: JSON.stringify(data),
    });
    setCsrfToken(result.csrf_token);
    state.initialized = true;
    await bootstrap();
  } catch (error) {
    $("#auth-error").textContent = error.message;
  }
});

$("#logout").addEventListener("click", async () => {
  closeEventSource();
  if (initialJob) updateJobProgress(initialJob);
  const result = await api("/api/auth/logout", { method: "POST", body: "{}" });
  setCsrfToken(result.csrf_token);
  state.authenticated = false;
  showAuth();
});

$$(".nav").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

$(".brand")?.addEventListener("click", (event) => {
  event.preventDefault();
  switchView("overview");
});

$$(".tab").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
});

async function switchView(view) {
  if (view !== "media-library-detail") sessionStorage.setItem("clp-active-view", view);
  $$(".nav").forEach((item) => item.classList.toggle("active", item.dataset.view === (view === "media-library-detail" ? "media-library" : view)));
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  if (view !== "jobs") closeEventSource();
  await ensureViewData(view);
}

function switchTab(tab) {
  const button = $(`.tab[data-tab="${tab}"]`);
  if (!button) return;
  const container = button.closest(".view");
  container.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  container.querySelectorAll(".tab-panel").forEach((item) => item.classList.toggle("active", item.id === `tab-${tab}`));
}

async function ensureViewData(view) {
  if (state.loadedViews.has(view)) return;
  if (view === "overview") await loadOverview();
  if (view === "servers") await loadServers();
  if (view === "media-library") {
    await loadServers();
    await loadMediaLibrary();
  }
  if (view === "media-library-detail") await loadMediaDetailFromUrl();
  if (view === "localization") await Promise.all([loadServers(), loadTags()]);
  if (view === "jobs") await loadJobs();
  if (view === "automation") {
    await Promise.all([loadServers(), loadSchedules(), loadWebhookData(), loadNotifications()]);
    await updateSchedulePreview();
  }
  if (view === "tools") await Promise.all([loadServers(), loadCollectionRules(), loadContinueWatchingRuns(), loadEpisodeAuditSettings(), loadEpisodeAuditRuns()]);
  if (view === "system") await loadDiagnostics();
  state.loadedViews.add(view);
}

function goToJobs(jobId) {
  switchView("jobs");
  if (jobId) selectJob(jobId);
}

async function loadServers() {
  state.servers = await api("/api/servers");
  renderServers();
  fillServerSelects();
  renderWebhooks();
}

function renderServers() {
  const list = $("#server-list");
  list.innerHTML = "";
  state.servers.forEach((server) => {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <strong>${escapeHtml(server.name)}</strong>
      <div class="meta">${escapeHtml(server.address)}</div>
      <div class="meta">跳过：${escapeHtml(server.skip_libraries || "无")} · ${server.enabled ? "启用" : "停用"}</div>
      <button type="button">编辑</button>
    `;
    item.querySelector("button").addEventListener("click", () => fillServerForm(server));
    list.appendChild(item);
  });
}

function fillServerSelects() {
  const options = state.servers.map((server) => `<option value="${server.id}">${escapeHtml(server.name)}</option>`).join("");
  $("#localize-server").innerHTML = options;
  $("#maintenance-server").innerHTML = options;
  $("#collection-server").innerHTML = options;
  $("#continue-watching-server").innerHTML = options;
  $("#episode-audit-server").innerHTML = options;
  $("#media-library-server").innerHTML = options;
  $("#webhook-rule-server").innerHTML = options;
  $("#tag-suggestion-server").innerHTML = options;
  $("#schedule-form").server_id.innerHTML = options;
}

function mediaImageUrl(serverId, path) {
  return path ? `/api/servers/${encodeURIComponent(serverId)}/media-image?path=${encodeURIComponent(path)}` : "";
}

function formatMinutes(milliseconds) {
  const minutes = Math.round(Number(milliseconds || 0) / 60000);
  return minutes ? `${minutes} 分钟` : "-";
}

function renderMediaLibrarySummary(libraries) {
  const totalMovies = libraries.filter((library) => library.kind === "movie").reduce((sum, library) => sum + Number(library.item_count || library.items?.length || 0), 0);
  const totalShows = libraries.filter((library) => library.kind === "show").reduce((sum, library) => sum + Number(library.item_count || library.items?.length || 0), 0);
  const totalCollections = libraries.reduce((sum, library) => sum + (library.collections || []).length, 0);
  const totalEpisodes = libraries.filter((library) => library.kind === "show").reduce((sum, library) => sum + library.items.reduce((n, show) => n + (show.episode_count || (show.seasons || []).reduce((m, season) => m + (season.episodes || []).length, 0)), 0), 0);
  $("#media-library-summary").innerHTML = [["媒体库", libraries.length], ["电影", totalMovies], ["剧集", totalShows], ["集数", totalEpisodes], ["合集", totalCollections]].map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

function renderMediaLibrary(libraries, serverId) {
  state.mediaLibraryData = libraries;
  if (!state.mediaLibrarySelectedId || !libraries.some((library) => String(library.id) === String(state.mediaLibrarySelectedId))) {
    state.mediaLibrarySelectedId = libraries[0]?.id || null;
  }
  renderMediaLibrarySummary(libraries);
  const picker = $("#media-library-picker");
  picker.innerHTML = libraries.map((library) => `<option value="${library.id}">${escapeHtml(library.title)} · ${library.kind === "movie" ? "电影" : library.kind === "show" ? "剧集" : "合集"} · ${library.item_count || library.items?.length || 0} 项</option>`).join("");
  picker.value = String(state.mediaLibrarySelectedId || "");
  const sort = $("#media-library-sort");
  if (sort) sort.value = state.mediaLibrarySort;
  const sortDirection = $("#media-library-sort-direction");
  if (sortDirection) sortDirection.textContent = state.mediaLibraryDirection === "asc" ? "升序" : "降序";
  const animationMode = $("#media-library-animation-mode");
  if (animationMode) animationMode.value = libraries.find((library) => String(library.id) === String(state.mediaLibrarySelectedId))?.animation_mode || "auto";
  const syncLibrary = libraries.find((library) => String(library.id) === String(state.mediaLibrarySelectedId));
  if (syncLibrary) {
    const syncStatus = $("#media-library-sync-status");
    if (syncStatus) syncStatus.textContent = `${syncLibrary.plex_synced_at ? `Plex：${syncLibrary.plex_synced_at}` : "尚未从 Plex 同步"}${syncLibrary.tmdb_synced_at ? ` · TMDB：${syncLibrary.tmdb_synced_at}` : ""}${syncLibrary.sync_status && syncLibrary.sync_status !== "idle" ? ` · 状态：${syncLibrary.sync_status}` : ""}${syncLibrary.sync_error ? ` · ${syncLibrary.sync_error}` : ""}`;
    renderMediaLibrarySyncProgress(syncLibrary);
  }
  $("#media-library-tabs").innerHTML = libraries.map((library) => `<button type="button" class="media-library-tab ${String(library.id) === String(state.mediaLibrarySelectedId) ? "active" : ""}" data-library-tab="${library.id}">${escapeHtml(library.title)}<span>${library.item_count || library.items?.length || 0}</span></button>`).join("");
  $("#media-library-tabs").querySelectorAll("[data-library-tab]").forEach((button) => button.addEventListener("click", () => {
    state.mediaLibrarySelectedId = button.dataset.libraryTab;
    state.mediaLibraryPage = 1;
    picker.value = state.mediaLibrarySelectedId;
    $("#media-library-tabs").querySelectorAll(".media-library-tab").forEach((item) => item.classList.toggle("active", item === button));
    selectMediaLibrary(state.mediaLibrarySelectedId, serverId);
  }));
  renderMediaLibraryPage(serverId);
}

function renderMediaLibrarySyncProgress(library) {
  const box = $("#media-library-sync-progress");
  if (!box) return;
  const active = ["queued", "running"].includes(library?.sync_status);
  box.hidden = !active;
  if (!active) return;
  const processed = Number(library.sync_processed || 0);
  const total = Number(library.sync_total || 0);
  const percent = total ? Math.min(100, Math.round(processed * 100 / total)) : 0;
  $("#media-library-sync-progress-title").textContent = library.sync_status === "queued" ? "等待 Worker 拉取" : `正在拉取 · ${library.sync_stage || "处理中"}`;
  $("#media-library-sync-progress-text").textContent = total ? `${processed} / ${total}（${percent}%）` : "准备中…";
  $("#media-library-sync-progress-bar").style.width = `${percent}%`;
  $("#media-library-sync-progress-detail").textContent = library.sync_current_library ? `当前阶段：${library.sync_current_library}` : "任务已加入后台队列，页面可继续使用。";
}

async function selectMediaLibrary(libraryId, serverId) {
  state.mediaLibrarySelectedId = libraryId;
  state.mediaLibraryPage = 1;
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(libraryId));
  if (library?.is_animation) {
    state.mediaLibrarySort = "first_episode_date";
    state.mediaLibraryDirection = "desc";
    const sort = $("#media-library-sort");
    if (sort) sort.value = state.mediaLibrarySort;
    const direction = $("#media-library-sort-direction");
    if (direction) direction.textContent = "降序";
  }
  if (library && !library.items?.length) {
    try {
      const cached = await api(`/api/servers/${serverId}/media-library/${library.id}/items`);
      library.items = cached.items || [];
      renderMediaLibraryPage(serverId);
    } catch (error) {
      setMessage("#media-library-message", error.message, true);
    }
  }
  renderMediaLibraryPage(serverId);
  if (library?.kind === "show" && library.is_animation) {
    const quarter = await api(`/api/servers/${serverId}/media-library/${library.id}/quarter-index`);
    state.mediaLibraryQuarterGroups = quarter.groups || [];
    state.mediaLibraryLiveItems = quarter.live_items || [];
    renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems);
  } else {
    await loadVisibleShowDetails(serverId);
  }
}

function renderMediaLibraryPage(serverId) {
  const libraries = state.mediaLibraryData || [];
  const library = libraries.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
  const content = $("#media-library-content");
  if (!library) {
    content.innerHTML = '<div class="panel media-empty">没有读取到媒体库内容。请检查服务器连接和权限。</div>';
    return;
  }
  const orderedItems = sortMediaItems(library.items);
  const pageItems = orderedItems;
  const collectionMarkup = (library.collections || []).length ? `<div class="library-collections"><div class="section-label">合集 · ${library.collections.length}</div><div class="collection-strip">${library.collections.map((item) => `<div class="collection-chip"><strong>${escapeHtml(item.title)}</strong><span>${item.child_count || 0} 项</span></div>`).join("")}</div></div>` : "";
  const panel = (() => {
    const collectionMarkup = (library.collections || []).length ? `<div class="library-collections"><div class="section-label">合集 · ${library.collections.length}</div><div class="collection-strip">${library.collections.map((item) => `<div class="collection-chip"><strong>${escapeHtml(item.title)}</strong><span>${item.child_count || 0} 项</span></div>`).join("")}</div></div>` : "";
    if (library.kind === "movie") {
      return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">MOVIES · ${library.items.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">已加载全部 ${library.items.length} 项</span></div><span class="library-type">电影</span></div>${collectionMarkup}<div class="movie-table-wrap"><table class="movie-table"><thead><tr><th>海报</th><th>标题</th><th>年份</th><th>类型</th><th>分级</th><th>片长</th><th>条目键</th><th>操作</th></tr></thead><tbody>${pageItems.map((item) => `<tr><td>${item.thumb ? `<img class="table-poster" src="${mediaImageUrl(serverId, item.thumb)}" alt="">` : '<span class="table-poster fallback">影</span>'}</td><td><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.original_title || "")}</small></td><td>${escapeHtml(item.year || "-")}</td><td>${escapeHtml(item.genre || "-")}</td><td>${escapeHtml(item.content_rating || "-")}</td><td>${formatMinutes(item.duration)}</td><td><code>${escapeHtml(item.rating_key)}</code></td><td><button class="small media-detail-link" type="button" data-library-id="${library.id}" data-rating-key="${escapeHtml(item.rating_key)}" data-media-type="movie">查看详情</button></td></tr>`).join("")}</tbody></table></div></section>`;
    }
    if (library.kind === "show") {
      return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">SERIES · ${library.items.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">已加载全部 ${library.items.length} 项</span></div><span class="library-type">剧集</span></div>${collectionMarkup}<div class="show-grid">${pageItems.map((show) => renderShowCard(show, serverId)).join("")}</div></section>`;
    }
    return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">COLLECTIONS · ${library.items.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">已加载全部 ${library.items.length} 项</span></div><span class="library-type">合集</span></div><div class="collection-grid">${pageItems.map((item) => `<article class="collection-card">${item.thumb ? `<img src="${mediaImageUrl(serverId, item.thumb)}" alt="">` : '<div class="collection-fallback">集</div>'}<div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.summary || "暂无简介")}</span></div></article>`).join("")}</div></section>`;
  })();
  content.innerHTML = panel;
  content.querySelectorAll(".media-recheck").forEach((button) => button.addEventListener("click", handleMediaRecheck));
  content.querySelectorAll(".media-detail-link").forEach((button) => button.addEventListener("click", () => openMediaDetail(button.dataset.ratingKey, button.dataset.libraryId, button.dataset.mediaType)));
  bindMediaKindToggles();
  bindSeasonToggles();
}

function renderAnimationQuarterPage(groups, serverId, liveItems = []) {
  const content = $("#media-library-content");
  const animationField = state.mediaLibrarySort === "name" || state.mediaLibrarySort === "year" ? "first_episode_date" : state.mediaLibrarySort;
  const animationDirection = state.mediaLibrarySort === "name" || state.mediaLibrarySort === "year" ? state.mediaLibraryAnimationDirection : state.mediaLibraryDirection;
  const sortQuarterItems = (items) => [...items].sort((a, b) => {
    const av = a[animationField] ?? "";
    const bv = b[animationField] ?? "";
    const emptyA = av === "" || av == null;
    const emptyB = bv === "" || bv == null;
    if (emptyA !== emptyB) return emptyA ? 1 : -1;
    const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true }) || String(a.title || "").localeCompare(String(b.title || ""));
    return animationDirection === "desc" ? -cmp : cmp;
  });
  // Keep the server's quarter ordering (latest normal quarter first, then
  // 特别篇 and 未定档) while sorting cards only inside each quarter.
  const visibleGroups = groups.reduce((map, group) => {
    map[group.label] = sortQuarterItems(group.items || []);
    return map;
  }, {});
  const directoryGroups = groups.reduce((map, group) => {
    const label = String(group.label || "");
    const yearMatch = label.match(/^(\d{4})\s+/);
    // Prefix keys so JavaScript does not reorder numeric year properties.
    const key = yearMatch ? `year-${yearMatch[1]}` : `special-${label}`;
    if (!map[key]) map[key] = { year: yearMatch ? yearMatch[1] : label, total: 0, quarters: [] };
    map[key].total += (group.items || []).length;
    map[key].quarters.push({ label, count: (group.items || []).length });
    return map;
  }, {});
  const directoryMarkup = Object.values(directoryGroups).map((yearGroup) => {
    const title = yearGroup.year;
    const isYear = /^\d{4}$/.test(title);
    const quarterBySlot = yearGroup.quarters.reduce((slots, quarter) => {
      const match = quarter.label.match(/\bQ([1-4])\b/);
      if (match) slots[match[1]] = quarter;
      return slots;
    }, {});
    const quarterSlots = isYear ? ["4", "3", "2", "1"].map((slot) => {
      const quarter = quarterBySlot[slot];
      if (!quarter) return '<span class="quarter-slot empty" aria-hidden="true">&nbsp;</span>';
      const shortLabel = (quarter.label.includes("/") ? quarter.label.split("/").pop().trim() : quarter.label).replace(/番$/, "");
      return `<a href="#quarter-${encodeURIComponent(quarter.label)}" title="${escapeHtml(quarter.label)}"><span>${escapeHtml(shortLabel)}</span><b>${quarter.count}</b></a>`;
    }).join("") : yearGroup.quarters.map(({ label, count }) => {
      const shortLabel = (label.includes("/") ? label.split("/").pop().trim() : label).replace(/番$/, "");
      return `<a href="#quarter-${encodeURIComponent(label)}" title="${escapeHtml(label)}"><span>${escapeHtml(shortLabel)}</span><b>${count}</b></a>`;
    }).join("");
    return `<section class="quarter-year-card${isYear ? "" : " special"}"><div class="quarter-year-heading"><strong>${escapeHtml(title)}</strong><span>${yearGroup.total}</span></div><div class="quarter-year-items">${quarterSlots}</div></section>`;
  }).join("");
  const liveMarkup = liveItems.length ? `<section class="panel quarter-group"><div class="library-heading"><div><span class="eyebrow">LIVE ACTION · ${liveItems.length}</span><h3>真人剧集</h3></div><span class="library-type">真人</span></div><div class="show-grid">${liveItems.map((item) => renderShowCard(item, serverId)).join("")}</div></section>` : "";
  content.innerHTML = `<div class="quarter-layout"><aside class="quarter-directory"><div class="quarter-directory-title">季度目录</div>${directoryMarkup}</aside><div class="quarter-groups">${liveMarkup}${Object.entries(visibleGroups).map(([label, items]) => `<section id="quarter-${encodeURIComponent(label)}" class="panel quarter-group"><div class="library-heading"><div><span class="eyebrow">ANIMATION QUARTER · ${items.length}</span><h3>${escapeHtml(label)}</h3></div><span class="library-type">季度</span></div><div class="show-grid">${items.map((item) => renderShowCard(item, serverId, item.season)).join("")}</div></section>`).join("")}</div></div>`;
  content.querySelectorAll(".media-recheck").forEach((button) => button.addEventListener("click", handleMediaRecheck));
  content.querySelectorAll(".media-detail-link").forEach((button) => button.addEventListener("click", () => openMediaDetail(button.dataset.ratingKey, button.dataset.libraryId, button.dataset.mediaType)));
  bindMediaKindToggles();
  bindSeasonToggles();
  loadVisibleQuarterDetails(serverId, groups);
}

function saveMediaListState() {
  sessionStorage.setItem("clp-media-library-state", JSON.stringify({
    serverId: $("#media-library-server")?.value || "",
    libraryId: state.mediaLibrarySelectedId,
    pageSize: state.mediaLibraryPageSize,
    page: state.mediaLibraryPage,
    sort: state.mediaLibrarySort,
    direction: state.mediaLibraryDirection,
  }));
}

function openMediaDetail(ratingKey, libraryId, mediaType) {
  saveMediaListState();
  const url = `/media-library/detail?server_id=${encodeURIComponent($("#media-library-server").value)}&library_id=${encodeURIComponent(libraryId)}&rating_key=${encodeURIComponent(ratingKey)}&media_type=${encodeURIComponent(mediaType)}`;
  window.location.href = url;
}

async function loadMediaDetailFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const serverId = params.get("server_id") || "";
  const libraryId = params.get("library_id") || "";
  const ratingKey = params.get("rating_key") || "";
  if (!serverId || !libraryId || !ratingKey) {
    $("#media-detail-content").innerHTML = '<div class="panel media-empty">缺少媒体详情参数。</div>';
    return;
  }
  try {
    const item = await api(`/api/servers/${encodeURIComponent(serverId)}/media-library/item?library_id=${encodeURIComponent(libraryId)}&rating_key=${encodeURIComponent(ratingKey)}&media_type=${encodeURIComponent(params.get("media_type") || "")}`);
    const isShow = item.type === "show" || Array.isArray(item.seasons);
    $("#media-detail-content").innerHTML = renderMediaDetail(item, serverId, libraryId, isShow);
    $("#media-detail-back").onclick = () => {
      const saved = JSON.parse(sessionStorage.getItem("clp-media-library-state") || "{}");
      window.location.href = `/?view=media-library&server_id=${encodeURIComponent(saved.serverId || serverId)}`;
    };
  } catch (error) {
    $("#media-detail-content").innerHTML = `<div class="panel error">${escapeHtml(error.message)}</div>`;
  }
}

function renderMediaDetail(item, serverId, libraryId, isShow) {
  const hero = `<div class="media-detail-hero">${item.thumb ? `<img src="${mediaImageUrl(serverId, item.thumb)}" alt="">` : '<div class="show-fallback">媒</div>'}<div><span class="eyebrow">MEDIA DETAIL</span><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.original_title || "")}</p><div class="meta">${escapeHtml(item.year || "未知年份")} · ${escapeHtml(item.genre || "未分类")} · ${escapeHtml(item.content_rating || "未分级")}</div></div></div>`;
  const facts = ["rating", "audience_rating", "duration", "added_at", "release_date", "rating_key"].map((key) => `<div class="detail-fact"><span>${escapeHtml(key)}</span><strong>${escapeHtml(item[key] ?? "-")}</strong></div>`).join("");
  const seasons = isShow ? (item.seasons || []).map((season) => `<div class="season-block"><div class="season-heading"><strong>${season.season === 0 ? "S00 · 特别篇" : `S${String(season.season).padStart(2, "0")}`}</strong><span>${escapeHtml(season.bucket)} · ${escapeHtml(season.release_date || "日期未知")}</span></div><div class="episode-grid">${season.episodes.map((episode) => `<div class="episode-box"><strong>E${String(episode.episode).padStart(2, "0")}</strong><span>${escapeHtml(episode.title)}</span><small>${escapeHtml(episode.air_date || "待定")}</small></div>`).join("")}</div></div>`).join("") : "";
  return `<article class="panel media-detail-panel">${hero}<div class="detail-facts">${facts}</div><div class="detail-summary">${escapeHtml(item.summary || "暂无简介")}</div>${seasons ? `<div class="season-list">${seasons}</div>` : ""}</article>`;
}

async function loadVisibleQuarterDetails(serverId, groups) {
  const cards = Array.from(document.querySelectorAll("[data-show-card]"));
  for (const card of cards) {
    const group = groups.find((item) => item.items.some((entry) => String(entry.rating_key) === String(card.dataset.ratingKey)));
    const entry = group?.items.find((item) => String(item.rating_key) === String(card.dataset.ratingKey));
    if (!entry) continue;
    const cardKey = card.dataset.showCard;
    try {
      const fresh = await api(`/api/servers/${serverId}/media-library/item?library_id=${encodeURIComponent(entry.library_id)}&rating_key=${encodeURIComponent(entry.rating_key)}&media_type=show`);
      card.outerHTML = renderShowCard(fresh, serverId, entry.season);
      const replacement = document.querySelector(`[data-show-card="${CSS.escape(cardKey)}"]`);
      replacement?.querySelector(".media-recheck")?.addEventListener("click", handleMediaRecheck);
      replacement?.querySelector(".media-detail-link")?.addEventListener("click", () => openMediaDetail(fresh.rating_key, entry.library_id, "show"));
      bindMediaKindToggles();
      bindSeasonToggles();
    } catch (_) {
      card.querySelector(".show-details-loading")?.replaceWith(Object.assign(document.createElement("div"), { className: "show-details-loading error", textContent: "剧集详情读取失败，可点击“重新检查”重试。" }));
    }
  }
}

function sortMediaItems(items) {
  const direction = state.mediaLibraryDirection === "desc" ? -1 : 1;
  const field = state.mediaLibrarySort || "name";
  const value = (item) => {
    const raw = item[field] ?? item.title ?? "";
    if (["year", "added_at", "rating", "audience_rating", "duration", "season_count", "episode_count"].includes(field)) return Number(raw) || 0;
    return String(raw).toLocaleLowerCase();
  };
  return [...items].sort((a, b) => {
    const av = value(a), bv = value(b);
    if (av < bv) return -1 * direction;
    if (av > bv) return 1 * direction;
    return String(a.rating_key || "").localeCompare(String(b.rating_key || ""));
  });
}

function rerenderSelectedMediaLibrary(serverId) {
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
  if (library?.kind === "show" && state.mediaLibraryQuarterGroups) {
    renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems || []);
  } else {
    renderMediaLibraryPage(serverId);
  }
}

async function handleMediaRecheck(event) {
  const button = event.currentTarget;
  const serverId = $("#media-library-server").value;
  button.disabled = true;
  button.textContent = "检查中…";
  try {
    const fresh = await api(`/api/servers/${serverId}/media-library/recheck`, { method: "POST", body: JSON.stringify({ library_id: Number(button.dataset.libraryId), rating_key: button.dataset.ratingKey }) });
    const card = button.closest(".show-card");
    if (card) {
      card.outerHTML = renderShowCard(fresh, serverId);
      const replacement = document.querySelector(`.media-recheck[data-rating-key="${CSS.escape(fresh.rating_key)}"]`);
      replacement?.addEventListener("click", handleMediaRecheck);
      bindMediaKindToggles();
      bindSeasonToggles();
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = "重新检查";
    setMessage("#media-library-message", error.message, true);
  }
}

function renderShowCard(show, serverId, focusSeason = null) {
  const seasons = show.seasons || [];
  const episodeCount = seasons.reduce((sum, season) => sum + season.episodes.length, 0);
  const marks = JSON.parse(localStorage.getItem("clp-show-kinds") || "{}");
  const showKind = marks[show.rating_key] || show.show_kind || "真人";
  const seasonMarkup = seasons.length ? seasons.map((season) => {
    const currentSeason = focusSeason !== null && Number(focusSeason) === Number(season.season);
    const missingEpisodes = season.episodes.filter((episode) => episode.missing && episodeAvailability(episode) === "媒体文件缺失");
    const pendingEpisodes = season.episodes.filter((episode) => episode.missing && episodeAvailability(episode) === "待发布");
    const missingSeason = missingEpisodes.length > 0;
    const seasonStatus = [missingSeason ? `缺失 ${missingEpisodes.length} 集` : "", pendingEpisodes.length ? `待发布 ${pendingEpisodes.length} 集` : ""].filter(Boolean).join(" · ");
    return `<details class="season-block ${currentSeason ? "current-season" : "other-season"} ${missingSeason ? "has-missing" : ""}" data-season-block="${season.season}" open><summary class="season-heading"><strong>${season.season === 0 ? "S00 · 特别篇" : `S${String(season.season).padStart(2, "0")}`}</strong><span>${escapeHtml(season.bucket)} · ${escapeHtml(season.release_date || "日期未知")}${seasonStatus ? ` · ${seasonStatus}` : ""}</span></summary><div class="episode-grid">${season.episodes.map((episode) => {
      const availability = episodeAvailability(episode);
      const episodeClass = [availability === "媒体文件缺失" ? "episode-missing" : "", availability === "待发布" ? "episode-pending" : "", !currentSeason ? "other-episode" : ""].filter(Boolean).join(" ");
      return `<div class="episode-box ${episodeClass}"><strong>E${String(episode.episode).padStart(2, "0")}</strong><span>${escapeHtml(episode.title || "未命名")}</span><small>${escapeHtml(episode.air_date || "待定")}${availability ? ` · ${availability}` : ""}</small></div>`;
    }).join("")}</div></details>`;
  }).join("") : '<div class="show-details-loading">正在读取季和集的详细信息…</div>';
  const cardKey = `${show.rating_key}${focusSeason === null ? "" : `-${focusSeason}`}`;
  return `<article class="show-card" data-show-card="${escapeHtml(cardKey)}" data-rating-key="${escapeHtml(show.rating_key)}"><div class="show-card-head">${show.thumb ? `<img src="${mediaImageUrl(serverId, show.thumb)}" alt="">` : '<div class="show-fallback">剧</div>'}<div class="show-card-title"><div class="show-title-line"><h4>${escapeHtml(show.title)}</h4><button class="show-kind ${showKind === "动画" ? "anime" : "live"} kind-toggle" type="button" data-rating-key="${escapeHtml(show.rating_key)}">${escapeHtml(showKind)}</button></div><p>${escapeHtml(show.original_title || "")}</p><span class="meta">${escapeHtml(show.year || "未知年份")} · ${seasons.length ? `${seasons.length} 季 · ${episodeCount} 集` : "详情读取中"}${show.genre ? ` · ${escapeHtml(show.genre)}` : ""}</span></div><div class="media-card-actions"><button class="small media-recheck" type="button" data-library-id="${show.library_id}" data-rating-key="${escapeHtml(show.rating_key)}">重新检查</button><button class="small media-detail-link" type="button" data-library-id="${show.library_id}" data-rating-key="${escapeHtml(show.rating_key)}" data-media-type="show">查看详情</button></div></div><div class="season-list">${seasonMarkup}</div></article>`;
}

function episodeAvailability(episode) {
  if (!episode?.missing) return "";
  const airDate = String(episode.air_date || "").slice(0, 10);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return airDate && airDate > today ? "待发布" : "媒体文件缺失";
}

function bindSeasonToggles() {
  document.querySelectorAll("[data-season-block]").forEach((season) => season.addEventListener("toggle", () => {
    const card = season.closest(".show-card");
    if (card) card.dataset.expandedSeason = season.open ? season.dataset.seasonBlock : "";
  }));
}

function bindMediaKindToggles() {
  document.querySelectorAll(".kind-toggle").forEach((button) => button.addEventListener("click", () => {
    const marks = JSON.parse(localStorage.getItem("clp-show-kinds") || "{}");
    marks[button.dataset.ratingKey] = button.textContent.trim() === "动画" ? "真人" : "动画";
    localStorage.setItem("clp-show-kinds", JSON.stringify(marks));
    const label = marks[button.dataset.ratingKey];
    button.textContent = label;
    button.classList.toggle("anime", label === "动画");
    button.classList.toggle("live", label !== "动画");
  }));
}

async function loadVisibleShowDetails(serverId) {
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
  if (!library || library.kind !== "show") return;
  const cards = Array.from(document.querySelectorAll("[data-show-card]"));
  for (let index = 0; index < cards.length; index += 1) {
    const card = cards[index];
    const show = library.items.find((item) => String(item.rating_key) === String(card.dataset.showCard));
    if (!show || show.seasons?.length) continue;
    setMessage("#media-library-message", `正在读取剧集详情 ${index + 1}/${cards.length}…`);
    try {
      const fresh = await api(`/api/servers/${serverId}/media-library/item?library_id=${encodeURIComponent(library.id)}&rating_key=${encodeURIComponent(show.rating_key)}&media_type=show`);
      Object.assign(show, fresh);
      card.outerHTML = renderShowCard(fresh, serverId);
      document.querySelector(`[data-show-card="${CSS.escape(fresh.rating_key)}"] .media-recheck`)?.addEventListener("click", handleMediaRecheck);
      bindMediaKindToggles();
    } catch (_) {
      card.querySelector(".show-details-loading")?.replaceWith(Object.assign(document.createElement("div"), { className: "show-details-loading error", textContent: "剧集详情读取失败，可点击“重新检查”重试。" }));
    }
  }
  setMessage("#media-library-message", "当前媒体库页面已更新");
}

async function loadMediaLibrary() {
  const saved = JSON.parse(sessionStorage.getItem("clp-media-library-state") || "{}");
  if (saved.serverId && $("#media-library-server")?.querySelector(`option[value="${CSS.escape(String(saved.serverId))}"]`)) {
    $("#media-library-server").value = String(saved.serverId);
  }
  const serverId = $("#media-library-server").value;
  if (!serverId) {
    $("#media-library-content").innerHTML = '<div class="panel media-empty">请先配置并选择 Plex 服务器。</div>';
    return;
  }
  setMessage("#media-library-message", "正在读取媒体库缓存…");
  try {
    if (saved.serverId && String(saved.serverId) === String(serverId)) {
      state.mediaLibrarySelectedId = saved.libraryId || state.mediaLibrarySelectedId;
      state.mediaLibraryPageSize = saved.pageSize || state.mediaLibraryPageSize;
      state.mediaLibraryPage = Number(saved.page || state.mediaLibraryPage);
      state.mediaLibrarySort = saved.sort || state.mediaLibrarySort;
      state.mediaLibraryDirection = saved.direction || state.mediaLibraryDirection;
    }
    const result = await api(`/api/servers/${serverId}/media-library`);
    renderMediaLibrary(result.libraries || [], serverId);
    const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
    if (library?.is_animation && !saved.sort) {
      state.mediaLibrarySort = "first_episode_date";
      state.mediaLibraryDirection = "desc";
    }
    if (library) {
      const cached = await api(`/api/servers/${serverId}/media-library/${library.id}/items`);
      library.items = cached.items || [];
      renderMediaLibraryPage(serverId);
    }
    if (library?.kind === "show" && library.is_animation) {
      const quarter = await api(`/api/servers/${serverId}/media-library/${library.id}/quarter-index`);
      state.mediaLibraryQuarterGroups = quarter.groups || [];
    renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, quarter.live_items || []);
    } else {
      await loadVisibleShowDetails(serverId);
    }
    setMessage("#media-library-message", `已加载数据库缓存 · ${result.generated_at || ""}`);
    startMediaLibrarySyncPolling(serverId);
  } catch (error) {
    setMessage("#media-library-message", error.message, true);
  }
}

function startMediaLibrarySyncPolling(serverId) {
  if (state.mediaLibrarySyncTimer) window.clearInterval(state.mediaLibrarySyncTimer);
  const hasActive = () => (state.mediaLibraryData || []).some((library) => ["queued", "running"].includes(library.sync_status));
  if (!hasActive()) return;
  state.mediaLibrarySyncTimer = window.setInterval(async () => {
    try {
      const result = await api(`/api/servers/${serverId}/media-library`);
      const previousId = state.mediaLibrarySelectedId;
      state.mediaLibraryData = result.libraries || [];
      const selected = state.mediaLibraryData.find((library) => String(library.id) === String(previousId));
      if (selected) {
        const status = $("#media-library-sync-status");
        if (status) status.textContent = `${selected.plex_synced_at ? `Plex：${selected.plex_synced_at}` : "尚未从 Plex 同步"}${selected.tmdb_synced_at ? ` · TMDB：${selected.tmdb_synced_at}` : ""}${selected.sync_status && selected.sync_status !== "idle" ? ` · 状态：${selected.sync_status}` : ""}${selected.sync_error ? ` · ${selected.sync_error}` : ""}`;
        renderMediaLibrarySyncProgress(selected);
      }
      if (!hasActive()) {
        window.clearInterval(state.mediaLibrarySyncTimer);
        state.mediaLibrarySyncTimer = null;
        await loadMediaLibrary();
      }
    } catch (_) {}
  }, 5000);
}

$("#media-library-refresh")?.addEventListener("click", async () => {
  const serverId = $("#media-library-server")?.value;
  const libraryId = state.mediaLibrarySelectedId;
  if (!serverId || !libraryId) return;
  try { const result = await api(`/api/servers/${serverId}/media-library/${libraryId}/refresh`, { method: "POST", body: "{}" }); setMessage("#media-library-message", `媒体服务器同步任务已创建：${result.id}`); await loadMediaLibrary(); } catch (error) { setMessage("#media-library-message", error.message, true); }
});
$("#media-library-tmdb-refresh")?.addEventListener("click", async () => {
  const serverId = $("#media-library-server")?.value;
  const libraryId = state.mediaLibrarySelectedId;
  if (!serverId || !libraryId) return;
  try { const result = await api(`/api/servers/${serverId}/media-library/${libraryId}/tmdb-refresh`, { method: "POST", body: "{}" }); setMessage("#media-library-message", `TMDB 同步任务已创建：${result.id}`); await loadMediaLibrary(); } catch (error) { setMessage("#media-library-message", error.message, true); }
});
$("#media-library-server")?.addEventListener("change", () => { state.mediaLibrarySelectedId = null; state.mediaLibraryPage = 1; loadMediaLibrary(); });
$("#media-library-picker")?.addEventListener("change", () => selectMediaLibrary($("#media-library-picker").value, $("#media-library-server").value));
$("#media-library-animation-mode")?.addEventListener("change", async () => {
  const serverId = $("#media-library-server")?.value;
  const libraryId = state.mediaLibrarySelectedId;
  if (!serverId || !libraryId) return;
  try { await api(`/api/servers/${serverId}/libraries/${libraryId}/settings`, { method: "PUT", body: JSON.stringify({ animation_mode: $("#media-library-animation-mode").value }) }); await loadMediaLibrary(); } catch (error) { setMessage("#media-library-message", error.message, true); }
});
$("#media-library-page-size")?.addEventListener("change", () => { state.mediaLibraryPageSize = $("#media-library-page-size").value; state.mediaLibraryPage = 1; rerenderSelectedMediaLibrary($("#media-library-server").value); });
$("#media-library-sort")?.addEventListener("change", () => { state.mediaLibrarySort = $("#media-library-sort").value; state.mediaLibraryPage = 1; rerenderSelectedMediaLibrary($("#media-library-server").value); });
$("#media-library-sort-direction")?.addEventListener("click", () => { state.mediaLibraryDirection = state.mediaLibraryDirection === "asc" ? "desc" : "asc"; $("#media-library-sort-direction").textContent = state.mediaLibraryDirection === "asc" ? "升序" : "降序"; state.mediaLibraryPage = 1; rerenderSelectedMediaLibrary($("#media-library-server").value); });

function fillServerForm(server) {
  const form = $("#server-form");
  form.id.value = server.id;
  form.name.value = server.name;
  form.address.value = server.address;
  form.token.value = "";
  form.token.placeholder = server.token_configured ? "已配置，留空保持不变" : "X-Plex-Token";
  form.pinyin_mode.value = server.pinyin_mode;
  form.skip_libraries.value = server.skip_libraries || "";
  form.enabled.checked = Boolean(server.enabled);
  $("#server-library-list").innerHTML = "";
}

$("#plex-oauth-start").addEventListener("click", async () => {
  const panel = $("#oauth-panel");
  panel.hidden = false;
  $("#oauth-server-list").innerHTML = "";
  $("#oauth-message").textContent = "正在创建 Plex 授权请求...";
  try {
    const result = await api("/api/plex/oauth/start", { method: "POST", body: "{}" });
    $("#oauth-message").innerHTML = `授权码：<strong>${escapeHtml(result.code)}</strong>。已打开 Plex 授权页面，完成后请回到这里。`;
    window.open(result.auth_url, "_blank", "noopener");
    pollOAuth(result.flow_id);
  } catch (error) {
    $("#oauth-message").textContent = error.message;
  }
});

function pollOAuth(flowId) {
  if (state.oauthPollTimer) clearInterval(state.oauthPollTimer);
  state.oauthPollTimer = setInterval(async () => {
    try {
      const result = await api(`/api/plex/oauth/status/${flowId}`);
      if (!result.claimed) return;
      clearInterval(state.oauthPollTimer);
      state.oauthPollTimer = null;
      $("#oauth-message").textContent = "授权成功，请选择要导入的 Plex 服务器。";
      renderOAuthServers(result.servers || [], result.flow_id);
    } catch (error) {
      clearInterval(state.oauthPollTimer);
      state.oauthPollTimer = null;
      $("#oauth-message").textContent = error.message;
    }
  }, 2500);
}

function renderOAuthServers(servers, flowId) {
  const list = $("#oauth-server-list");
  list.innerHTML = "";
  if (!servers.length) {
    list.innerHTML = '<div class="item">未发现可导入的 Plex 服务器。</div>';
    return;
  }
  servers.forEach((server) => {
    const connection = server.best_connection || (server.connections || [])[0] || {};
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <strong>${escapeHtml(server.name)}</strong>
      <div class="meta">${escapeHtml(connection.uri || "无可用连接")}</div>
      <button type="button" ${connection.uri ? "" : "disabled"}>导入</button>
    `;
    item.querySelector("button").addEventListener("click", async () => {
      await api("/api/plex/oauth/import-server", {
        method: "POST",
        body: JSON.stringify({
          flow_id: flowId,
          resource_index: server.resource_index,
          connection_uri: connection.uri,
        }),
      });
      $("#oauth-message").textContent = "服务器已导入。";
      await loadServers();
    });
    list.appendChild(item);
  });
}

$("#new-server").addEventListener("click", () => {
  $("#server-form").reset();
  $("#server-form").id.value = "";
  $("#server-form").enabled.checked = true;
  setMessage("#server-message", "");
});

$("#server-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = form.enabled.checked;
  const id = data.id;
  delete data.id;
  try {
    await api(id ? `/api/servers/${id}` : "/api/servers", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(data),
    });
    setMessage("#server-message", "已保存。");
    await loadServers();
    await loadOverview();
  } catch (error) {
    setMessage("#server-message", error.message, true);
  }
});

$("#test-server").addEventListener("click", async () => {
  const id = $("#server-form").id.value;
  if (!id) return setMessage("#server-message", "请先保存服务器。", true);
  try {
    const result = await api(`/api/servers/${id}/test`, { method: "POST", body: "{}" });
    setMessage("#server-message", `连接成功：${result.friendly_name}`);
  } catch (error) {
    setMessage("#server-message", error.message, true);
  }
});

$("#delete-server").addEventListener("click", async () => {
  const id = $("#server-form").id.value;
  if (!id) return;
  if (!confirm("删除该服务器及关联任务记录？")) return;
  await api(`/api/servers/${id}`, { method: "DELETE" });
  $("#server-form").reset();
  await refreshAll();
});

$("#copy-server-webhook").addEventListener("click", async () => {
  const id = $("#server-form").id.value;
  if (!id) return setMessage("#server-message", "请先选择服务器。", true);
  const server = state.servers.find((item) => String(item.id) === String(id));
  if (!server) return;
  await navigator.clipboard.writeText(server.webhook_url);
  setMessage("#server-message", "Webhook URL 已复制。");
});

$("#rotate-server-webhook").addEventListener("click", async () => {
  const id = $("#server-form").id.value;
  if (!id || !confirm("轮换后，Plex 中现有的 Webhook URL 将立即失效。继续？")) return;
  const result = await api(`/api/servers/${id}/webhook-secret/rotate`, { method: "POST", body: "{}" });
  await navigator.clipboard.writeText(result.webhook_url);
  setMessage("#server-message", "Webhook 密钥已轮换，新 URL 已复制。");
  await loadServers();
});

$("#load-server-libraries").addEventListener("click", async () => {
  const id = $("#server-form").id.value;
  if (!id) return setMessage("#server-message", "请先保存服务器。", true);
  try {
    const libraries = await api(`/api/servers/${id}/libraries`);
    const skipped = new Set(String($("#server-form").skip_libraries.value || "").split("；").map((item) => item.trim()).filter(Boolean));
    $("#server-library-list").innerHTML = libraries
      .map((library) => `<div class="server-library-setting"><label class="check"><input type="checkbox" value="${escapeHtml(library.title)}" ${skipped.has(library.title) ? "checked" : ""}> 跳过 ${escapeHtml(library.title)}</label>${Number(library.type) === 2 ? `<label class="compact-field">动画模式<select data-library-animation="${library.key}"><option value="auto" ${library.animation_mode === "auto" ? "selected" : ""}>自动</option><option value="animation" ${library.animation_mode === "animation" ? "selected" : ""}>动画</option><option value="normal" ${library.animation_mode === "normal" ? "selected" : ""}>普通</option></select></label>` : ""}</div>`)
      .join("");
    $("#server-library-list").querySelectorAll("input").forEach((input) => {
      input.addEventListener("change", () => {
        const values = $$("#server-library-list input:checked").map((item) => item.value);
        $("#server-form").skip_libraries.value = values.join("；");
      });
    });
    $("#server-library-list").querySelectorAll("[data-library-animation]").forEach((select) => select.addEventListener("change", async () => {
      try { await api(`/api/servers/${id}/libraries/${select.dataset.libraryAnimation}/settings`, { method: "PUT", body: JSON.stringify({ animation_mode: select.value }) }); setMessage("#server-message", "媒体库动画模式已保存。"); } catch (error) { setMessage("#server-message", error.message, true); }
    }));
  } catch (error) {
    setMessage("#server-message", error.message, true);
  }
});

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function renderJobs() {
  const list = $("#job-list");
  list.innerHTML = "";
  state.jobs.forEach((job) => {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <strong>#${job.id} ${escapeHtml(job.type)} · ${escapeHtml(job.status)}</strong>
      <div class="meta">${escapeHtml(job.server_name)} · ${escapeHtml(job.created_at)}</div>
      <div class="meta">${job.processed}/${job.total || "-"} · 变更 ${job.changes} · 错误 ${job.errors}</div>
      <button type="button">查看</button>
    `;
    item.querySelector("button").addEventListener("click", () => selectJob(job.id, job));
    list.appendChild(item);
  });
}

$("#localize-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const addedWithinDays = getLocalizeAddedWithinDays();
  if (addedWithinDays === false) return setMessage("#localize-message", "自定义天数必须是正整数。", true);
  const fields = [];
  if (form.field_titleSort.checked) fields.push("titleSort");
  if (form.field_genre.checked) fields.push("genre");
  if (form.field_style.checked) fields.push("style");
  if (form.field_mood.checked) fields.push("mood");
  if (form.field_collections.checked) fields.push("collections");
  const scope = {
    library_ids: splitCsv(data.library_ids),
    media_types: splitCsv(data.media_types),
    fields,
  };
  if (addedWithinDays) scope.added_within_days = addedWithinDays;
  try {
    const result = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ type: "localize", server_id: Number(data.server_id), payload: { mode: data.mode, scope } }),
    });
    setMessage("#localize-message", data.mode === "dry_run" ? "预览任务已创建，可在任务中心查看。" : "执行任务已创建，可在任务中心查看。");
    await loadJobs();
    await loadOverview();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#localize-message", error.message, true);
  }
});

$("#localize-added-filter").addEventListener("change", updateLocalizeAddedFilter);

function updateLocalizeAddedFilter() {
  $("#localize-custom-days-row").hidden = $("#localize-added-filter").value !== "custom";
}

function getLocalizeAddedWithinDays() {
  const value = $("#localize-added-filter").value;
  if (!value) return null;
  if (value !== "custom") return Number(value);
  const raw = $("#localize-custom-days").value;
  const days = Number(raw);
  if (!Number.isInteger(days) || days <= 0) return false;
  return days;
}

$("#localize-all-preset").addEventListener("click", () => {
  const form = $("#localize-form");
  form.library_ids.value = "";
  form.media_types.value = "";
  $("#localize-added-filter").value = "";
  $("#localize-custom-days").value = "";
  updateLocalizeAddedFilter();
  form.field_titleSort.checked = true;
  form.field_genre.checked = true;
  form.field_style.checked = true;
  form.field_mood.checked = true;
  form.field_collections.checked = true;
  $("#localize-library-list").innerHTML = '<div class="item">已使用全量预设：所有媒体库、所有支持字段。</div>';
});

$("#localize-load-libraries").addEventListener("click", async () => {
  const serverId = $("#localize-server").value;
  if (!serverId) return;
  try {
    const libraries = await api(`/api/servers/${serverId}/libraries`);
    renderLibraryPicker("#localize-library-list", libraries, (selected) => {
      $("#localize-form").library_ids.value = selected.join(",");
    });
  } catch (error) {
    setMessage("#localize-message", error.message, true);
  }
});

async function selectJob(jobId, initialJob = null) {
  state.selectedJob = jobId;
  state.selectedJobData = initialJob;
  state.logLines = [];
  renderLogs();
  const logDetails = $("#job-log-details");
  if (logDetails) logDetails.open = true;
  closeEventSource();
  $("#apply-preview-job").disabled = !isRunnablePreview(initialJob);
  const logs = await api(`/api/jobs/${jobId}/logs?limit=300`);
  logs.forEach(appendLog);
  $("#job-changes-list").innerHTML = "";
  const after = logs.length ? logs[logs.length - 1].id : 0;
  state.eventSource = new EventSource(`/api/jobs/${jobId}/events?after=${after}`);
  state.eventSource.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.kind === "job") updateJobProgress(payload.job);
    if (payload.kind === "log") appendLog(payload.log);
  };
}

function updateJobProgress(job) {
  state.selectedJobData = job;
  $("#job-title").textContent = `任务 #${job.id}`;
  $("#job-status").textContent = job.status;
  $("#job-library").textContent = job.current_library || job.stage || "-";
  $("#job-progress").textContent = `${job.processed}/${job.total || "-"}`;
  const progressBar = $("#job-progress-bar-fill");
  const progressDetail = $("#job-progress-detail");
  const percent = Number(job.total) > 0 ? Math.min(100, Math.round(Number(job.processed || 0) * 100 / Number(job.total))) : 0;
  if (progressBar) progressBar.style.width = `${percent}%`;
  if (progressDetail) progressDetail.textContent = `${job.stage || ""}${job.current_library ? ` · ${job.current_library}` : ""}${job.total ? ` · ${percent}%` : ""}`;
  $("#job-changes").textContent = job.changes;
  $("#job-errors").textContent = job.errors;
  $("#cancel-job").disabled = !["queued", "running"].includes(job.status);
  $("#cancel-job").textContent = job.status === "running" ? "请求取消" : "取消排队";
  $("#retry-job").disabled = !["failed", "cancelled", "interrupted"].includes(job.status);
  $("#rollback-job").disabled = !["succeeded", "failed"].includes(job.status);
  $("#apply-preview-job").disabled = !isRunnablePreview(job);
  scheduleJobListRefresh(["succeeded", "failed", "interrupted", "cancelled"].includes(job.status));
  if (["succeeded", "failed", "interrupted", "cancelled"].includes(job.status)) {
    const logDetails = $("#job-log-details");
    if (logDetails) logDetails.open = false;
  }
}

function isRunnablePreview(job) {
  if (job?.status !== "succeeded") return false;
  if (job?.type === "localize" && job?.payload?.mode === "dry_run") return true;
  if (job?.type === "continue_watching_preview") return true;
  return false;
}

function applyPreviewConfirmMessage(job) {
  if (job?.type === "continue_watching_preview") {
    return "将执行该继续观看预览中的所有候选剧集，尝试写入播放进度。继续？";
  }
  return "只执行此预览记录的精确变更；已发生变化的字段会被跳过。继续？";
}

function appendLog(log) {
  state.logLines.push(`[${log.created_at}] ${log.message}`);
  if (state.logLines.length > MAX_VISIBLE_LOG_LINES) {
    state.logLines.splice(0, state.logLines.length - MAX_VISIBLE_LOG_LINES);
  }
  renderLogs();
}

function renderLogs() {
  const el = $("#job-logs");
  el.textContent = state.logLines.join("\n");
  if (state.logLines.length) {
    el.textContent += "\n";
  }
  el.scrollTop = el.scrollHeight;
}

function closeEventSource() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function scheduleJobListRefresh(immediate = false) {
  const now = Date.now();
  if (immediate || now - state.lastJobListRefresh >= JOB_LIST_REFRESH_INTERVAL) {
    if (state.jobListRefreshTimer) {
      clearTimeout(state.jobListRefreshTimer);
      state.jobListRefreshTimer = null;
    }
    state.lastJobListRefresh = now;
    loadJobs();
    return;
  }
  if (!state.jobListRefreshTimer) {
    state.jobListRefreshTimer = setTimeout(() => {
      state.jobListRefreshTimer = null;
      state.lastJobListRefresh = Date.now();
      loadJobs();
    }, JOB_LIST_REFRESH_INTERVAL - (now - state.lastJobListRefresh));
  }
}

$("#cancel-job").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  await api(`/api/jobs/${state.selectedJob}/cancel`, { method: "POST", body: "{}" });
  await loadJobs();
});

$("#retry-job").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  const result = await api(`/api/jobs/${state.selectedJob}/retry`, { method: "POST", body: "{}" });
  await loadJobs();
  selectJob(result.id);
});

$("#refresh-jobs").addEventListener("click", loadJobs);

$("#load-changes").addEventListener("click", async () => {
  if (!state.selectedJob) return;
  const changes = await api(`/api/jobs/${state.selectedJob}/changes`);
  renderChanges(changes);
});

$("#rollback-job").addEventListener("click", async () => {
  if (!state.selectedJob || !confirm("回滚该任务记录的已应用变更？")) return;
  const result = await api(`/api/jobs/${state.selectedJob}/rollback`, { method: "POST", body: "{}" });
  await loadJobs();
  selectJob(result.id);
});

$("#apply-preview-job").addEventListener("click", async () => {
  if (!state.selectedJob || !confirm(applyPreviewConfirmMessage(state.selectedJobData))) return;
  const result = await api(`/api/jobs/${state.selectedJob}/apply-preview`, { method: "POST", body: "{}" });
  await loadJobs();
  await loadOverview();
  selectJob(result.id);
});

function renderChanges(changes) {
  const list = $("#job-changes-list");
  if (!changes.length) {
    list.innerHTML = '<div class="item">暂无变更记录。</div>';
    return;
  }
  list.innerHTML = changes
    .map(
      (change) => `
        <div class="item">
          <strong>${escapeHtml(change.title || change.rating_key)} · ${escapeHtml(change.field)}</strong>
          <div class="meta">${escapeHtml(change.media_type)} · ${escapeHtml(changeStatusText(change.status))}</div>
          <div class="meta">${escapeHtml(JSON.stringify(change.old_value))} → ${escapeHtml(JSON.stringify(change.new_value))}</div>
        </div>`
    )
    .join("");
}

function changeStatusText(status) {
  return {
    pending: "待应用",
    applied: "已应用",
    conflict: "冲突，已跳过",
    rolled_back: "已回滚",
  }[status] || "待应用";
}

$("#maintenance-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    const result = await api("/api/maintenance/jobs", {
      method: "POST",
      body: JSON.stringify({
        server_id: Number(data.server_id),
        action: data.action,
        library_id: data.library_id ? Number(data.library_id) : undefined,
        rating_key: data.rating_key || undefined,
      }),
    });
    setMessage("#maintenance-message", "维护任务已加入队列。");
    await loadJobs();
    await loadOverview();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#maintenance-message", error.message, true);
  }
});

$("#maintenance-load-libraries").addEventListener("click", async () => {
  const serverId = $("#maintenance-server").value;
  if (!serverId) return;
  try {
    const libraries = await api(`/api/servers/${serverId}/libraries`);
    renderLibraryPicker("#maintenance-library-list", libraries, (selected) => {
      $("#maintenance-form").library_id.value = selected[0] || "";
    }, { single: true });
  } catch (error) {
    setMessage("#maintenance-message", error.message, true);
  }
});

async function loadSchedules() {
  state.schedules = await api("/api/schedules");
  renderSchedules();
}

$("#schedule-mode").addEventListener("change", updateScheduleMode);
["schedule-time", "schedule-weekday", "schedule-day", "schedule-interval-number", "schedule-interval-unit", "schedule-cron"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", updateSchedulePreview);
});

function updateScheduleMode() {
  const mode = $("#schedule-mode").value;
  $("#schedule-time-fields").hidden = mode === "interval" || mode === "cron";
  $("#schedule-weekly-fields").hidden = mode !== "weekly";
  $("#schedule-monthly-fields").hidden = mode !== "monthly";
  $("#schedule-interval-fields").hidden = mode !== "interval";
  $("#schedule-cron-fields").hidden = mode !== "cron";
  updateSchedulePreview();
}

function buildScheduleValue() {
  const mode = $("#schedule-mode").value;
  if (mode === "interval") {
    const number = Math.max(1, Number($("#schedule-interval-number").value || 1));
    const unit = $("#schedule-interval-unit").value;
    return { schedule_type: "interval", schedule_value: String(unit === "hours" ? number * 60 : number) };
  }
  if (mode === "cron") {
    return { schedule_type: "cron", schedule_value: $("#schedule-cron").value.trim() };
  }
  const { hour, minute } = splitTime($("#schedule-time").value);
  if (mode === "weekly") {
    return { schedule_type: "cron", schedule_value: `${minute} ${hour} * * ${$("#schedule-weekday").value}` };
  }
  if (mode === "monthly") {
    return { schedule_type: "cron", schedule_value: `${minute} ${hour} ${$("#schedule-day").value || 1} * *` };
  }
  return { schedule_type: "cron", schedule_value: `${minute} ${hour} * * *` };
}

async function updateSchedulePreview() {
  const built = buildScheduleValue();
  $("#schedule-form").schedule_type.value = built.schedule_type;
  $("#schedule-form").schedule_value.value = built.schedule_value;
  try {
    const result = await api(`/api/schedules/describe?type=${encodeURIComponent(built.schedule_type)}&value=${encodeURIComponent(built.schedule_value)}`);
    $("#schedule-preview").textContent = result.label;
  } catch (error) {
    $("#schedule-preview").textContent = error.message;
  }
}

$("#schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const built = buildScheduleValue();
  data.schedule_type = built.schedule_type;
  data.schedule_value = built.schedule_value;
  data.payload = {
    type: "localize",
    payload: {
      mode: data.task_mode || "apply",
      scope: { fields: ["titleSort", "genre", "style", "mood", "collections"] },
    },
  };
  data.enabled = form.enabled.checked;
  if (data.id) data.id = Number(data.id);
  data.server_id = Number(data.server_id);
  try {
    await api("/api/schedules", { method: "POST", body: JSON.stringify(data) });
    setMessage("#schedule-message", "已保存。");
    form.reset();
    form.enabled.checked = true;
    await loadSchedules();
    await loadOverview();
  } catch (error) {
    setMessage("#schedule-message", error.message, true);
  }
});

function renderSchedules() {
  const list = $("#schedule-list");
  list.innerHTML = "";
  state.schedules.forEach((schedule) => {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <strong>${escapeHtml(schedule.name)}</strong>
      <div class="meta">${escapeHtml(schedule.server_name)} · ${escapeHtml(schedule.label || schedule.schedule_value)}</div>
      <div class="button-row"><button type="button" class="edit">编辑</button><button type="button" class="danger delete">删除</button></div>
    `;
    item.querySelector(".edit").addEventListener("click", () => fillScheduleForm(schedule));
    item.querySelector(".delete").addEventListener("click", async () => {
      await api(`/api/schedules/${schedule.id}`, { method: "DELETE" });
      await loadSchedules();
      await loadOverview();
    });
    list.appendChild(item);
  });
}

function fillScheduleForm(schedule) {
  const form = $("#schedule-form");
  form.id.value = schedule.id;
  form.name.value = schedule.name;
  form.server_id.value = schedule.server_id;
  form.schedule_type.value = schedule.schedule_type;
  form.schedule_value.value = schedule.schedule_value;
  const payload = schedule.payload && typeof schedule.payload === "object" ? schedule.payload : {};
  form.task_mode.value = payload.payload?.mode || "apply";
  applyScheduleToBuilder(schedule.schedule_type, schedule.schedule_value);
  form.enabled.checked = Boolean(schedule.enabled);
}

function applyScheduleToBuilder(type, value) {
  if (type === "interval") {
    const minutes = Number(value || 60);
    $("#schedule-mode").value = "interval";
    if (minutes % 60 === 0) {
      $("#schedule-interval-number").value = minutes / 60;
      $("#schedule-interval-unit").value = "hours";
    } else {
      $("#schedule-interval-number").value = minutes;
      $("#schedule-interval-unit").value = "minutes";
    }
    updateScheduleMode();
    return;
  }
  const parts = (value || "").split(" ");
  if (parts.length === 5) {
    const [minute, hour, day, month, weekday] = parts;
    $("#schedule-time").value = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
    if (day === "*" && month === "*" && weekday !== "*") {
      $("#schedule-mode").value = "weekly";
      $("#schedule-weekday").value = weekday;
    } else if (day !== "*" && month === "*" && weekday === "*") {
      $("#schedule-mode").value = "monthly";
      $("#schedule-day").value = day;
    } else if (day === "*" && month === "*" && weekday === "*") {
      $("#schedule-mode").value = "daily";
    } else {
      $("#schedule-mode").value = "cron";
      $("#schedule-cron").value = value;
    }
  } else {
    $("#schedule-mode").value = "cron";
    $("#schedule-cron").value = value || "";
  }
  updateScheduleMode();
}

function renderWebhooks() {
  const list = $("#webhook-list");
  list.innerHTML = "";
  state.servers.forEach((server) => {
    const url = server.webhook_url;
    const item = document.createElement("div");
    item.className = "panel";
    item.innerHTML = `<h3>${escapeHtml(server.name)}</h3><p>${escapeHtml(url)}</p><button type="button">复制</button>`;
    item.querySelector("button").addEventListener("click", () => navigator.clipboard.writeText(url));
    list.appendChild(item);
  });
}

function renderLibraryPicker(selector, libraries, onChange, options = {}) {
  const container = $(selector);
  if (!libraries.length) {
    container.innerHTML = '<div class="item">未读取到媒体库。</div>';
    onChange([]);
    return;
  }
  container.innerHTML = libraries
    .map(
      (library) =>
        `<label class="check"><input type="${options.single ? "radio" : "checkbox"}" name="${selector.replace(/[^a-z0-9]/gi, "")}" value="${library.key}"> ${escapeHtml(library.title)}</label>`
    )
    .join("");
  const update = () => {
    const selected = Array.from(container.querySelectorAll("input:checked")).map((input) => input.value);
    onChange(selected);
  };
  container.querySelectorAll("input").forEach((input) => input.addEventListener("change", update));
  update();
}

async function loadCollectionRules() {
  try {
    state.collectionRules = await api("/api/collection-rules");
    renderCollectionRules();
  } catch (_) {}
}

$("#collection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.server_id = Number(data.server_id);
  data.library_id = Number(data.library_id);
  data.enabled = form.enabled.checked;
  const id = data.id;
  delete data.id;
  try {
    await api(id ? `/api/collection-rules/${id}` : "/api/collection-rules", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(data),
    });
    setMessage("#collection-message", "合集规则已保存。");
    form.reset();
    form.enabled.checked = true;
    await loadCollectionRules();
  } catch (error) {
    setMessage("#collection-message", error.message, true);
  }
});

function renderCollectionRules() {
  const list = $("#collection-list");
  list.innerHTML = "";
  state.collectionRules.forEach((rule) => {
    const item = document.createElement("div");
    item.className = "item";
    item.innerHTML = `
      <strong>${escapeHtml(rule.name)}</strong>
      <div class="meta">${escapeHtml(rule.server_name)} · 库 ${rule.library_id} · ${escapeHtml(rule.match_field)} 包含 ${escapeHtml(rule.match_value)}</div>
      <div class="meta">报告：${escapeHtml(rule.collection_title)} · ${rule.enabled ? "启用" : "停用"}</div>
      <div class="button-row"><button class="edit" type="button">编辑</button><button class="danger delete" type="button">删除</button></div>
    `;
    item.querySelector(".edit").addEventListener("click", () => fillCollectionForm(rule));
    item.querySelector(".delete").addEventListener("click", async () => {
      await api(`/api/collection-rules/${rule.id}`, { method: "DELETE" });
      await loadCollectionRules();
    });
    list.appendChild(item);
  });
}

function fillCollectionForm(rule) {
  const form = $("#collection-form");
  form.id.value = rule.id;
  form.name.value = rule.name;
  form.server_id.value = rule.server_id;
  form.library_id.value = rule.library_id;
  form.match_field.value = rule.match_field;
  form.match_value.value = rule.match_value;
  form.collection_title.value = rule.collection_title;
  form.enabled.checked = Boolean(rule.enabled);
}

$("#preview-collection").addEventListener("click", async () => {
  const id = $("#collection-form").id.value;
  if (!id) return setMessage("#collection-message", "请先保存规则。", true);
  try {
    const result = await api(`/api/collection-rules/${id}/preview`, { method: "POST", body: "{}" });
    setMessage("#collection-message", "预览任务已加入队列，可在任务中心查看命中日志。");
    await loadJobs();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#collection-message", error.message, true);
  }
});

$("#run-collection").addEventListener("click", async () => {
  const id = $("#collection-form").id.value;
  if (!id) return setMessage("#collection-message", "请先保存规则。", true);
  const result = await api(`/api/collection-rules/${id}/run`, { method: "POST", body: JSON.stringify({ preview: false }) });
  await loadJobs();
  await loadOverview();
  goToJobs(result.id);
});

$("#continue-watching-load-libraries").addEventListener("click", async () => {
  const serverId = $("#continue-watching-server").value;
  if (!serverId) return;
  try {
    const libraries = (await api(`/api/servers/${serverId}/libraries`)).filter((library) => Number(library.type) === 2);
    renderLibraryPicker(
      "#continue-watching-library-list",
      libraries,
      (selected) => {
        $("#continue-watching-form").library_id.value = selected[0] || "";
      },
      { single: true }
    );
  } catch (error) {
    setMessage("#continue-watching-message", error.message, true);
  }
});

$("#continue-watching-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.library_id) return setMessage("#continue-watching-message", "请选择电视剧库。", true);
  try {
    const result = await api("/api/continue-watching/preview", {
      method: "POST",
      body: JSON.stringify({ server_id: Number(data.server_id), library_id: Number(data.library_id) }),
    });
    setMessage("#continue-watching-message", "继续观看候选预览任务已创建。");
    await loadJobs();
    await loadOverview();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#continue-watching-message", error.message, true);
  }
});

async function loadContinueWatchingRuns() {
  try {
    state.continueWatchingRuns = await api("/api/continue-watching/runs");
    renderContinueWatchingRuns();
  } catch (_) {}
}

function renderContinueWatchingRuns() {
  const list = $("#continue-watching-runs");
  list.innerHTML = state.continueWatchingRuns.length
    ? state.continueWatchingRuns
        .map(
          (run) => `
            <div class="item">
              <strong>#${run.id} ${escapeHtml(run.mode)} · ${escapeHtml(run.status)}</strong>
              <div class="meta">${escapeHtml(run.server_name)} · 候选 ${run.candidate_count} · 成功 ${run.applied_count} · 失败 ${run.error_count}</div>
              <div class="button-row">
                <button type="button" class="view-run" data-id="${run.id}">查看候选</button>
                ${run.job_id ? `<button type="button" class="view-job" data-id="${run.job_id}">任务</button>` : ""}
              </div>
            </div>`
        )
        .join("")
    : '<div class="item">暂无继续观看记录。</div>';
  list.querySelectorAll(".view-run").forEach((button) => button.addEventListener("click", () => selectContinueWatchingRun(Number(button.dataset.id))));
  list.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => goToJobs(Number(button.dataset.id))));
}

async function selectContinueWatchingRun(runId) {
  state.selectedContinueWatchingRun = runId;
  state.continueWatchingItems = await api(`/api/continue-watching/runs/${runId}/items`);
  renderContinueWatchingItems();
}

function renderContinueWatchingItems() {
  const list = $("#continue-watching-items");
  list.innerHTML = state.continueWatchingItems.length
    ? state.continueWatchingItems
        .map((item) => {
          const label = `S${String(item.season).padStart(2, "0")}E${String(item.episode).padStart(2, "0")}`;
          return `
            <div class="item continue-item">
              <label class="check"><input type="checkbox" value="${item.id}" ${item.status === "candidate" ? "checked" : ""} ${item.status === "candidate" ? "" : "disabled"}> <strong>${escapeHtml(item.show_title)} · ${label}</strong></label>
              <div class="meta">${escapeHtml(item.episode_title)} · 计划进度 ${Math.round((item.planned_offset || 0) / 1000)} 秒 · 当前进度 ${Math.round((item.current_offset || 0) / 1000)} 秒 · ${escapeHtml(item.status)}</div>
              ${item.result ? `<div class="meta">${escapeHtml(item.result)}</div>` : ""}
            </div>`;
        })
        .join("")
    : '<div class="item">暂无候选。预览任务完成后点击历史记录中的“查看候选”。</div>';
}

$("#continue-watching-apply").addEventListener("click", async () => {
  const itemIds = $$("#continue-watching-items input[type='checkbox']:checked").map((input) => Number(input.value));
  if (!itemIds.length) return setMessage("#continue-watching-message", "请选择要执行的候选剧集。", true);
  if (!confirm("将为选中的剧集写入少量播放进度，尝试加入 Plex 继续观看。继续？")) return;
  try {
    const result = await api("/api/continue-watching/apply", {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    });
    setMessage("#continue-watching-message", "继续观看执行任务已创建。");
    await loadJobs();
    await loadOverview();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#continue-watching-message", error.message, true);
  }
});

async function loadEpisodeAuditSettings() {
  try {
    const settings = await api("/api/settings/tmdb");
    $("#tmdb-key-status").textContent = settings.configured ? "已保存" : "未配置";
    if ($("#system-tmdb-key-status")) $("#system-tmdb-key-status").textContent = settings.configured ? "已保存" : "未配置";
  } catch (_) {}
}

$("#system-tmdb-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/settings/tmdb", { method: "POST", body: JSON.stringify({ api_key: $("#system-tmdb-api-key").value }) });
    $("#system-tmdb-api-key").value = "";
    $("#system-tmdb-key-status").textContent = "已保存";
    setMessage("#system-tmdb-message", "TMDB API Key 已保存。");
  } catch (error) { setMessage("#system-tmdb-message", error.message, true); }
});

$("#save-tmdb-key").addEventListener("click", async () => {
  try {
    await api("/api/settings/tmdb", {
      method: "POST",
      body: JSON.stringify({ api_key: $("#tmdb-api-key").value }),
    });
    $("#tmdb-api-key").value = "";
    $("#tmdb-key-status").textContent = "已保存";
    setMessage("#episode-audit-message", "TMDB API Key 已保存。");
  } catch (error) {
    setMessage("#episode-audit-message", error.message, true);
  }
});

$("#episode-audit-load-libraries").addEventListener("click", async () => {
  const serverId = $("#episode-audit-server").value;
  if (!serverId) return;
  try {
    const libraries = (await api(`/api/servers/${serverId}/libraries`)).filter((library) => Number(library.type) === 2);
    renderLibraryPicker(
      "#episode-audit-library-list",
      libraries,
      (selected) => {
        $("#episode-audit-form").library_id.value = selected[0] || "";
      },
      { single: true }
    );
  } catch (error) {
    setMessage("#episode-audit-message", error.message, true);
  }
});

$("#episode-audit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.library_id) return setMessage("#episode-audit-message", "请选择电视剧库。", true);
  try {
    const result = await api("/api/episode-audits", {
      method: "POST",
      body: JSON.stringify({
        server_id: Number(data.server_id),
        library_id: Number(data.library_id),
        options: {
          ignore_specials: form.ignore_specials.checked,
          ignore_future: form.ignore_future.checked,
          only_ended: form.only_ended.checked,
        },
      }),
    });
    setMessage("#episode-audit-message", "缺集检查任务已创建，可在任务中心查看进度。");
    await loadJobs();
    await loadOverview();
    goToJobs(result.id);
  } catch (error) {
    setMessage("#episode-audit-message", error.message, true);
  }
});

async function loadEpisodeAuditRuns() {
  try {
    state.episodeAuditRuns = await api("/api/episode-audits");
    renderEpisodeAuditRuns();
  } catch (_) {}
}

function renderEpisodeAuditRuns() {
  const list = $("#episode-audit-runs");
  list.innerHTML = state.episodeAuditRuns.length
    ? state.episodeAuditRuns
        .map(
          (run) => `
            <div class="item">
              <strong>#${run.id} ${escapeHtml(run.status)} · 库 ${run.library_id}</strong>
              <div class="meta">${escapeHtml(run.server_name)} · 已检查 ${run.checked_shows}/${run.total_shows} · 缺失 ${run.missing_count} · 已忽略 ${run.ignored_count || 0} · 未匹配 ${run.unmatched_count}</div>
              <div class="button-row">
                <button type="button" class="view-run" data-id="${run.id}">查看结果</button>
                ${run.job_id ? `<button type="button" class="view-job" data-id="${run.job_id}">任务</button>` : ""}
              </div>
            </div>`
        )
        .join("")
    : '<div class="item">暂无缺集检查记录。</div>';
  list.querySelectorAll(".view-run").forEach((button) => button.addEventListener("click", () => selectEpisodeAuditRun(Number(button.dataset.id))));
  list.querySelectorAll(".view-job").forEach((button) => button.addEventListener("click", () => goToJobs(Number(button.dataset.id))));
}

async function selectEpisodeAuditRun(runId) {
  state.selectedEpisodeAudit = runId;
  const run = state.episodeAuditRuns.find((item) => item.id === runId) || (await api(`/api/episode-audits/${runId}`));
  $("#episode-audit-summary").textContent = `#${run.id} · 缺失 ${run.missing_count} 集 · 已忽略 ${run.ignored_count || 0} 项 · 未匹配 ${run.unmatched_count} 部 · 低置信度 ${run.ambiguous_count} 部`;
  await loadEpisodeAuditReport();
  await loadEpisodeAuditItems();
}

async function loadEpisodeAuditReport() {
  if (!state.selectedEpisodeAudit) return;
  state.episodeAuditReport = await api(`/api/episode-audits/${state.selectedEpisodeAudit}/report`);
  renderEpisodeAuditReport();
}

async function loadEpisodeAuditItems() {
  if (!state.selectedEpisodeAudit) return;
  const params = new URLSearchParams();
  const status = $("#episode-audit-status-filter").value;
  const q = $("#episode-audit-search").value.trim();
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  state.episodeAuditItems = await api(`/api/episode-audits/${state.selectedEpisodeAudit}/items?${params.toString()}`);
  renderEpisodeAuditItems();
}

$("#episode-audit-status-filter").addEventListener("change", loadEpisodeAuditItems);
["episode-audit-only-missing", "episode-audit-show-complete", "episode-audit-show-ignored"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", () => {
    renderEpisodeAuditReport();
    loadEpisodeAuditItems();
  });
});
$("#episode-audit-search").addEventListener("input", () => {
  clearTimeout(state.episodeAuditSearchTimer);
  state.episodeAuditSearchTimer = setTimeout(() => {
    renderEpisodeAuditReport();
    loadEpisodeAuditItems();
  }, 250);
});

function renderEpisodeAuditItems() {
  const list = $("#episode-audit-items");
  list.innerHTML = state.episodeAuditItems.length
    ? state.episodeAuditItems
        .map((item) => {
          const details = item.details || {};
          const episodeLabel = item.season ? `S${String(item.season).padStart(2, "0")}E${String(item.episode).padStart(2, "0")}` : "-";
          return `
            <div class="item">
              <strong>${escapeHtml(item.show_title)} · ${escapeHtml(episodeLabel)}</strong>
              <div class="meta">${escapeHtml(item.status)} · ${escapeHtml(item.match_source || "未匹配")} · TMDB ${escapeHtml(item.tmdb_id || "-")} ${escapeHtml(item.tmdb_title || "")}</div>
              <div class="meta">${escapeHtml(details.episode_title || "")}${item.air_date ? ` · ${escapeHtml(item.air_date)}` : ""}${details.error ? ` · ${escapeHtml(details.error)}` : ""}</div>
              ${item.status === "missing" ? `<div class="button-row"><button type="button" class="ignore-episode" data-id="${item.id}">忽略这一集</button><button type="button" class="ignore-show" data-id="${item.id}">忽略整部剧</button></div>` : ""}
            </div>`;
        })
        .join("")
    : '<div class="item">当前筛选下没有结果。</div>';
  bindEpisodeAuditIgnoreButtons(list);
}

function renderEpisodeAuditReport() {
  const container = $("#episode-audit-report");
  const report = state.episodeAuditReport;
  if (!report) {
    container.innerHTML = "";
    return;
  }
  const summary = report.summary || {};
  const groups = filterEpisodeAuditGroups(report.groups || []);
  $("#episode-audit-summary").textContent = `#${report.run.id} · ${summary.shows_with_missing} 部剧有缺失 · 已有 ${summary.present_episodes || 0} 集 · 缺 ${summary.missing_episodes} 集 · 已忽略 ${summary.ignored_items} 项 · 未匹配 ${summary.unmatched_shows} 部`;
  container.innerHTML = `
    ${summary.legacy_summary_only ? '<div class="notice">这是旧版报告，只包含缺失/忽略摘要；重新扫描后可显示完整绿色格子。</div>' : ""}
    <div class="metric-grid audit-metrics">
      <div class="metric"><span>有缺失的剧</span><strong>${summary.shows_with_missing || 0}</strong></div>
      <div class="metric"><span>已有集数</span><strong>${summary.present_episodes || 0}</strong></div>
      <div class="metric"><span>缺失集数</span><strong>${summary.missing_episodes || 0}</strong></div>
      <div class="metric"><span>已忽略</span><strong>${summary.ignored_items || 0}</strong></div>
      <div class="metric"><span>未匹配</span><strong>${summary.unmatched_shows || 0}</strong></div>
    </div>
    ${
      groups.length
        ? groups.map(renderEpisodeAuditGroup).join("")
        : '<div class="item">当前筛选下没有需要处理的缺集报表。</div>'
    }
  `;
  bindEpisodeAuditIgnoreButtons(container);
  bindEpisodeMatchOverrideButtons(container);
}

function filterEpisodeAuditGroups(groups) {
  const query = ($("#episode-audit-search").value || "").toLowerCase();
  const onlyMissing = $("#episode-audit-only-missing")?.checked;
  const showComplete = $("#episode-audit-show-complete")?.checked;
  return groups.filter((group) => {
    const matchesQuery = !query || group.show_title.toLowerCase().includes(query) || String(group.tmdb_title || "").toLowerCase().includes(query);
    if (!matchesQuery) return false;
    if (onlyMissing && !showComplete && !group.missing_count && !group.unmatched && !group.ambiguous) return false;
    if (!showComplete && !group.missing_count && !group.ignored_count && !group.unmatched && !group.ambiguous) return false;
    return true;
  });
}

function renderEpisodeAuditGroup(group) {
  const matrixEpisodes = (group.seasons || []).flatMap((season) => season.episodes || []);
  const missing = matrixEpisodes.filter((episode) => episode.status === "missing").concat(group.episodes || []);
  const ignored = matrixEpisodes.filter((episode) => episode.status?.startsWith("ignored")).concat(group.ignored || []);
  const presentCount = group.present_count || matrixEpisodes.filter((episode) => episode.status === "present").length;
  const showIgnored = $("#episode-audit-show-ignored")?.checked;
  const badges = [];
  if (group.unmatched) badges.push("未匹配");
  if (group.ambiguous) badges.push("低置信度");
  return `
    <div class="item audit-group">
      <div class="audit-group-head">
        <div>
          <strong>${escapeHtml(group.show_title)}</strong>
          <div class="meta">TMDB ${escapeHtml(group.tmdb_id || "-")} ${escapeHtml(group.tmdb_title || "")} · ${escapeHtml(group.match_source || "未匹配")} ${badges.length ? `· ${badges.map(escapeHtml).join(" · ")}` : ""}</div>
        </div>
        <div class="audit-counts">已有 ${presentCount} · 缺 ${group.missing_count} · 忽略 ${group.ignored_count}</div>
      </div>
      ${renderEpisodeCalendar(group, showIgnored)}
      ${
        group.ambiguous && (group.candidates || []).length
          ? `<div class="button-row match-override" data-rating-key="${escapeHtml(group.plex_rating_key)}">
              <select>${group.candidates
                .map(
                  (candidate) =>
                    `<option value="${candidate.id}">${escapeHtml(candidate.name)} ${escapeHtml(candidate.first_air_date || "")}</option>`
                )
                .join("")}</select>
              <button type="button" class="save-match-override">使用此 TMDB 匹配</button>
            </div>`
          : ""
      }
      ${
        ignored.length && showIgnored
          ? `<div class="meta">已忽略：${ignored.map((episode) => `${escapeHtml(episode.label)}${episode.reason ? `（${escapeHtml(episode.reason)}）` : ""}`).join("、")}</div>`
          : ""
      }
      ${missing.length || group.unmatched || group.ambiguous ? `<div class="button-row"><button type="button" class="ignore-show" data-id="${missing[0]?.id || ignored[0]?.id || group.representative_item_id || ""}" ${missing[0]?.id || ignored[0]?.id || group.representative_item_id ? "" : "disabled"}>忽略整部剧</button></div>` : ""}
    </div>`;
}

function renderEpisodeCalendar(group, showIgnored) {
  const seasons = group.seasons || [];
  if (!seasons.length) {
    const fallback = group.episodes || [];
    if (!fallback.length) return '<div class="meta">这份旧报告没有完整剧集矩阵，重新扫描后会显示绿色/红色格子。</div>';
    return `<div class="episode-chip-row">${fallback.map((episode) => renderEpisodeCell(episode, showIgnored)).join("")}</div>`;
  }
  return `
    <div class="episode-calendar">
      ${seasons
        .map((season) => {
          const episodes = (season.episodes || []).filter((episode) => showIgnored || !String(episode.status || "").startsWith("ignored"));
          if (!episodes.length) return "";
          return `
            <div class="episode-season-row">
              <div class="season-label">S${String(season.season).padStart(2, "0")}</div>
              <div class="episode-cell-row">${episodes.map((episode) => renderEpisodeCell(episode, showIgnored)).join("")}</div>
            </div>`;
        })
        .join("")}
    </div>`;
}

function renderEpisodeCell(episode, showIgnored) {
  const status = episode.status || "missing";
  if (!showIgnored && status.startsWith("ignored")) return "";
  const title = [episode.label, episode.title, episode.air_date, statusText(status), episode.reason].filter(Boolean).join(" · ");
  const canIgnore = status === "missing";
  return `
    <button
      type="button"
      class="episode-cell status-${escapeHtml(status)} ${canIgnore ? "ignore-episode" : ""}"
      data-id="${episode.id || episode.item_id || ""}"
      title="${escapeHtml(title)}"
      ${canIgnore ? "" : "disabled"}
    >
      ${escapeHtml(episode.label)}
    </button>`;
}

function statusText(status) {
  const labels = {
    present: "已有",
    missing: "缺失",
    ignored_missing: "已忽略缺失",
    ignored_show: "已忽略整部剧",
    unmatched_show: "未匹配",
    ambiguous_match: "低置信度",
  };
  return labels[status] || status;
}

function bindEpisodeAuditIgnoreButtons(container) {
  container.querySelectorAll(".ignore-episode").forEach((button) => {
    button.addEventListener("click", () => ignoreEpisodeAuditItem(Number(button.dataset.id), "episode"));
  });
  container.querySelectorAll(".ignore-show").forEach((button) => {
    button.addEventListener("click", () => ignoreEpisodeAuditItem(Number(button.dataset.id), "show"));
  });
}

function bindEpisodeMatchOverrideButtons(container) {
  container.querySelectorAll(".save-match-override").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest(".match-override");
      const run = state.episodeAuditReport?.run;
      if (!row || !run) return;
      await api("/api/episode-match-overrides", {
        method: "PUT",
        body: JSON.stringify({
          server_id: run.server_id,
          library_id: run.library_id,
          plex_rating_key: row.dataset.ratingKey,
          tmdb_id: Number(row.querySelector("select").value),
        }),
      });
      button.textContent = "已保存，下次检查生效";
      button.disabled = true;
    });
  });
}

async function ignoreEpisodeAuditItem(itemId, scope) {
  if (!itemId) return;
  const reason = prompt(scope === "show" ? "忽略整部剧的原因（可留空）" : "忽略这一集的原因（可留空）", "");
  if (reason === null) return;
  await api("/api/episode-audit-ignores/from-item", {
    method: "POST",
    body: JSON.stringify({ item_id: itemId, scope, reason }),
  });
  setMessage("#episode-audit-message", scope === "show" ? "已忽略整部剧。" : "已忽略这一集。");
  await loadEpisodeAuditRuns();
  await loadEpisodeAuditReport();
  await loadEpisodeAuditItems();
}

$("#export-episode-audit-json").addEventListener("click", () => {
  downloadText(`episode-audit-${state.selectedEpisodeAudit || "results"}.json`, JSON.stringify(state.episodeAuditReport || state.episodeAuditItems, null, 2), "application/json");
});

$("#export-episode-audit-csv").addEventListener("click", () => {
  const rows = [["show_title", "status", "match_source", "tmdb_id", "tmdb_title", "season", "episode", "label", "air_date", "episode_title"]];
  const groups = state.episodeAuditReport?.groups || [];
  groups.forEach((group) => {
    (group.seasons || []).forEach((season) => {
      (season.episodes || []).forEach((episode) => {
        rows.push([
          group.show_title,
          episode.status,
          group.match_source,
          group.tmdb_id || "",
          group.tmdb_title || "",
          episode.season || "",
          episode.episode || "",
          episode.label || "",
          episode.air_date || "",
          episode.title || "",
        ]);
      });
    });
  });
  if (rows.length === 1) {
    state.episodeAuditItems.forEach((item) => {
      rows.push([
        item.show_title,
        item.status,
        item.match_source,
        item.tmdb_id || "",
        item.tmdb_title || "",
        item.season || "",
        item.episode || "",
        item.season ? `S${String(item.season).padStart(2, "0")}E${String(item.episode).padStart(2, "0")}` : "",
        item.air_date || "",
        (item.details || {}).episode_title || "",
      ]);
    });
  }
  const csv = rows.map((row) => row.map(csvCell).join(",")).join("\n");
  downloadText(`episode-audit-${state.selectedEpisodeAudit || "results"}.csv`, csv, "text/csv;charset=utf-8");
});

async function loadWebhookData() {
  try {
    const [events, rules] = await Promise.all([api("/api/webhook-events"), api("/api/webhook-rules")]);
    state.webhookEvents = events;
    state.webhookRules = rules;
    renderWebhookEvents();
  } catch (_) {}
}

$("#webhook-rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.server_id = Number(data.server_id);
  data.enabled = form.enabled.checked;
  if (data.id) data.id = Number(data.id);
  try {
  await api("/api/webhook-rules", { method: "POST", body: JSON.stringify(data) });
    setMessage("#webhook-rule-message", "Webhook 规则已保存。");
    form.reset();
    form.enabled.checked = true;
    await loadWebhookData();
    await loadOverview();
  } catch (error) {
    setMessage("#webhook-rule-message", error.message, true);
  }
});

function renderWebhookEvents() {
  const list = $("#webhook-events");
  list.innerHTML = state.webhookEvents.length
    ? state.webhookEvents
        .map((event) => `<div class="item"><strong>${escapeHtml(event.event)}</strong><div class="meta">${escapeHtml(event.server_name)} · ${escapeHtml(event.summary)} · ${escapeHtml(event.created_at)}</div></div>`)
        .join("")
    : '<div class="item">暂无 Webhook 事件。</div>';
}

$("#run-diagnostics").addEventListener("click", loadDiagnostics);

$("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  try {
    await api("/api/auth/password", { method: "PUT", body: JSON.stringify(data) });
    form.reset();
    setMessage("#password-message", "密码已修改。");
  } catch (error) {
    setMessage("#password-message", error.message, true);
  }
});

async function loadDiagnostics() {
  const diagnostics = await api("/api/diagnostics");
  $("#diagnostics-list").innerHTML = diagnostics
    .map(
      (item) => `
        <div class="panel">
          <h3>${escapeHtml(item.server_name || item.server_id)} · ${escapeHtml(item.status)}</h3>
          <div class="list">
            ${item.checks.map((check) => `<div class="item"><strong>${escapeHtml(check.name)} · ${escapeHtml(check.status)}</strong><div class="meta">${escapeHtml(check.message)}</div></div>`).join("")}
          </div>
        </div>`
    )
    .join("");
}

async function loadTags() {
  if (!state.authenticated && !$("#app").hidden) return;
  try {
    const tags = await api("/api/tags");
    state.tags = Object.entries(tags)
      .map(([source, target]) => ({ source, target }))
      .sort((a, b) => a.source.localeCompare(b.source));
    state.tagsDirty = false;
    renderTags();
    await loadTagSuggestions();
  } catch (_) {}
}

$("#save-tags").addEventListener("click", async () => {
  try {
    const tags = tagsToObject();
    await api("/api/tags", { method: "POST", body: JSON.stringify(tags) });
    state.tagsDirty = false;
    setMessage("#tags-message", "已保存。");
  } catch (error) {
    setMessage("#tags-message", error.message, true);
  }
});

$("#add-tag").addEventListener("click", () => {
  state.tags.unshift({ source: "", target: "" });
  state.tagsDirty = true;
  renderTags();
});

$("#tag-search").addEventListener("input", renderTags);

$("#toggle-tags-json").addEventListener("click", () => {
  const editor = $("#tags-editor");
  if (editor.hidden) {
    editor.value = JSON.stringify(tagsToObject(false), null, 2);
    editor.hidden = false;
  } else {
    try {
      const parsed = JSON.parse(editor.value || "{}");
      state.tags = Object.entries(parsed).map(([source, target]) => ({ source, target: String(target) }));
      state.tagsDirty = true;
      editor.hidden = true;
      renderTags();
    } catch (error) {
      setMessage("#tags-message", error.message, true);
    }
  }
});

$("#export-tags-json").addEventListener("click", () => {
  $("#tags-editor").hidden = false;
  $("#tags-editor").value = JSON.stringify(tagsToObject(false), null, 2);
});

$("#scan-tag-suggestions").addEventListener("click", async () => {
  const serverId = $("#tag-suggestion-server").value;
  if (!serverId) return;
  const result = await api("/api/tag-suggestions/scan", {
    method: "POST",
    body: JSON.stringify({ server_id: Number(serverId) }),
  });
  setMessage("#tags-message", "标签扫描任务已创建，可在任务中心查看。");
  await loadJobs();
  await loadOverview();
  goToJobs(result.id);
});

async function loadTagSuggestions() {
  try {
    const serverId = $("#tag-suggestion-server").value;
    state.tagSuggestions = await api(`/api/tag-suggestions${serverId ? `?server_id=${encodeURIComponent(serverId)}` : ""}`);
    renderTagSuggestions();
  } catch (_) {}
}

$("#tag-suggestion-server").addEventListener("change", loadTagSuggestions);

function renderTagSuggestions() {
  const list = $("#tag-suggestions");
  list.innerHTML = state.tagSuggestions.length
    ? state.tagSuggestions
        .slice(0, 80)
        .map(
          (item) => `
            <div class="item suggestion" data-source="${escapeHtml(item.source)}" data-suggested="${escapeHtml(item.suggested)}">
              <label class="check"><input type="checkbox"> <strong>${escapeHtml(item.source)}</strong></label>
              <div class="meta">${escapeHtml(item.field)} · 出现 ${item.count} 次</div>
              <input class="suggested-target" value="${escapeHtml(item.suggested)}">
            </div>`
        )
        .join("")
    : '<div class="item">暂无推荐。扫描后会显示未映射标签。</div>';
}

$("#add-selected-suggestions").addEventListener("click", async () => {
  const entries = $$("#tag-suggestions .suggestion")
    .filter((row) => row.querySelector("input[type='checkbox']").checked)
    .map((row) => ({ source: row.dataset.source, target: row.querySelector(".suggested-target").value }));
  if (!entries.length) return setMessage("#tags-message", "请选择要加入的推荐。", true);
  await api("/api/tags/bulk-add", { method: "POST", body: JSON.stringify({ entries }) });
  setMessage("#tags-message", "已加入标签映射。");
  await loadTags();
});

function renderTags() {
  const table = $("#tags-table");
  const query = ($("#tag-search").value || "").toLowerCase();
  const rows = state.tags
    .map((tag, index) => ({ ...tag, index }))
    .filter((tag) => !query || tag.source.toLowerCase().includes(query) || tag.target.toLowerCase().includes(query));
  table.innerHTML = `
    <div class="tag-row tag-head"><strong>英文标签</strong><strong>中文标签</strong><span></span></div>
    ${rows
      .map(
        (tag) => `
          <div class="tag-row" data-index="${tag.index}">
            <input class="tag-source" value="${escapeHtml(tag.source)}" placeholder="Action">
            <input class="tag-target" value="${escapeHtml(tag.target)}" placeholder="动作">
            <button type="button" class="danger small tag-delete">删除</button>
          </div>`
      )
      .join("")}
  `;
  table.querySelectorAll(".tag-row[data-index]").forEach((row) => {
    const index = Number(row.dataset.index);
    row.querySelector(".tag-source").addEventListener("input", (event) => {
      state.tags[index].source = event.target.value;
      state.tagsDirty = true;
    });
    row.querySelector(".tag-target").addEventListener("input", (event) => {
      state.tags[index].target = event.target.value;
      state.tagsDirty = true;
    });
    row.querySelector(".tag-delete").addEventListener("click", () => {
      state.tags.splice(index, 1);
      state.tagsDirty = true;
      renderTags();
    });
  });
  setMessage("#tags-message", state.tagsDirty ? "有未保存的标签修改。" : "");
}

function tagsToObject(validate = true) {
  const output = {};
  const seen = new Set();
  for (const tag of state.tags) {
    const source = (tag.source || "").trim();
    const target = (tag.target || "").trim();
    if (!source && !target) continue;
    if (validate && (!source || !target)) throw new Error("英文标签和中文标签都不能为空。");
    if (validate && seen.has(source)) throw new Error(`英文标签重复：${source}`);
    seen.add(source);
    if (source) output[source] = target;
  }
  return output;
}

async function loadNotifications() {
  try {
    state.notifications = await api("/api/notifications/channels");
    renderNotifications();
  } catch (_) {}
}

$("#notification-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = form.enabled.checked;
  data.events = [];
  if (form.event_job_succeeded.checked) data.events.push("job_succeeded");
  if (form.event_job_failed.checked) data.events.push("job_failed");
  if (form.event_webhook_event.checked) data.events.push("webhook_event");
  if (data.id) data.id = Number(data.id);
  try {
    await api("/api/notifications/channels", { method: "POST", body: JSON.stringify(data) });
    setMessage("#notification-message", "通知渠道已保存。");
    form.reset();
    form.enabled.checked = true;
    form.event_job_succeeded.checked = true;
    form.event_job_failed.checked = true;
    await loadNotifications();
    await loadOverview();
  } catch (error) {
    setMessage("#notification-message", error.message, true);
  }
});

function renderNotifications() {
  const list = $("#notification-list");
  list.innerHTML = state.notifications.length
    ? state.notifications
        .map(
          (channel) => `
            <div class="item">
              <strong>${escapeHtml(channel.name)}</strong>
              <div class="meta">${escapeHtml(channel.url)} · ${channel.enabled ? "启用" : "停用"}</div>
              <div class="meta">${escapeHtml((channel.events || []).join(", ") || "全部事件")}</div>
              <div class="button-row">
                <button type="button" class="edit" data-id="${channel.id}">编辑</button>
                <button type="button" class="test" data-id="${channel.id}">测试</button>
                <button type="button" class="danger delete" data-id="${channel.id}">删除</button>
              </div>
            </div>`
        )
        .join("")
    : '<div class="item">暂无通知渠道。</div>';
  list.querySelectorAll(".edit").forEach((button) => button.addEventListener("click", () => fillNotificationForm(Number(button.dataset.id))));
  list.querySelectorAll(".test").forEach((button) =>
    button.addEventListener("click", async () => {
      try {
        await api(`/api/notifications/channels/${button.dataset.id}/test`, { method: "POST", body: "{}" });
        setMessage("#notification-message", "测试通知已发送。");
      } catch (error) {
        setMessage("#notification-message", error.message, true);
      }
    })
  );
  list.querySelectorAll(".delete").forEach((button) =>
    button.addEventListener("click", async () => {
      await api(`/api/notifications/channels/${button.dataset.id}`, { method: "DELETE" });
      await loadNotifications();
      await loadOverview();
    })
  );
}

function fillNotificationForm(id) {
  const channel = state.notifications.find((item) => item.id === id);
  if (!channel) return;
  const form = $("#notification-form");
  form.id.value = channel.id;
  form.name.value = channel.name;
  form.url.value = channel.url;
  form.enabled.checked = Boolean(channel.enabled);
  form.event_job_succeeded.checked = channel.events.includes("job_succeeded");
  form.event_job_failed.checked = channel.events.includes("job_failed");
  form.event_webhook_event.checked = channel.events.includes("webhook_event");
}

function splitCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function downloadText(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

window.addEventListener("beforeunload", (event) => {
  if (!state.tagsDirty) return;
  event.preventDefault();
  event.returnValue = "";
});

bindOverview();
bootstrap();
updateScheduleMode();
updateLocalizeAddedFilter();

