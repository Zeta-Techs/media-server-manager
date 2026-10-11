import { api, escapeHtml, setCsrfToken } from "./api.js";
import { state } from "./state.js";
import { bindOverview, loadOverview } from "./views/overview.js";

const MAX_VISIBLE_LOG_LINES = 500;
const JOB_LIST_REFRESH_INTERVAL = 2000;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const MEDIA_SEARCH_MAX_ROWS = 100;
const MEDIA_LIBRARY_SORT_KEYS = new Set(["name", "original_title", "year", "added_at", "release_date", "rating", "audience_rating", "duration", "season_count", "episode_count", "content_rating", "genre"]);
const mediaSearchState = { rows: [], nextId: 1, version: 0 };

// Keep the state transition code safe when a browser still has an older
// cached state.js module without the newer generation field.
if (!Number.isFinite(Number(state.mediaLibrarySelectionGeneration))) {
  state.mediaLibrarySelectionGeneration = 0;
}

function createMediaSearchRow(values = {}) {
  return {
    id: mediaSearchState.nextId++,
    query: String(values.query || ""),
    media_type: values.media_type || "all",
    year: String(values.year || ""),
    status: "",
    error: false,
    results: [],
    requestVersion: 0,
  };
}

function mediaSearchRowById(id) {
  return mediaSearchState.rows.find((row) => String(row.id) === String(id));
}

function showAuth() {
  $("#auth-screen").hidden = false;
  $("#app").hidden = true;
  $("#auth-copy").textContent = state.initialized ? "登录 Media Server Manager 管理端。" : "创建首个本地管理员账号。";
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
  const requestedView = new URLSearchParams(window.location.search).get("view") || state.preferences.active_view || "overview";
  const allowedViews = new Set(["overview", "servers", "media-library", "media-library-detail", "localization", "jobs", "automation", "system"]);
  const savedView = window.location.pathname === "/media-library/detail" ? "media-library-detail" : (allowedViews.has(requestedView) ? requestedView : "overview");
  await switchView(savedView);
}

async function refreshInitial() {
  await Promise.all([loadOverview(), loadServers(), loadJobs(), loadPreferences()]);
  state.loadedViews.add("overview");
  state.loadedViews.add("servers");
  state.loadedViews.add("jobs");
}

async function loadPreferences() { state.preferences = await api("/api/preferences"); }
let preferenceSaveTimer = null;
function savePreferences() {
  if (preferenceSaveTimer) window.clearTimeout(preferenceSaveTimer);
  preferenceSaveTimer = window.setTimeout(async () => {
    try { await api("/api/preferences", { method: "PUT", body: JSON.stringify(state.preferences) }); } catch (_) {}
  }, 250);
}

async function refreshAll() {
  await Promise.all([loadOverview(), loadServers(), loadJobs(), loadSchedules(), loadWebhookData(), loadNotifications()]);
  await loadTmdbSettings();
  await loadTags();
  ["overview", "servers", "localization", "jobs", "automation", "system"].forEach((view) => state.loadedViews.add(view));
}

function setMessage(selector, message, isError = false) {
  const el = $(selector);
  if (!el) return;
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
  if (view !== "media-library-detail") { state.preferences.active_view = view; savePreferences(); }
  if (view !== "media-library") stopMediaLibrarySyncPolling();
  $$(".nav").forEach((item) => item.classList.toggle("active", item.dataset.view === (view === "media-library-detail" ? "media-library" : view)));
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  if (view !== "jobs") closeEventSource();
  await ensureViewData(view);
}

function stopMediaLibrarySyncPolling() {
  if (state.mediaLibrarySyncInterval) window.clearTimeout(state.mediaLibrarySyncInterval);
  state.mediaLibrarySyncInterval = null;
  state.mediaLibrarySyncTimer = null;
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
  if (view === "system") await Promise.all([loadDiagnostics(), loadTmdbSettings()]);
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
  $("#media-library-server").innerHTML = options;
  $("#webhook-rule-server").innerHTML = options;
  $("#tag-suggestion-server").innerHTML = options;
  $("#schedule-form").server_id.innerHTML = options;
}

function mediaImageUrl(serverId, path) {
  return path ? `/api/servers/${encodeURIComponent(serverId)}/media-image?path=${encodeURIComponent(path)}` : "";
}

function deferredMediaImageMarkup(source, alt, className = "") {
  if (!source) return '<span class="media-poster-fallback" aria-hidden="true">影</span>';
  const classes = ["deferred-media-image", "media-lightbox-image", className].filter(Boolean).join(" ");
  return `<img class="${classes}" data-src="${escapeHtml(source)}" data-lightbox-src="${escapeHtml(source)}" alt="${escapeHtml(alt || "封面")}" decoding="async">`;
}

function bindDeferredMediaImages() {
  state.mediaLibraryImageObserver?.disconnect();
  state.mediaLibraryImageObserver = null;
  const images = Array.from(document.querySelectorAll("img.deferred-media-image[data-src]"));
  const load = (image) => {
    if (!image || image.dataset.imageState === "loaded" || image.dataset.imageState === "failed") return;
    const source = image.dataset.src;
    if (!source) return;
    image.dataset.imageState = "loading";
    image.addEventListener("load", () => { image.dataset.imageState = "loaded"; }, { once: true });
    image.addEventListener("error", () => {
      image.dataset.imageState = "failed";
      image.classList.add("media-image-error");
      image.removeAttribute("src");
    }, { once: true });
    image.src = source;
  };
  if (!images.length) return;
  if (!("IntersectionObserver" in window)) {
    images.forEach(load);
    return;
  }
  state.mediaLibraryImageObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      state.mediaLibraryImageObserver?.unobserve(entry.target);
      load(entry.target);
    });
  }, { rootMargin: "700px 0px" });
  images.forEach((image) => state.mediaLibraryImageObserver.observe(image));
}

function openMediaImageLightbox(source, alt = "") {
  if (!source) return;
  let lightbox = document.querySelector("#media-image-lightbox");
  if (!lightbox) {
    lightbox = document.createElement("div");
    lightbox.id = "media-image-lightbox";
    lightbox.className = "image-lightbox";
    lightbox.hidden = true;
    lightbox.innerHTML = '<div class="image-lightbox-panel" role="dialog" aria-modal="true" aria-label="封面预览"><button class="image-lightbox-close" type="button" aria-label="关闭封面预览">×</button><img class="image-lightbox-image" alt=""></div>';
    document.body.appendChild(lightbox);
    lightbox.addEventListener("click", (event) => {
      if (event.target === lightbox || event.target.closest(".image-lightbox-close")) lightbox.hidden = true;
    });
  }
  const image = lightbox.querySelector(".image-lightbox-image");
  image.src = source;
  image.alt = alt;
  lightbox.hidden = false;
}

document.addEventListener("click", (event) => {
  const image = event.target.closest(".media-lightbox-image");
  if (image) openMediaImageLightbox(image.dataset.lightboxSrc || image.currentSrc || image.src, image.alt || "");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const lightbox = document.querySelector("#media-image-lightbox");
    if (lightbox) lightbox.hidden = true;
  }
});

function formatMinutes(milliseconds) {
  const minutes = Math.round(Number(milliseconds || 0) / 60000);
  return minutes ? `${minutes} 分钟` : "-";
}

function formatSyncAge(value) {
  if (!value) return "暂无记录";
  const timestamp = Date.parse(String(value).replace(" ", "T"));
  if (!Number.isFinite(timestamp)) return String(value);
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(months / 12)} 年前`;
}

function formatSyncTimestamp(label, value) {
  return `${label}：${value || "暂无记录"}${value ? `（${formatSyncAge(value)}）` : ""}`;
}

function renderMediaLibrarySyncStatus(library) {
  const parts = [
    formatSyncTimestamp("Plex", library?.plex_synced_at),
    formatSyncTimestamp("TMDB", library?.tmdb_synced_at),
  ];
  if (library?.sync_status && !["idle", "succeeded"].includes(library.sync_status)) parts.push(`状态：${library.sync_status}`);
  if (library?.sync_error) parts.push(library.sync_error);
  return parts.join(" · ");
}

function renderMediaLibraryAutoRecheck(library, serverId) {
  const field = $("#media-library-auto-recheck-field");
  const input = $("#media-library-auto-recheck");
  const help = $("#media-library-auto-recheck-help");
  const isShow = library?.kind === "show";
  field.hidden = !isShow;
  input.checked = isShow && Boolean(library.auto_recheck_new_episodes);
  help.hidden = !isShow;
  const server = state.servers.find((item) => String(item.id) === String(serverId));
  help.textContent = "自动重检需要 Plex 配置 Webhook 并发送 library.new 事件；同一电视剧连续新增集会等待 5 分钟合并，失败后重试一次。";
  if (server?.webhook_url) {
    const link = document.createElement("a");
    link.href = server.webhook_url;
    link.textContent = " 查看当前服务器 Webhook 地址";
    link.addEventListener("click", (event) => { event.preventDefault(); window.prompt("将此地址添加到 Plex Webhook 设置", server.webhook_url); });
    help.append(link);
  }
}

async function refreshMediaLibrarySyncStatus(serverId) {
  const result = await api(`/api/servers/${serverId}/media-library`);
  const selected = (result.libraries || []).find((library) => String(library.id) === String(state.mediaLibrarySelectedId));
  if (selected) {
    const status = $("#media-library-sync-status");
    if (status) status.textContent = renderMediaLibrarySyncStatus(selected);
    renderMediaLibrarySyncProgress(selected);
  }
}

function resetMediaLibraryWindow() {
  state.mediaLibraryItemsGeneration += 1;
  state.mediaLibraryItemsTotal = 0;
  state.mediaLibraryItemsLoading = false;
  state.mediaLibraryLoadingLibraryId = null;
  state.mediaLibraryDetailQueue = [];
  state.mediaLibraryDetailActive = 0;
  state.mediaLibraryDetailRequests.forEach((controller) => controller.abort());
  state.mediaLibraryDetailRequests.clear();
  if (state.mediaLibraryAbortController) state.mediaLibraryAbortController.abort();
  state.mediaLibraryAbortController = null;
  state.mediaLibraryObserver?.disconnect();
  state.mediaLibraryObserver = null;
  state.mediaLibraryImageObserver?.disconnect();
  state.mediaLibraryImageObserver = null;
  if (state.mediaLibraryScrollHandler) window.removeEventListener("scroll", state.mediaLibraryScrollHandler);
  state.mediaLibraryScrollHandler = null;
}

function mediaLibraryQuery(serverId, libraryId) {
  const params = new URLSearchParams({
    mode: "full",
    sort: state.mediaLibrarySort || "name",
    direction: state.mediaLibraryDirection || "asc",
  });
  return `/api/servers/${encodeURIComponent(serverId)}/media-library/${encodeURIComponent(libraryId)}/items?${params}`;
}

function mediaLibraryCacheKey(serverId, libraryId) {
  return `${String(serverId)}:${String(libraryId)}:${state.mediaLibrarySort || "name"}:${state.mediaLibraryDirection || "asc"}`;
}

function mediaLibraryQuarterCacheKey(serverId, libraryId) {
  return `${String(serverId)}:${String(libraryId)}`;
}

function invalidateMediaLibraryCache(serverId, libraryId = null) {
  const serverPrefix = `${String(serverId)}:`;
  if (libraryId === null || libraryId === undefined) {
    for (const key of state.mediaLibraryContentCache.keys()) {
      if (key.startsWith(serverPrefix)) state.mediaLibraryContentCache.delete(key);
    }
    for (const key of state.mediaLibraryQuarterCache.keys()) {
      if (key.startsWith(serverPrefix)) state.mediaLibraryQuarterCache.delete(key);
    }
    return;
  }
  const libraryPrefix = `${String(serverId)}:${String(libraryId)}:`;
  for (const key of state.mediaLibraryContentCache.keys()) {
    if (key.startsWith(libraryPrefix)) state.mediaLibraryContentCache.delete(key);
  }
  state.mediaLibraryQuarterCache.delete(mediaLibraryQuarterCacheKey(serverId, libraryId));
}

function applyMediaLibraryContentCache(serverId, library) {
  if (!library || (library.kind === "show" && library.is_animation)) return false;
  const cached = state.mediaLibraryContentCache.get(mediaLibraryCacheKey(serverId, library.id));
  if (!cached) return false;
  library.items = cached.items;
  library.item_count = cached.total || library.item_count;
  library._itemsLoaded = true;
  library._itemsSort = cached.sort;
  library._itemsDirection = cached.direction;
  state.mediaLibraryItemsTotal = Number(cached.total || library.items.length);
  return true;
}

function applyMediaLibraryQuarterCache(serverId, library) {
  if (!library?.is_animation) return null;
  const cached = state.mediaLibraryQuarterCache.get(mediaLibraryQuarterCacheKey(serverId, library.id));
  if (!cached) return null;
  state.mediaLibraryQuarterGroups = cached.groups;
  state.mediaLibraryLiveItems = cached.liveItems;
  return cached;
}

async function loadMediaLibraryBatch(serverId, library, selectionGeneration = state.mediaLibrarySelectionGeneration) {
  if (!library) return;
  if (
    state.mediaLibraryItemsLoading &&
    String(state.mediaLibraryLoadingLibraryId) === String(library.id) &&
    selectionGeneration === state.mediaLibrarySelectionGeneration
  ) return;
  if (state.mediaLibraryItemsLoading && state.mediaLibraryAbortController) {
    state.mediaLibraryAbortController.abort();
  }
  state.mediaLibraryItemsLoading = true;
  state.mediaLibraryLoadingLibraryId = library.id;
  const generation = state.mediaLibraryItemsGeneration;
  const controller = new AbortController();
  state.mediaLibraryAbortController = controller;
  try {
    const result = await api(mediaLibraryQuery(serverId, library.id), { signal: controller.signal });
    if (generation !== state.mediaLibraryItemsGeneration || selectionGeneration !== state.mediaLibrarySelectionGeneration) return;
    const incoming = result.items || [];
    library.items = incoming;
    library._itemsLoaded = true;
    state.mediaLibraryItemsTotal = Number(result.total || library.item_count || library.items.length);
    library._itemsSort = state.mediaLibrarySort;
    library._itemsDirection = state.mediaLibraryDirection;
    state.mediaLibraryContentCache.set(mediaLibraryCacheKey(serverId, library.id), {
      items: incoming,
      total: state.mediaLibraryItemsTotal,
      sort: state.mediaLibrarySort,
      direction: state.mediaLibraryDirection,
      loadedAt: Date.now(),
    });
    renderMediaLibraryPage(serverId);
  } catch (error) {
    if (error.name !== "AbortError") setMessage("#media-library-message", error.message, true);
  } finally {
    if (state.mediaLibraryAbortController === controller) {
      state.mediaLibraryAbortController = null;
      state.mediaLibraryItemsLoading = false;
      state.mediaLibraryLoadingLibraryId = null;
    }
  }
}

function observeMediaLibraryNodes(serverId) {
  state.mediaLibraryObserver?.disconnect();
  if (state.mediaLibraryScrollHandler) window.removeEventListener("scroll", state.mediaLibraryScrollHandler);
  state.mediaLibraryScrollHandler = null;
  const root = $("#media-library-content");
  if (!root || !("IntersectionObserver" in window)) {
    Array.from(root?.querySelectorAll("[data-show-card]") || []).slice(0, 8).forEach((node) => queueMediaLibraryDetail(serverId, node));
    bindDeferredMediaImages();
    return;
  }
  state.mediaLibraryObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const node = entry.target;
      state.mediaLibraryObserver.unobserve(node);
      if (node.dataset.showCard) queueMediaLibraryDetail(serverId, node);
    }
  }, { rootMargin: "700px 0px" });
  root.querySelectorAll("[data-show-card]").forEach((node) => {
    if (node.querySelector(".show-details-loading")) state.mediaLibraryObserver.observe(node);
  });
  bindDeferredMediaImages();
}

function queueMediaLibraryDetail(serverId, card) {
  if (!card || state.mediaLibraryDetailRequests.has(card.dataset.ratingKey)) return;
  state.mediaLibraryDetailQueue.push({ serverId, card });
  drainMediaLibraryDetailQueue();
}

async function drainMediaLibraryDetailQueue() {
  while (state.mediaLibraryDetailActive < 4 && state.mediaLibraryDetailQueue.length) {
    const { serverId, card } = state.mediaLibraryDetailQueue.shift();
    if (!card?.isConnected || state.mediaLibraryDetailRequests.has(card.dataset.ratingKey)) continue;
    const controller = new AbortController();
    const key = card.dataset.ratingKey;
    state.mediaLibraryDetailRequests.set(key, controller);
    state.mediaLibraryDetailActive += 1;
    api(`/api/servers/${encodeURIComponent(serverId)}/media-library/item?library_id=${encodeURIComponent(card.dataset.libraryId)}&rating_key=${encodeURIComponent(key)}&media_type=show`, { signal: controller.signal })
      .then((fresh) => {
        const library = state.mediaLibraryData.find((entry) => String(entry.id) === String(card.dataset.libraryId));
        const show = library?.items?.find((entry) => String(entry.rating_key) === String(fresh.rating_key));
        if (show) Object.assign(show, fresh);
        if (!card.isConnected) return;
        const focusSeason = card.dataset.showCard?.startsWith(`${fresh.rating_key}-`) ? Number(card.dataset.showCard.slice(String(fresh.rating_key).length + 1)) : null;
        card.outerHTML = renderShowCard(fresh, serverId, Number.isFinite(focusSeason) ? focusSeason : null);
        bindMediaLibraryEvents();
        bindDeferredMediaImages();
      })
      .catch((error) => {
        if (error.name !== "AbortError" && card.isConnected) {
          card.querySelector(".show-details-loading")?.replaceWith(Object.assign(document.createElement("div"), { className: "show-details-loading error", textContent: "剧集详情读取失败，可点击“重新检查”重试。" }));
        }
      })
      .finally(() => {
        state.mediaLibraryDetailRequests.delete(key);
        state.mediaLibraryDetailActive -= 1;
        drainMediaLibraryDetailQueue();
      });
  }
}

function bindMediaLibraryEvents() {
  const root = $("#media-library-content");
  if (!root || root.dataset.eventsBound === "1") return;
  root.dataset.eventsBound = "1";
  root.addEventListener("click", (event) => {
    const detail = event.target.closest(".media-detail-link");
    if (detail) openMediaDetail(detail.dataset.ratingKey, detail.dataset.libraryId, detail.dataset.mediaType);
    const recheck = event.target.closest(".media-recheck");
    if (recheck) handleMediaRecheck({ currentTarget: recheck });
    const kind = event.target.closest(".kind-toggle");
    if (kind) toggleMediaKind(kind);
  });
}

function toggleMediaKind(button) {
  const marks = state.preferences.show_kinds || (state.preferences.show_kinds = {});
  marks[button.dataset.ratingKey] = button.textContent.trim() === "动画" ? "真人" : "动画";
  savePreferences();
  const label = marks[button.dataset.ratingKey];
  button.textContent = label;
  button.classList.toggle("anime", label === "动画");
  button.classList.toggle("live", label !== "动画");
}

function renderMediaLibrarySummary(libraries) {
  const totalMovies = libraries.filter((library) => library.kind === "movie").reduce((sum, library) => sum + Number(library.item_count || library.items?.length || 0), 0);
  const totalShows = libraries.filter((library) => library.kind === "show").reduce((sum, library) => sum + Number(library.item_count || library.items?.length || 0), 0);
  const totalCollections = libraries.reduce((sum, library) => sum + (library.collections || []).length, 0);
  const totalEpisodes = libraries.filter((library) => library.kind === "show").reduce((sum, library) => sum + Number(library.episode_count || library.items?.reduce((n, show) => n + (show.episode_count || (show.seasons || []).reduce((m, season) => m + (season.episodes || []).length, 0)), 0) || 0), 0);
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
  renderMediaLibraryAutoRecheck(syncLibrary, serverId);
  if (syncLibrary) {
    state.mediaLibrarySort = MEDIA_LIBRARY_SORT_KEYS.has(syncLibrary.sort_key) ? syncLibrary.sort_key : state.mediaLibrarySort;
    if (!MEDIA_LIBRARY_SORT_KEYS.has(state.mediaLibrarySort)) state.mediaLibrarySort = "name";
    state.mediaLibraryDirection = syncLibrary.sort_direction || state.mediaLibraryDirection;
    if (sort) sort.value = state.mediaLibrarySort;
    if (sortDirection) sortDirection.textContent = state.mediaLibraryDirection === "asc" ? "升序" : "降序";
    const syncStatus = $("#media-library-sync-status");
    if (syncStatus) syncStatus.textContent = renderMediaLibrarySyncStatus(syncLibrary);
    renderMediaLibrarySyncProgress(syncLibrary);
  }
  $("#media-library-tabs").innerHTML = libraries.map((library) => `<button type="button" draggable="true" class="media-library-tab ${String(library.id) === String(state.mediaLibrarySelectedId) ? "active" : ""}" data-library-tab="${library.id}">${escapeHtml(library.title)}<span>${library.item_count || library.items?.length || 0}</span></button>`).join("");
  let draggedLibrary = null;
  $("#media-library-tabs").querySelectorAll("[data-library-tab]").forEach((button) => {
    button.addEventListener("dragstart", () => { draggedLibrary = button; button.classList.add("dragging"); });
    button.addEventListener("dragend", () => { draggedLibrary = null; button.classList.remove("dragging"); });
    button.addEventListener("dragover", (event) => event.preventDefault());
    button.addEventListener("drop", async (event) => {
      event.preventDefault();
      if (!draggedLibrary || draggedLibrary === button) return;
      const container = $("#media-library-tabs");
      const buttons = [...container.querySelectorAll("[data-library-tab]")];
      const from = buttons.indexOf(draggedLibrary), to = buttons.indexOf(button);
      if (from < 0 || to < 0) return;
      if (from < to) button.after(draggedLibrary); else button.before(draggedLibrary);
      try {
        await api(`/api/servers/${serverId}/media-library/order`, { method: "PUT", body: JSON.stringify({ order: [...container.querySelectorAll("[data-library-tab]")].map((item) => Number(item.dataset.libraryTab)) }) });
        state.mediaLibraryData = [...container.querySelectorAll("[data-library-tab]")].map((item) => libraries.find((library) => String(library.id) === item.dataset.libraryTab)).filter(Boolean);
      } catch (error) {
        setMessage("#media-library-message", error.message, true);
        renderMediaLibrary(libraries, serverId);
      }
    });
  });
  $("#media-library-tabs").querySelectorAll("[data-library-tab]").forEach((button) => button.addEventListener("click", () => {
    state.mediaLibraryPage = 1;
    picker.value = button.dataset.libraryTab;
    $("#media-library-tabs").querySelectorAll(".media-library-tab").forEach((item) => item.classList.toggle("active", item === button));
    selectMediaLibrary(button.dataset.libraryTab, serverId);
  }));
  // Animation libraries are rendered from the quarter index below. Avoid
  // briefly showing an empty ordinary library panel while that index loads.
  if (syncLibrary?.kind === "show" && syncLibrary.is_animation) {
    $("#media-library-content").innerHTML = '<div class="panel media-empty media-library-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在读取季度目录…</span></div>';
    return;
  }
  if (syncLibrary && !syncLibrary._itemsLoaded && !(syncLibrary.items || []).length) {
    $("#media-library-content").innerHTML = '<div class="panel media-empty media-library-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在读取媒体库条目…</span></div>';
    return;
  }
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
  const previousLibraryId = state.mediaLibrarySelectedId;
  const selectionGeneration = Number(state.mediaLibrarySelectionGeneration) + 1;
  state.mediaLibrarySelectionGeneration = selectionGeneration;
  if (String(previousLibraryId) !== String(libraryId)) state.mediaLibraryQuarterSignature = "";
  resetMediaLibraryWindow();
  state.mediaLibrarySelectedId = libraryId;
  state.mediaLibraryPage = 1;
  state.mediaLibraryQuarterGroups = null;
  state.mediaLibraryLiveItems = [];
  const content = $("#media-library-content");
  if (content) content.innerHTML = '<div class="panel media-empty media-library-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在切换媒体库…</span></div>';
  const picker = $("#media-library-picker");
  if (picker) picker.value = String(libraryId);
  $("#media-library-tabs")?.querySelectorAll("[data-library-tab]").forEach((button) => {
    button.classList.toggle("active", String(button.dataset.libraryTab) === String(libraryId));
  });
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(libraryId));
  renderMediaLibraryAutoRecheck(library, serverId);
  if (!library?.is_animation && !MEDIA_LIBRARY_SORT_KEYS.has(state.mediaLibrarySort)) {
    state.mediaLibrarySort = "name";
    state.mediaLibraryDirection = "asc";
  }
  if (library?.is_animation) {
    state.mediaLibrarySort = "first_episode_date";
    state.mediaLibraryDirection = "desc";
    const sort = $("#media-library-sort");
    if (sort) sort.value = state.mediaLibrarySort;
    const direction = $("#media-library-sort-direction");
    if (direction) direction.textContent = "降序";
  }
  try {
    if (library?.kind === "show" && library.is_animation) {
      const cached = applyMediaLibraryQuarterCache(serverId, library);
      const quarter = cached || await api(`/api/servers/${serverId}/media-library/${library.id}/quarter-index`);
      if (selectionGeneration !== state.mediaLibrarySelectionGeneration || String(state.mediaLibrarySelectedId) !== String(libraryId)) return;
      state.mediaLibraryQuarterGroups = cached ? cached.groups : (quarter.groups || []);
      state.mediaLibraryLiveItems = cached ? cached.liveItems : (quarter.live_items || []);
      if (!cached) {
        state.mediaLibraryQuarterCache.set(mediaLibraryQuarterCacheKey(serverId, library.id), {
          groups: state.mediaLibraryQuarterGroups,
          liveItems: state.mediaLibraryLiveItems,
          loadedAt: Date.now(),
        });
      }
      renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems);
    } else {
      // Replace the previous library's DOM immediately. The new request may
      // take a moment, but stale quarter cards must never remain visible.
      renderMediaLibraryPage(serverId);
      if (applyMediaLibraryContentCache(serverId, library)) renderMediaLibraryPage(serverId);
      else await loadMediaLibraryBatch(serverId, library, selectionGeneration);
      if (selectionGeneration === state.mediaLibrarySelectionGeneration && library && !library._itemsLoaded) {
        // A superseded initial request may still be unwinding while this
        // selection is already current. Render the correct library shell now;
        // the guarded request will fill its rows when it completes.
        renderMediaLibraryPage(serverId);
      }
    }
  } catch (error) {
    if (selectionGeneration !== state.mediaLibrarySelectionGeneration) return;
    setMessage("#media-library-message", error.message || "媒体库加载失败", true);
    if (content) content.innerHTML = `<div class="panel media-empty error" role="alert">读取媒体库失败：${escapeHtml(error.message || "未知错误")}</div>`;
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
  const pageItems = library.items || [];
  const collectionMarkup = (library.collections || []).length ? `<div class="library-collections"><div class="section-label">合集 · ${library.collections.length}</div><div class="collection-strip">${library.collections.map((item) => `<div class="collection-chip"><strong>${escapeHtml(item.title)}</strong><span>${item.child_count || 0} 项</span></div>`).join("")}</div></div>` : "";
  const panel = (() => {
    const collectionMarkup = (library.collections || []).length ? `<div class="library-collections"><div class="section-label">合集 · ${library.collections.length}</div><div class="collection-strip">${library.collections.map((item) => `<div class="collection-chip"><strong>${escapeHtml(item.title)}</strong><span>${item.child_count || 0} 项</span></div>`).join("")}</div></div>` : "";
    if (library.kind === "movie") {
      return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">MOVIES · ${library.item_count || state.mediaLibraryItemsTotal || pageItems.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">共 ${pageItems.length} 项</span></div><span class="library-type">电影</span></div>${collectionMarkup}<div class="movie-table-wrap"><table class="movie-table"><thead><tr><th>海报</th><th>标题</th><th>年份</th><th>类型</th><th>标签</th><th>分辨率</th><th>分级</th><th>片长</th><th>操作</th></tr></thead><tbody>${pageItems.map((item) => { const resolution = item.resolution || "×"; const poster = item.thumb ? mediaImageUrl(serverId, item.thumb) : (item.poster_url || ""); return `<tr class="${resolution === "×" ? "resolution-missing" : ""}" data-rating-key="${escapeHtml(item.rating_key)}" data-library-id="${library.id}"><td>${poster ? deferredMediaImageMarkup(poster, item.title || "封面", "table-poster") : '<span class="table-poster fallback">影</span>'}</td><td><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.original_title || "")}</small></td><td>${escapeHtml(item.year || "-")}</td><td>${escapeHtml(item.plex_type || "电影")}</td><td>${escapeHtml(item.genre || (item.system_genres || []).join(", ") || "-")}</td><td class="resolution-cell">${escapeHtml(resolution)}</td><td>${escapeHtml(item.content_rating || "-")}</td><td>${formatMinutes(item.duration)}</td><td><button class="small media-detail-link" type="button" data-library-id="${library.id}" data-rating-key="${escapeHtml(item.rating_key)}" data-media-type="movie">查看详情</button></td></tr>`; }).join("")}</tbody></table></div></section>`;
    }
    if (library.kind === "show") {
      return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">SERIES · ${library.item_count || state.mediaLibraryItemsTotal || pageItems.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">共 ${pageItems.length} 项</span></div><span class="library-type">剧集</span></div>${collectionMarkup}<div class="show-grid">${pageItems.map((show) => renderShowCard(show, serverId)).join("")}</div></section>`;
    }
    return `<section class="panel library-panel"><div class="library-heading"><div><span class="eyebrow">COLLECTIONS · ${library.item_count || state.mediaLibraryItemsTotal || pageItems.length}</span><h3>${escapeHtml(library.title)}</h3><span class="meta">共 ${pageItems.length} 项</span></div><span class="library-type">合集</span></div><div class="collection-grid">${pageItems.map((item) => `<article class="collection-card">${item.thumb ? deferredMediaImageMarkup(mediaImageUrl(serverId, item.thumb), item.title || "封面") : '<div class="collection-fallback">集</div>'}<div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.summary || "暂无简介")}</span></div></article>`).join("")}</div></section>`;
  })();
  content.innerHTML = panel;
  bindMediaLibraryEvents();
  bindSeasonToggles();
  observeMediaLibraryNodes(serverId);
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
  const layoutSignature = JSON.stringify({
    live: liveItems.map((item) => String(item.rating_key || "")),
    groups: Object.entries(visibleGroups).map(([label, items]) => [label, items.map((item) => `${item.rating_key || ""}:${item.season ?? ""}`)]),
  });
  // Keep the existing quarter structure when only metadata/details were
  // refreshed. This is what prevents ordinary searches and sync polling from
  // rebuilding thousands of cards and moving the user's scroll position.
  if (state.mediaLibraryQuarterSignature === layoutSignature && content.querySelector(".quarter-layout")) {
    state.mediaLibraryQuarterGroups = groups;
    state.mediaLibraryLiveItems = liveItems;
    return;
  }
  state.mediaLibraryQuarterSignature = layoutSignature;
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
  bindMediaLibraryEvents();
  bindSeasonToggles();
  observeMediaLibraryNodes(serverId);
}

function saveMediaListState() {
  state.preferences.media_library_state = {
    serverId: $("#media-library-server")?.value || "",
    libraryId: state.mediaLibrarySelectedId,
    pageSize: state.mediaLibraryPageSize,
    page: state.mediaLibraryPage,
    sort: state.mediaLibrarySort,
    direction: state.mediaLibraryDirection,
  };
  savePreferences();
}

function openMediaDetail(ratingKey, libraryId, mediaType) {
  saveMediaListState();
  const url = `/media-library/detail?server_id=${encodeURIComponent($("#media-library-server").value)}&library_id=${encodeURIComponent(libraryId)}&rating_key=${encodeURIComponent(ratingKey)}&media_type=${encodeURIComponent(mediaType)}`;
  window.location.href = url;
}

function renderExternalIdBadges(item, mediaType = "", extraClass = "") {
  const ids = item?.external_ids || {};
  const tmdbId = item?.tmdb_id || ids.tmdb || ids.tmdb_id || "";
  const imdbId = item?.imdb_id || ids.imdb || ids.imdb_id || "";
  const tvdbId = item?.tvdb_id || ids.tvdb || ids.tvdb_id || "";
  const kind = mediaType === "movie" || item?.media_type === "movie" || item?.type === "movie" ? "movie" : "tv";
  const badges = [];
  if (tmdbId) {
    badges.push(`<span class="external-id-badge tmdb" title="在 TMDB 打开"><span class="external-id-logo" aria-hidden="true">TMDB</span><a href="https://www.themoviedb.org/${kind}/${encodeURIComponent(tmdbId)}" target="_blank" rel="noopener" aria-label="在 TMDB 打开 ${escapeHtml(tmdbId)}">${escapeHtml(tmdbId)}</a></span>`);
  }
  if (imdbId) {
    badges.push(`<span class="external-id-badge imdb" title="在 IMDb 打开"><span class="external-id-logo" aria-hidden="true">IMDb</span><a href="https://www.imdb.com/title/${encodeURIComponent(imdbId)}/" target="_blank" rel="noopener" aria-label="在 IMDb 打开 ${escapeHtml(imdbId)}">${escapeHtml(imdbId)}</a></span>`);
  }
  if (tvdbId) {
    badges.push(`<span class="external-id-badge tvdb" title="在 TVDB 打开"><span class="external-id-logo" aria-hidden="true">TVDB</span><a href="https://thetvdb.com/dereferrer/series/${encodeURIComponent(tvdbId)}" target="_blank" rel="noopener" aria-label="在 TVDB 打开 ${escapeHtml(tvdbId)}">${escapeHtml(tvdbId)}</a></span>`);
  }
  return badges.length ? `<span class="external-id-badges${extraClass ? ` ${extraClass}` : ""}">${badges.join("")}</span>` : "";
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
      const saved = state.preferences.media_library_state || {};
      window.location.href = `/?view=media-library&server_id=${encodeURIComponent(saved.serverId || serverId)}`;
    };
  } catch (error) {
    $("#media-detail-content").innerHTML = `<div class="panel error">${escapeHtml(error.message)}</div>`;
  }
}

function renderMediaDetail(item, serverId, libraryId, isShow) {
  const hero = `<div class="media-detail-hero">${item.thumb ? `<img src="${mediaImageUrl(serverId, item.thumb)}" alt="">` : '<div class="show-fallback">媒</div>'}<div><span class="eyebrow">MEDIA DETAIL</span><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.original_title || "")}</p><div class="meta">${escapeHtml(item.year || "未知年份")} · ${escapeHtml(item.genre || "未分类")} · ${escapeHtml(item.content_rating || "未分级")}</div><div class="meta media-detail-ids">${renderExternalIdBadges(item, isShow ? "tv" : "movie") || '<span class="meta">暂无外部 ID</span>'}</div></div></div>`;
  const facts = ["rating", "audience_rating", "duration", "added_at", "release_date", "rating_key", "source", "system_managed"].map((key) => `<div class="detail-fact"><span>${escapeHtml(key)}</span><strong>${escapeHtml(item[key] ?? "-")}</strong></div>`).join("");
  const seasons = isShow ? (item.seasons || []).map((season) => `<div class="season-block"><div class="season-heading"><strong>${season.season === 0 ? "S00 · 特别篇" : `S${String(season.season).padStart(2, "0")}`}</strong><span>${escapeHtml(season.bucket)} · ${escapeHtml(season.release_date || "日期未知")} · 已收录 ${season.episodes.filter((episode) => !episode.missing).length} / ${season.episodes.length}</span></div><div class="episode-grid">${season.episodes.map((episode) => `<div class="episode-box"><strong>E${String(episode.episode).padStart(2, "0")}</strong><span title="${escapeHtml(episode.title || "未命名")}">${escapeHtml(episode.title || "未命名")}</span><small>${escapeHtml(episode.air_date || "待定")}</small></div>`).join("")}</div></div>`).join("") : "";
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
      bindDeferredMediaImages();
      const replacement = document.querySelector(`[data-show-card="${CSS.escape(cardKey)}"]`);
      replacement?.querySelector(".media-recheck")?.addEventListener("click", handleMediaRecheck);
      replacement?.querySelector(".media-detail-link")?.addEventListener("click", () => openMediaDetail(fresh.rating_key, entry.library_id, "show"));
      bindMediaKindToggles();
      bindDeferredMediaImages();
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

function renderMovieTableRow(item, serverId, libraryId) {
  const resolution = item.resolution || "×";
  const poster = item.thumb ? mediaImageUrl(serverId, item.thumb) : (item.poster_url || "");
  return `<tr class="${resolution === "×" ? "resolution-missing" : ""}" data-rating-key="${escapeHtml(item.rating_key)}" data-library-id="${libraryId}"><td>${poster ? deferredMediaImageMarkup(poster, item.title || "封面", "table-poster") : '<span class="table-poster fallback">影</span>'}</td><td><strong>${escapeHtml(item.title || "")}</strong><small>${escapeHtml(item.original_title || "")}</small></td><td>${escapeHtml(item.year || "-")}</td><td>${escapeHtml(item.plex_type || "电影")}</td><td>${escapeHtml(item.genre || (item.system_genres || []).join(", ") || "-")}</td><td class="resolution-cell">${escapeHtml(resolution)}</td><td>${escapeHtml(item.content_rating || "-")}</td><td>${formatMinutes(item.duration)}</td><td><button class="small media-detail-link" type="button" data-library-id="${libraryId}" data-rating-key="${escapeHtml(item.rating_key)}" data-media-type="movie">查看详情</button></td></tr>`;
}

function refreshMediaLibraryCounts() {
  const libraries = state.mediaLibraryData || [];
  renderMediaLibrarySummary(libraries);
  const picker = $("#media-library-picker");
  if (picker) {
    const selected = picker.value;
    picker.innerHTML = libraries.map((library) => `<option value="${library.id}">${escapeHtml(library.title)} · ${library.kind === "movie" ? "电影" : library.kind === "show" ? "剧集" : "合集"} · ${library.item_count || library.items?.length || 0} 项</option>`).join("");
    picker.value = selected;
  }
  $("#media-library-tabs")?.querySelectorAll("[data-library-tab]").forEach((button) => {
    const library = libraries.find((item) => String(item.id) === String(button.dataset.libraryTab));
    const count = button.querySelector("span");
    if (library && count) count.textContent = library.item_count || library.items?.length || 0;
  });
}

function patchAnimationQuarterDirectory(groups) {
  const directory = $("#media-library-content .quarter-directory");
  if (!directory) return;
  const directoryGroups = groups.reduce((map, group) => {
    const label = String(group.label || "");
    const yearMatch = label.match(/^(\d{4})\s+/);
    const key = yearMatch ? `year-${yearMatch[1]}` : `special-${label}`;
    if (!map[key]) map[key] = { year: yearMatch ? yearMatch[1] : label, total: 0, quarters: [] };
    map[key].total += (group.items || []).length;
    map[key].quarters.push({ label, count: (group.items || []).length });
    return map;
  }, {});
  const groupKey = (label) => {
    if (label === "特别篇") return [1, 0, 0];
    if (label === "未定档") return [2, 0, 0];
    try { return [0, -Number(label.slice(0, 4)), -Number(label.split("Q", 1)[1].split(" ")[0])]; } catch (_) { return [2, 0, 0]; }
  };
  const markup = Object.values(directoryGroups).sort((a, b) => {
    const an = /^\d{4}$/.test(String(a.year)) ? Number(a.year) : -1;
    const bn = /^\d{4}$/.test(String(b.year)) ? Number(b.year) : -1;
    if (an !== bn) return bn - an;
    return String(a.year).localeCompare(String(b.year));
  }).map((yearGroup) => {
    const isYear = /^\d{4}$/.test(yearGroup.year);
    const quarters = [...yearGroup.quarters].sort((a, b) => {
      const ak = groupKey(a.label), bk = groupKey(b.label);
      return ak[0] - bk[0] || ak[1] - bk[1] || ak[2] - bk[2];
    });
    const quarterBySlot = quarters.reduce((slots, quarter) => { const match = quarter.label.match(/\bQ([1-4])\b/); if (match) slots[match[1]] = quarter; return slots; }, {});
    const slots = isYear ? ["4", "3", "2", "1"].map((slot) => {
      const quarter = quarterBySlot[slot];
      if (!quarter) return '<span class="quarter-slot empty" aria-hidden="true">&nbsp;</span>';
      const shortLabel = (quarter.label.includes("/") ? quarter.label.split("/").pop().trim() : quarter.label).replace(/番$/, "");
      return `<a href="#quarter-${encodeURIComponent(quarter.label)}" title="${escapeHtml(quarter.label)}"><span>${escapeHtml(shortLabel)}</span><b>${quarter.count}</b></a>`;
    }).join("") : quarters.map(({ label, count }) => `<a href="#quarter-${encodeURIComponent(label)}" title="${escapeHtml(label)}"><span>${escapeHtml((label.includes("/") ? label.split("/").pop().trim() : label).replace(/番$/, ""))}</span><b>${count}</b></a>`).join("");
    return `<section class="quarter-year-card${isYear ? "" : " special"}"><div class="quarter-year-heading"><strong>${escapeHtml(yearGroup.year)}</strong><span>${yearGroup.total}</span></div><div class="quarter-year-items">${slots}</div></section>`;
  }).join("");
  directory.innerHTML = `<div class="quarter-directory-title">季度目录</div>${markup}`;
}

function bindPatchedMediaItem(item) {
  const root = $("#media-library-content");
  root?.querySelectorAll(`[data-rating-key="${CSS.escape(String(item.rating_key))}"] .media-detail-link`).forEach((button) => button.addEventListener("click", () => openMediaDetail(button.dataset.ratingKey, button.dataset.libraryId, button.dataset.mediaType)));
  root?.querySelectorAll(`[data-rating-key="${CSS.escape(String(item.rating_key))}"] .media-recheck`).forEach((button) => button.addEventListener("click", handleMediaRecheck));
  bindSeasonToggles();
  bindDeferredMediaImages();
}

function patchVisibleMediaItem(item, serverId) {
  const library = state.mediaLibraryData.find((entry) => String(entry.id) === String(item.library_id));
  if (!library || String(state.mediaLibrarySelectedId) !== String(item.library_id)) return;
  if (library.kind === "movie") {
    const tbody = $("#media-library-content .movie-table tbody");
    if (!tbody) return;
    const old = tbody.querySelector(`[data-rating-key="${CSS.escape(String(item.rating_key))}"]`);
    const wrapper = document.createElement("tbody");
    wrapper.innerHTML = renderMovieTableRow(item, serverId, library.id);
    const replacement = wrapper.firstElementChild;
    if (!replacement) return;
    if (old) old.replaceWith(replacement);
    else {
      const ordered = sortMediaItems(library.items || []);
      const position = ordered.findIndex((entry) => String(entry.rating_key) === String(item.rating_key));
      const next = position >= 0 ? tbody.querySelector(`[data-rating-key="${CSS.escape(String(ordered[position + 1]?.rating_key || ""))}"]`) : null;
      if (next) tbody.insertBefore(replacement, next); else tbody.appendChild(replacement);
    }
    bindPatchedMediaItem(item);
    return;
  }
  if (library.kind === "show" && !library.is_animation) {
    const grid = $("#media-library-content .library-panel .show-grid");
    if (!grid) return;
    const existing = grid.querySelector(`[data-rating-key="${CSS.escape(String(item.rating_key))}"]`);
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderShowCard(item, serverId);
    const card = wrapper.firstElementChild;
    if (existing) existing.replaceWith(card);
    else {
      const ordered = sortMediaItems(library.items || []);
      const position = ordered.findIndex((entry) => String(entry.rating_key) === String(item.rating_key));
      const next = position >= 0 ? grid.querySelector(`[data-rating-key="${CSS.escape(String(ordered[position + 1]?.rating_key || ""))}"]`) : null;
      if (next) grid.insertBefore(card, next); else grid.appendChild(card);
    }
    bindPatchedMediaItem(item);
    return;
  }
  if (library.kind !== "show" || !$("#media-library-content .quarter-layout")) return;
  const groups = state.mediaLibraryQuarterGroups || [];
  for (const entry of item.quarter_entries || []) {
    let group = groups.find((candidate) => String(candidate.label) === String(entry.label));
    if (!group) { group = { label: entry.label || "未定档", items: [] }; groups.push(group); }
    const quarterItem = { ...item, ...entry, library_id: item.library_id };
    const key = `${item.rating_key}-${entry.season}`;
    const index = group.items.findIndex((candidate) => `${candidate.rating_key}-${candidate.season}` === key);
    if (index >= 0) group.items[index] = quarterItem; else group.items.push(quarterItem);
    const sectionId = `quarter-${encodeURIComponent(String(group.label))}`;
    let section = document.getElementById(sectionId);
    if (!section) {
      section = document.createElement("section");
      section.id = sectionId;
      section.className = "panel quarter-group";
      section.innerHTML = `<div class="library-heading"><div><span class="eyebrow">ANIMATION QUARTER · 0</span><h3>${escapeHtml(group.label)}</h3></div><span class="library-type">季度</span></div><div class="show-grid"></div>`;
      $("#media-library-content .quarter-groups")?.appendChild(section);
    }
    const grid = section.querySelector(".show-grid");
    const old = grid?.querySelector(`[data-show-card="${CSS.escape(key)}"]`);
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderShowCard(quarterItem, serverId, entry.season);
    if (old) old.replaceWith(wrapper.firstElementChild); else grid?.appendChild(wrapper.firstElementChild);
    const eyebrow = section.querySelector(".eyebrow");
    if (eyebrow) eyebrow.textContent = `ANIMATION QUARTER · ${group.items.length}`;
  }
  state.mediaLibraryQuarterGroups = groups;
  state.mediaLibraryQuarterSignature = "";
  patchAnimationQuarterDirectory(groups);
  bindPatchedMediaItem(item);
}

async function updateMediaLibraryAfterAdd(serverId, mediaType, tmdbId, libraryId, results, resultVersion, searchRow = null) {
  const payload = await api(`/api/servers/${encodeURIComponent(serverId)}/media-library/item-by-tmdb?library_id=${encodeURIComponent(libraryId)}&media_type=${encodeURIComponent(mediaType)}&tmdb_id=${encodeURIComponent(tmdbId)}`);
  const library = state.mediaLibraryData.find((entry) => String(entry.id) === String(libraryId));
  if (library) {
    library.item_count = payload.library_item_count;
    if (library._itemsLoaded || String(state.mediaLibrarySelectedId) === String(libraryId)) {
      library.items = library.items || [];
      const index = library.items.findIndex((entry) => String(entry.rating_key) === String(payload.rating_key) || (Number(entry.tmdb_id) === Number(payload.tmdb_id) && String(entry.tmdb_media_type || entry.media_type) === String(mediaType)));
      if (index >= 0) library.items[index] = payload; else library.items.push(payload);
    }
  }
  refreshMediaLibraryCounts();
  patchVisibleMediaItem(payload, serverId);
  const current = (results || []).find((entry) => String(entry.media_type) === String(mediaType) && Number(entry.tmdb_id) === Number(tmdbId));
  if (current) {
    current.source = "merged";
    current.can_add = false;
    current.duplicate_reason = "已存在于媒体库";
    current.library_ids = [...new Set([...(current.library_ids || []), Number(libraryId)])];
    current.library_titles = [...new Set([...(current.library_titles || []), payload.library_title || String(libraryId)])];
    current.source_labels = [...new Set([...(current.source_labels || []), "本系统"])]
    current.locations = [...(current.locations || []).filter((location) => !(Number(location.library_id) === Number(libraryId) && String(location.rating_key) === String(payload.rating_key))), { library_id: Number(libraryId), rating_key: payload.rating_key, media_type: mediaType }];
    if (searchRow && searchRow.requestVersion === resultVersion) {
      searchRow.results = results;
      renderMediaSearchRow(searchRow);
    }
  }
  return payload;
}

function rerenderSelectedMediaLibrary(serverId) {
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
  if (library?.kind === "show" && state.mediaLibraryQuarterGroups) {
    renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems || []);
  } else {
    renderMediaLibraryPage(serverId);
  }
}

async function reloadSelectedMediaLibrary(serverId) {
  const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
  resetMediaLibraryWindow();
  invalidateMediaLibraryCache(serverId, library?.id);
  if (library?.kind === "show" && library.is_animation) {
    const quarter = await api(`/api/servers/${serverId}/media-library/${library.id}/quarter-index`);
    state.mediaLibraryQuarterGroups = quarter.groups || [];
    state.mediaLibraryLiveItems = quarter.live_items || [];
    state.mediaLibraryQuarterCache.set(mediaLibraryQuarterCacheKey(serverId, library.id), {
      groups: state.mediaLibraryQuarterGroups,
      liveItems: state.mediaLibraryLiveItems,
      loadedAt: Date.now(),
    });
    renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems);
    return;
  }
  await loadMediaLibraryBatch(serverId, library);
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
      // Quarter cards highlight the season that placed the show in the
      // current quarter. Preserve that focus after refreshing the show data;
      // otherwise every season is rendered as an older, grey card.
      const cardKey = card.dataset.showCard || "";
      const prefix = `${fresh.rating_key}-`;
      const focusSeason = cardKey.startsWith(prefix) ? Number(cardKey.slice(prefix.length)) : null;
      card.outerHTML = renderShowCard(fresh, serverId, Number.isFinite(focusSeason) ? focusSeason : null);
      const replacement = document.querySelector(`[data-show-card="${CSS.escape(cardKey)}"]`);
      replacement?.querySelector(".media-recheck")?.addEventListener("click", handleMediaRecheck);
      replacement?.querySelector(".media-detail-link")?.addEventListener("click", () => openMediaDetail(fresh.rating_key, fresh.library_id, "show"));
      bindMediaKindToggles();
      bindDeferredMediaImages();
      bindSeasonToggles();
    }
    await refreshMediaLibrarySyncStatus(serverId);
  } catch (error) {
    button.disabled = false;
    button.textContent = "重新检查";
    setMessage("#media-library-message", error.message, true);
  }
}

function renderShowCard(show, serverId, focusSeason = null) {
  const seasons = show.seasons || [];
  const episodeCount = seasons.reduce((sum, season) => sum + season.episodes.length, 0);
  const seasonMarkup = seasons.length ? seasons.map((season) => {
    const currentSeason = focusSeason !== null && Number(focusSeason) === Number(season.season);
    const missingEpisodes = season.episodes.filter((episode) => episode.missing && episodeAvailability(episode) === "媒体文件缺失");
    const pendingEpisodes = season.episodes.filter((episode) => episode.missing && episodeAvailability(episode) === "待发布");
    const missingSeason = missingEpisodes.length > 0;
    const presentEpisodes = season.episodes.filter((episode) => !episode.missing).length;
    const totalEpisodes = season.episodes.length;
    const seasonStatus = [
      `已收录 ${presentEpisodes} / ${totalEpisodes}`,
      missingSeason ? `缺失 ${missingEpisodes.length} 集` : "",
      pendingEpisodes.length ? `待发布 ${pendingEpisodes.length} 集` : "",
    ].filter(Boolean).join(" · ");
    return `<details class="season-block ${currentSeason ? "current-season" : "other-season"} ${missingSeason ? "has-missing" : ""}" data-season-block="${season.season}" open><summary class="season-heading"><strong>${season.season === 0 ? "S00 · 特别篇" : `S${String(season.season).padStart(2, "0")}`}</strong><span>${escapeHtml(season.bucket)} · ${escapeHtml(season.release_date || "日期未知")}${seasonStatus ? ` · ${seasonStatus}` : ""}</span></summary><div class="episode-grid">${season.episodes.map((episode) => {
      const availability = episodeAvailability(episode);
      const episodeClass = [availability === "媒体文件缺失" ? "episode-missing" : "", availability === "待发布" ? "episode-pending" : "", !currentSeason ? "other-episode" : ""].filter(Boolean).join(" ");
      const episodeTitle = episode.title || "未命名";
      return `<div class="episode-box ${episodeClass}"><strong>E${String(episode.episode).padStart(2, "0")}</strong><span title="${escapeHtml(episodeTitle)}">${escapeHtml(episodeTitle)}</span><small>${escapeHtml(episode.air_date || "待定")}${availability ? ` · ${availability}` : ""}</small></div>`;
    }).join("")}</div></details>`;
  }).join("") : '<div class="show-details-loading">正在读取季和集的详细信息…</div>';
  const cardKey = `${show.rating_key}${focusSeason === null ? "" : `-${focusSeason}`}`;
  const poster = show.thumb ? mediaImageUrl(serverId, show.thumb) : (show.poster_url || "");
  const idMarkup = renderExternalIdBadges(show, "tv", "media-show-ids");
  return `<article class="show-card" data-show-card="${escapeHtml(cardKey)}" data-rating-key="${escapeHtml(show.rating_key)}" data-library-id="${show.library_id}"><div class="show-card-head">${poster ? deferredMediaImageMarkup(poster, show.title || "封面") : '<div class="show-fallback">剧</div>'}<div class="show-card-title"><div class="show-title-line"><h4>${escapeHtml(show.title)}</h4></div><p>${escapeHtml(show.original_title || "")}</p><span class="meta">${escapeHtml(show.year || "未知年份")} · ${seasons.length ? `${seasons.length} 季 · ${episodeCount} 集` : "详情读取中"}${show.genre ? ` · ${escapeHtml(show.genre)}` : ""}</span>${idMarkup}</div><div class="media-card-actions"><button class="small media-recheck" type="button" data-library-id="${show.library_id}" data-rating-key="${escapeHtml(show.rating_key)}">重新检查</button><button class="small media-detail-link" type="button" data-library-id="${show.library_id}" data-rating-key="${escapeHtml(show.rating_key)}" data-media-type="show">查看详情</button></div></div><div class="season-list">${seasonMarkup}</div></article>`;
}

function episodeAvailability(episode) {
  if (!episode?.missing) return "";
  const airDate = String(episode.air_date || "").slice(0, 10);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return airDate && airDate > today ? "待发布" : "媒体文件缺失";
}

function bindSeasonToggles() {
  document.querySelectorAll("[data-season-block]").forEach((season) => {
    if (season.dataset.toggleBound === "1") return;
    season.dataset.toggleBound = "1";
    season.addEventListener("toggle", () => {
    const card = season.closest(".show-card");
    if (card) card.dataset.expandedSeason = season.open ? season.dataset.seasonBlock : "";
    });
  });
}

function bindMediaKindToggles() {
  document.querySelectorAll(".kind-toggle").forEach((button) => {
    if (button.dataset.toggleBound === "1") return;
    button.dataset.toggleBound = "1";
    button.addEventListener("click", () => {
    const marks = state.preferences.show_kinds || (state.preferences.show_kinds = {});
    marks[button.dataset.ratingKey] = button.textContent.trim() === "动画" ? "真人" : "动画";
    savePreferences();
    const label = marks[button.dataset.ratingKey];
    button.textContent = label;
    button.classList.toggle("anime", label === "动画");
    button.classList.toggle("live", label !== "动画");
    });
  });
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
      bindDeferredMediaImages();
    } catch (_) {
      card.querySelector(".show-details-loading")?.replaceWith(Object.assign(document.createElement("div"), { className: "show-details-loading error", textContent: "剧集详情读取失败，可点击“重新检查”重试。" }));
    }
  }
}

async function loadMediaLibrary() {
  const loadGeneration = state.mediaLibrarySelectionGeneration;
  const saved = state.preferences.media_library_state || {};
  if (saved.serverId && $("#media-library-server")?.querySelector(`option[value="${CSS.escape(String(saved.serverId))}"]`)) {
    $("#media-library-server").value = String(saved.serverId);
  }
  const serverId = $("#media-library-server").value;
  if (!serverId) {
    $("#media-library-content").innerHTML = '<div class="panel media-empty">请先配置并选择 Plex 服务器。</div>';
    return;
  }
  $("#media-library-content").innerHTML = '<div class="panel media-empty media-library-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>正在读取媒体库…</span></div>';
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
    if (loadGeneration !== state.mediaLibrarySelectionGeneration) return;
    renderMediaLibrary(result.libraries || [], serverId);
    const library = state.mediaLibraryData.find((item) => String(item.id) === String(state.mediaLibrarySelectedId));
    if (library?.is_animation && !library.sort_key && !saved.sort) {
      state.mediaLibrarySort = "first_episode_date";
      state.mediaLibraryDirection = "desc";
    }
    resetMediaLibraryWindow();
    if (library && !(library.kind === "show" && library.is_animation)) {
      if (applyMediaLibraryContentCache(serverId, library)) renderMediaLibraryPage(serverId);
      else await loadMediaLibraryBatch(serverId, library, loadGeneration);
    }
    if (library?.kind === "show" && library.is_animation) {
      const cached = applyMediaLibraryQuarterCache(serverId, library);
      const quarter = cached || await api(`/api/servers/${serverId}/media-library/${library.id}/quarter-index`);
      if (loadGeneration !== state.mediaLibrarySelectionGeneration || String(state.mediaLibrarySelectedId) !== String(library.id)) return;
      state.mediaLibraryQuarterGroups = cached ? cached.groups : (quarter.groups || []);
      state.mediaLibraryLiveItems = cached ? cached.liveItems : (quarter.live_items || []);
      if (!cached) {
        state.mediaLibraryQuarterCache.set(mediaLibraryQuarterCacheKey(serverId, library.id), {
          groups: state.mediaLibraryQuarterGroups,
          liveItems: state.mediaLibraryLiveItems,
          loadedAt: Date.now(),
        });
      }
      renderAnimationQuarterPage(state.mediaLibraryQuarterGroups, serverId, state.mediaLibraryLiveItems);
    }
    setMessage("#media-library-message", "");
    startMediaLibrarySyncPolling(serverId);
  } catch (error) {
    setMessage("#media-library-message", error.message, true);
    $("#media-library-content").innerHTML = `<div class="panel media-empty error" role="alert">读取媒体库失败：${escapeHtml(error.message || "未知错误")}</div>`;
  }
}

function startMediaLibrarySyncPolling(serverId) {
  stopMediaLibrarySyncPolling();
  if (state.mediaLibrarySyncInterval) window.clearTimeout(state.mediaLibrarySyncInterval);
  const hasActive = () => (state.mediaLibraryData || []).some((library) => ["queued", "running"].includes(library.sync_status));
  const tick = async () => {
    if (document.hidden || !document.querySelector("#view-media-library.active") || !hasActive()) {
      state.mediaLibrarySyncTimer = null;
      return;
    }
    try {
      const result = await api(`/api/servers/${serverId}/media-library`);
      const previousId = state.mediaLibrarySelectedId;
      state.mediaLibraryData = result.libraries || [];
      const selected = state.mediaLibraryData.find((library) => String(library.id) === String(previousId));
      if (selected) {
        const status = $("#media-library-sync-status");
        if (status) status.textContent = renderMediaLibrarySyncStatus(selected);
        renderMediaLibrarySyncProgress(selected);
      }
      if (!hasActive()) {
        state.mediaLibrarySyncTimer = null;
        invalidateMediaLibraryCache(serverId);
        await loadMediaLibrary();
        return;
      }
    } catch (_) {}
    state.mediaLibrarySyncInterval = window.setTimeout(tick, 10000);
  };
  if (!hasActive()) return;
  state.mediaLibrarySyncTimer = true;
  tick();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && document.querySelector("#view-media-library.active")) {
    const serverId = $("#media-library-server")?.value;
    if (serverId) startMediaLibrarySyncPolling(serverId);
  }
});

$("#media-library-refresh")?.addEventListener("click", async () => {
  const serverId = $("#media-library-server")?.value;
  const libraryId = state.mediaLibrarySelectedId;
  if (!serverId || !libraryId) return;
  try { const result = await api(`/api/servers/${serverId}/media-library/${libraryId}/refresh`, { method: "POST", body: "{}" }); invalidateMediaLibraryCache(serverId); setMessage("#media-library-message", `已开始同步媒体库、更新 TMDB 数据并检查缺失剧集：${result.id}`); await loadMediaLibrary(); } catch (error) { setMessage("#media-library-message", error.message, true); }
});

function searchLibraryOptions(kind) {
  return (state.mediaLibraryData || []).filter((item) => item.kind === kind).map((item) => `<option value="${item.id}">${escapeHtml(item.title)}</option>`).join("");
}
async function jumpToMediaSearchLocation(item) {
  const location = (item.locations || [])[0] || (item.library_id && item.rating_key ? { library_id: item.library_id, rating_key: item.rating_key, media_type: item.media_type } : null);
  if (!location) return;
  const serverId = $("#media-library-server")?.value;
  if (!serverId) return;
  const ratingKey = String(location.rating_key || "");
  // Always go through the normal library selection flow. Animation libraries
  // use the quarter layout, so rendering the generic library page directly
  // leaves the UI on an incomplete page and the target card cannot be found.
  await selectMediaLibrary(location.library_id, serverId);
  const quarterGroups = state.mediaLibraryQuarterGroups || [];
  const targetQuarter = quarterGroups.find((group) => (group.items || []).some((entry) => String(entry.rating_key) === ratingKey));
  const selector = location.media_type === "movie"
    ? `tr[data-rating-key="${CSS.escape(ratingKey)}"]`
    : `[data-rating-key="${CSS.escape(ratingKey)}"]`;
  let element = document.querySelector(selector);
  // Quarter groups use content-visibility:auto for performance. A card can
  // exist in the DOM while its group still has only an intrinsic placeholder
  // height. Force the target group to participate in layout before reading
  // any card coordinates; otherwise the first click lands at a stale offset
  // and a second click appears to "fix" it.
  const targetSection = targetQuarter
    ? document.getElementById(`quarter-${encodeURIComponent(String(targetQuarter.label || ""))}`)
    : null;
  if (targetSection) {
    targetSection.style.contentVisibility = "visible";
    targetSection.style.containIntrinsicSize = "auto";
    targetSection.scrollIntoView({ behavior: "auto", block: "start" });
    await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
  }
  // Hydrate the requested card first. The background loop replaces loading
  // cards one by one; waiting for this card here prevents its later height
  // change from moving the first-click destination.
  if (element?.querySelector(".show-details-loading") && location.media_type !== "movie") {
    try {
      const entry = targetQuarter?.items?.find((item) => String(item.rating_key) === ratingKey);
      const fresh = await api(`/api/servers/${serverId}/media-library/item?library_id=${encodeURIComponent(location.library_id)}&rating_key=${encodeURIComponent(ratingKey)}&media_type=show`);
      const focusSeason = entry && entry.season !== undefined && entry.season !== null ? Number(entry.season) : null;
      const cardKey = element.dataset.showCard || `${ratingKey}${focusSeason === null ? "" : `-${focusSeason}`}`;
      element.outerHTML = renderShowCard(fresh, serverId, Number.isFinite(focusSeason) ? focusSeason : null);
      element = document.querySelector(`[data-show-card="${CSS.escape(cardKey)}"]`) || document.querySelector(selector);
      element?.querySelector(".media-recheck")?.addEventListener("click", handleMediaRecheck);
      element?.querySelector(".media-detail-link")?.addEventListener("click", () => openMediaDetail(fresh.rating_key, location.library_id, "show"));
      bindMediaKindToggles();
      bindSeasonToggles();
    } catch (_) {
      // The normal hydration loop can still replace the card if this request
      // fails, so navigation continues with the current DOM node.
    }
  }
  // Quarter details are hydrated asynchronously after the quarter layout is
  // painted. Wait briefly for the card rather than stopping on the loading
  // state or scrolling to the top of the page.
  const deadline = Date.now() + 6000;
  while (!element && Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    element = document.querySelector(selector);
  }
  if (element) {
    // The first layout pass may still be settling after content-visibility is
    // lifted. Wait for two stable animation frames and re-query the node in
    // case asynchronous detail hydration replaced it.
    await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
    element = document.querySelector(selector) || element;
    // Re-query after the layout pass because async detail hydration may have
    // replaced the card node. Use an explicit document offset so this also
    // works when content-visibility or nested layout containers are involved.
    const scrollingElement = document.scrollingElement || document.documentElement;
    const preferredTop = Math.max(96, Math.round(window.innerHeight * 0.18));
    const resolveElement = () => document.querySelector(selector) || element;
    const correctScroll = () => {
      const current = resolveElement();
      if (!current) return;
      const rect = current.getBoundingClientRect();
      const delta = rect.top - preferredTop;
      if (Math.abs(delta) > 1) scrollingElement.scrollTo({ top: Math.max(0, scrollingElement.scrollTop + delta), behavior: "auto" });
    };
    // Do the first move synchronously. Smooth scrolling lets asynchronous
    // card hydration move the destination underneath the animation, which is
    // why the old implementation often required a second click.
    correctScroll();
    // Detail requests for cards above the target can still replace loading
    // placeholders and change the document height. Re-anchor the target for a
    // short settling window so the first click remains accurate.
    let frames = 0;
    const settle = () => {
      correctScroll();
      if (++frames < 90) window.requestAnimationFrame(settle);
    };
    window.requestAnimationFrame(settle);
    const highlighted = resolveElement();
    highlighted?.classList.add("media-search-highlight");
    setTimeout(() => resolveElement()?.classList.remove("media-search-highlight"), 2200);
    return;
  }
  throw new Error("没有找到对应的媒体库条目，请刷新媒体库后重试。");
}
function syncMediaSearchRowFromDom(row) {
  const element = document.querySelector(`[data-media-search-row="${row.id}"]`);
  if (!element) return row;
  row.query = element.querySelector(".media-search-row-query")?.value.trim() || "";
  row.media_type = element.querySelector(".media-search-row-type")?.value || "all";
  row.year = element.querySelector(".media-search-row-year")?.value.trim() || "";
  return row;
}

function renderMediaSearchRowResults(row) {
  const element = document.querySelector(`[data-media-search-row="${row.id}"]`);
  const target = element?.querySelector(".media-library-search-row-results");
  if (!target) return;
  const results = row.results || [];
  const serverId = $("#media-library-server")?.value || "";
  target.classList.toggle("has-overflow", results.length > 10);
  target.innerHTML = results.map((item, resultIndex) => {
    const firstLocation = (item.locations || [])[0] || {};
    const label = item.media_type === "movie" ? "电影" : "剧集";
    const posterUrl = item.poster_url || (item.thumb && serverId ? mediaImageUrl(serverId, item.thumb) : "");
    const poster = posterUrl ? deferredMediaImageMarkup(posterUrl, item.title || "封面", "search-result-poster") : `<span class="search-result-poster fallback">影</span>`;
    const action = item.can_add ? `<select class="search-target-library" data-kind="${item.media_type}">${searchLibraryOptions(item.media_type === "movie" ? "movie" : "show")}</select><button class="small primary media-search-add" data-type="${item.media_type}" data-tmdb-id="${item.tmdb_id}">添加到媒体库</button><span class="media-search-add-status meta" hidden></span>` : `<span class="meta">${escapeHtml(item.add_status || item.duplicate_reason || "已存在")}</span>`;
    const jump = item.source !== "tmdb" && ((item.locations || []).length || item.rating_key) ? `<button class="small media-search-jump" type="button" data-result-index="${resultIndex}" title="跳转到媒体库条目">↗</button>` : "";
    const sourceLabels = [...new Set((item.source_labels || (item.source === "merged" ? ["媒体库", "本系统", "TMDB"] : item.source === "tmdb" ? ["TMDB"] : item.source === "catalog" ? ["本系统"] : ["媒体库"])).map((source) => String(source).startsWith("媒体服务器") ? "媒体服务器" : source))];
    const ids = renderExternalIdBadges(item, item.media_type);
    return `<article class="media-search-result" data-location-library-id="${escapeHtml(firstLocation.library_id || item.library_id || "")}" data-location-rating-key="${escapeHtml(firstLocation.rating_key || item.rating_key || "")}" data-result-index="${resultIndex}"><div>${poster}</div><div class="search-result-main"><strong>${escapeHtml(item.title || "未命名")}</strong><span>${escapeHtml(item.original_title || "")} · ${escapeHtml(item.year || "未知年份")} · ${label}</span>${ids}<small>${escapeHtml((item.genres || []).join(", ") || "无标签")}</small><small class="media-source-badges">${sourceLabels.map((source) => `<span class="media-source-badge">${escapeHtml(source)}</span>`).join("")}${item.media_type === "movie" && item.resolution ? ` · ${escapeHtml(item.resolution)}` : ""}</small></div><div class="media-card-actions">${jump}${action}</div></article>`;
  }).join("") || (row.status && !row.error ? '<div class="media-empty">没有找到匹配作品。</div>' : "");

   bindDeferredMediaImages();
   target.querySelectorAll(".media-search-jump").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try { await jumpToMediaSearchLocation(results[Number(button.dataset.resultIndex)]); }
    catch (error) { row.status = error.message || "跳转媒体库条目失败"; row.error = true; renderMediaSearchRow(row); }
    finally { button.disabled = false; }
  }));
  target.querySelectorAll(".media-search-add").forEach((button) => button.addEventListener("click", async () => {
    const serverId = $("#media-library-server")?.value;
    const card = button.closest(".media-search-result");
    const select = card?.querySelector(".search-target-library");
    const status = card?.querySelector(".media-search-add-status");
    button.disabled = true;
    if (select) select.disabled = true;
    button.textContent = "提交中…";
    if (status) { status.hidden = false; status.textContent = "正在创建添加任务…"; status.className = "media-search-add-status meta"; }
    try {
      const result = await api(`/api/servers/${serverId}/media-library/search/add`, { method: "POST", body: JSON.stringify({ media_type: button.dataset.type, tmdb_id: Number(button.dataset.tmdbId), library_id: Number(select?.value) }) });
      const jobId = Number(result.job_id);
      if (!jobId) throw new Error("添加任务未返回任务编号");
      row.status = `添加任务 #${jobId} 已创建，正在写入本系统媒体库…`;
      const startedAt = Date.now();
      const poll = async () => {
        try {
          const job = await api(`/api/jobs/${jobId}`);
          if (["queued", "running"].includes(job.status)) {
            const progress = job.total ? ` ${job.processed || 0}/${job.total}` : "";
            if (status) status.textContent = `任务 #${jobId}：${job.status === "running" ? "处理中" : "排队中"}${progress}`;
            if (Date.now() - startedAt < 180000) { window.setTimeout(poll, 700); return; }
            if (status) status.textContent = `任务 #${jobId}：仍在处理中，可在任务中心查看`;
            return;
          }
          if (job.status === "succeeded") {
            if (status) { status.textContent = `任务 #${jobId}：添加成功`; status.className = "media-search-add-status success"; }
            row.status = `任务 #${jobId}：添加成功`;
            row.error = false;
            const resultItem = row.results.find((entry) => String(entry.media_type) === String(button.dataset.type) && Number(entry.tmdb_id) === Number(button.dataset.tmdbId));
            if (resultItem) resultItem.add_status = `任务 #${jobId}：添加成功`;
            try {
              await updateMediaLibraryAfterAdd(serverId, button.dataset.type, Number(button.dataset.tmdbId), Number(select?.value), row.results, row.requestVersion, row);
            } catch (refreshError) {
              if (resultItem) { resultItem.add_status = `任务 #${jobId}：添加成功（局部刷新失败）`; resultItem.can_add = false; resultItem.duplicate_reason = "已写入本系统，页面请手动刷新"; }
              row.status = `任务 #${jobId} 已完成，但局部刷新失败：${refreshError.message || "请手动刷新页面"}`;
              row.error = true;
              renderMediaSearchRow(row);
            }
            return;
          }
          if (status) { status.textContent = `任务 #${jobId}：失败`; status.className = "media-search-add-status error"; }
          if (select) select.disabled = false;
          button.disabled = false;
          button.textContent = "重试添加";
          row.status = `添加任务 #${jobId} 失败：${job.error || `任务状态：${job.status}`}`;
          row.error = true;
          renderMediaSearchRow(row);
        } catch (error) {
          if (Date.now() - startedAt < 180000) { window.setTimeout(poll, 1200); return; }
          if (select) select.disabled = false;
          button.disabled = false;
          button.textContent = "重试添加";
          row.status = `已创建任务 #${jobId}，但暂时无法读取状态，请到任务中心查看。`;
          row.error = true;
          renderMediaSearchRow(row);
        }
      };
      window.setTimeout(poll, 250);
    } catch (error) {
      if (select) select.disabled = false;
      button.disabled = false;
      button.textContent = "添加到媒体库";
      row.status = error.message || "创建添加任务失败";
      row.error = true;
      renderMediaSearchRow(row);
    }
  }));
}

function renderMediaSearchRow(row) {
  const target = document.querySelector(`[data-media-search-row="${row.id}"]`);
  if (!target) return;
  const query = target.querySelector(".media-search-row-query");
  const type = target.querySelector(".media-search-row-type");
  const year = target.querySelector(".media-search-row-year");
  if (document.activeElement !== query) query.value = row.query;
  if (document.activeElement !== type) type.value = row.media_type;
  if (document.activeElement !== year) year.value = row.year;
  const status = target.querySelector(".media-library-search-row-status");
  if (status) { status.textContent = row.status || ""; status.className = `media-library-search-row-status meta${row.error ? " error" : ""}`; }
  renderMediaSearchRowResults(row);
}

function renderMediaSearchRows() {
  const target = $("#media-library-search-rows");
  if (!target) return;
  if (!mediaSearchState.rows.length) mediaSearchState.rows = [createMediaSearchRow()];
  target.innerHTML = mediaSearchState.rows.map((row, index) => `<section class="media-library-search-row panel" data-media-search-row="${row.id}"><div class="media-library-search-row-fields"><span class="media-search-row-number">${index + 1}</span><input class="media-search-row-query" aria-label="搜索" placeholder="名称、TMDB ID 或 TMDB URL" value="${escapeHtml(row.query)}"><select class="media-search-row-type" aria-label="类型"><option value="all">全部类型</option><option value="movie">电影</option><option value="tv">剧集</option></select><input class="media-search-row-year" aria-label="发布年份" type="number" min="1800" max="2200" placeholder="发布年份，例如 2024" value="${escapeHtml(row.year)}"><button class="small media-search-row-remove" type="button" title="删除此行">删除</button></div><div class="media-library-search-row-status meta"></div><div class="media-library-search-row-results"></div></section>`).join("");
  target.querySelectorAll(".media-library-search-row").forEach((element) => {
    const row = mediaSearchRowById(element.dataset.mediaSearchRow);
    const query = element.querySelector(".media-search-row-query");
    const type = element.querySelector(".media-search-row-type");
    const year = element.querySelector(".media-search-row-year");
    query.addEventListener("input", () => { row.query = query.value; row.requestVersion += 1; });
    type.addEventListener("change", () => { row.media_type = type.value; row.requestVersion += 1; });
    year.addEventListener("input", () => { row.year = year.value; row.requestVersion += 1; });
    query.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); runMediaLibrarySearchRow(row); } });
    query.addEventListener("paste", (event) => {
      const text = event.clipboardData?.getData("text/plain") || "";
      const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      if (lines.length <= 1) return;
      event.preventDefault();
      const base = { media_type: row.media_type, year: row.year };
      const available = Math.max(0, MEDIA_SEARCH_MAX_ROWS - mediaSearchState.rows.length);
      const values = lines.slice(0, available + 1);
      row.query = values.shift() || "";
      const additions = values.map((value) => createMediaSearchRow({ ...base, query: value }));
      mediaSearchState.rows = mediaSearchState.rows.slice(0, mediaSearchState.rows.indexOf(row) + 1).concat(additions, mediaSearchState.rows.slice(mediaSearchState.rows.indexOf(row) + 1));
      renderMediaSearchRows();
      const message = lines.length > available + 1 ? `已粘贴前 ${available + 1} 行，最多支持 ${MEDIA_SEARCH_MAX_ROWS} 行。` : `已生成 ${lines.length} 个搜索行。`;
      setMessage("#media-library-search-message", message, lines.length > available + 1);
    });
    element.querySelector(".media-search-row-remove")?.addEventListener("click", () => {
      mediaSearchState.rows = mediaSearchState.rows.filter((item) => item !== row);
      if (!mediaSearchState.rows.length) mediaSearchState.rows = [createMediaSearchRow()];
      renderMediaSearchRows();
    });
  });
  mediaSearchState.rows.forEach((row) => { if (row.results.length || row.status) renderMediaSearchRow(row); });
}

async function runMediaLibrarySearchRow(row) {
  syncMediaSearchRowFromDom(row);
  if (!row.query && !row.year && row.media_type === "all") { row.status = "请输入关键词、类型或年份"; row.error = true; renderMediaSearchRow(row); return; }
  const serverId = $("#media-library-server")?.value;
  if (!serverId) return;
  const requestVersion = ++row.requestVersion;
  row.status = "正在搜索…";
  row.error = false;
  row.results = [];
  renderMediaSearchRow(row);
  const params = new URLSearchParams({ q: row.query, media_type: row.media_type, include_tmdb: "1" });
  if (row.year) params.set("year", row.year);
  try {
    const data = await api(`/api/servers/${serverId}/media-library/search?${params}`);
    if (requestVersion !== row.requestVersion) return;
    row.results = data.results || [];
    row.status = data.warning || `找到 ${row.results.length} 项`;
    row.error = Boolean(data.warning);
    renderMediaSearchRow(row);
  } catch (error) {
    if (requestVersion !== row.requestVersion) return;
    row.status = error.message || "搜索失败";
    row.error = true;
    renderMediaSearchRow(row);
  }
}

async function runMediaLibrarySearch() {
  const nonEmpty = mediaSearchState.rows.map((row) => syncMediaSearchRowFromDom(row)).filter((row) => row.query || row.year || row.media_type !== "all");
  mediaSearchState.rows.filter((row) => !nonEmpty.includes(row)).forEach((row) => { row.requestVersion += 1; });
  mediaSearchState.rows = nonEmpty.length ? nonEmpty : [createMediaSearchRow()];
  renderMediaSearchRows();
  if (!nonEmpty.length) { setMessage("#media-library-search-message", "请输入至少一项搜索条件", true); return; }
  setMessage("#media-library-search-message", `正在搜索 ${nonEmpty.length} 行…`);
  await Promise.allSettled(nonEmpty.map((row) => runMediaLibrarySearchRow(row)));
  if (mediaSearchState.rows.length) setMessage("#media-library-search-message", "批量搜索完成");
}

function clearMediaLibrarySearch() {
  mediaSearchState.version += 1;
  mediaSearchState.rows.forEach((row) => { row.requestVersion += 1; });
  mediaSearchState.rows = [createMediaSearchRow()];
  renderMediaSearchRows();
  setMessage("#media-library-search-message", "");
}

$("#media-library-search-submit")?.addEventListener("click", runMediaLibrarySearch);
$("#media-library-search-add-one")?.addEventListener("click", () => { if (mediaSearchState.rows.length < MEDIA_SEARCH_MAX_ROWS) { mediaSearchState.rows.push(createMediaSearchRow()); renderMediaSearchRows(); document.querySelector(`[data-media-search-row="${mediaSearchState.rows.at(-1).id}"] .media-search-row-query`)?.focus(); } });
$("#media-library-search-add-five")?.addEventListener("click", () => { const count = Math.min(5, MEDIA_SEARCH_MAX_ROWS - mediaSearchState.rows.length); for (let index = 0; index < count; index += 1) mediaSearchState.rows.push(createMediaSearchRow()); renderMediaSearchRows(); });
$("#media-library-search-add-ten")?.addEventListener("click", () => { const count = Math.min(10, MEDIA_SEARCH_MAX_ROWS - mediaSearchState.rows.length); for (let index = 0; index < count; index += 1) mediaSearchState.rows.push(createMediaSearchRow()); renderMediaSearchRows(); });
$("#media-library-search-clear")?.addEventListener("click", clearMediaLibrarySearch);
renderMediaSearchRows();
$("#media-library-server")?.addEventListener("change", () => { state.mediaLibrarySelectedId = null; state.mediaLibraryPage = 1; loadMediaLibrary(); });
$("#media-library-picker")?.addEventListener("change", () => selectMediaLibrary($("#media-library-picker").value, $("#media-library-server").value));
$("#media-library-animation-mode")?.addEventListener("change", async () => {
  const serverId = $("#media-library-server")?.value;
  const libraryId = state.mediaLibrarySelectedId;
  if (!serverId || !libraryId) return;
  try { await api(`/api/servers/${serverId}/libraries/${libraryId}/settings`, { method: "PUT", body: JSON.stringify({ animation_mode: $("#media-library-animation-mode").value }) }); await loadMediaLibrary(); } catch (error) { setMessage("#media-library-message", error.message, true); }
});
$("#media-library-auto-recheck")?.addEventListener("change", async (event) => {
  const input = event.currentTarget;
  const serverId = $("#media-library-server").value;
  const libraryId = state.mediaLibrarySelectedId;
  const enabled = input.checked;
  input.disabled = true;
  try {
    await api(`/api/servers/${serverId}/libraries/${libraryId}/settings`, { method: "PUT", body: JSON.stringify({ auto_recheck_new_episodes: enabled }) });
    const library = state.mediaLibraryData.find((item) => String(item.id) === String(libraryId));
    if (library) library.auto_recheck_new_episodes = enabled;
    setMessage("#media-library-message", enabled ? "已开启该媒体库新增剧集自动重检" : "已关闭该媒体库新增剧集自动重检");
  } catch (error) {
    input.checked = !enabled;
    setMessage("#media-library-message", error.message, true);
  } finally { input.disabled = false; }
});
$("#media-library-page-size")?.addEventListener("change", () => { state.mediaLibraryPageSize = $("#media-library-page-size").value; state.mediaLibraryPage = 1; rerenderSelectedMediaLibrary($("#media-library-server").value); });
$("#media-library-sort")?.addEventListener("change", async () => { state.mediaLibrarySort = $("#media-library-sort").value; state.mediaLibraryPage = 1; const serverId = $("#media-library-server").value; const libraryId = state.mediaLibrarySelectedId; if (serverId && libraryId) { await reloadSelectedMediaLibrary(serverId); try { await api(`/api/servers/${serverId}/libraries/${libraryId}/settings`, { method: "PUT", body: JSON.stringify({ animation_mode: $("#media-library-animation-mode").value, sort_key: state.mediaLibrarySort, sort_direction: state.mediaLibraryDirection }) }); } catch (error) { setMessage("#media-library-message", error.message, true); } } });
$("#media-library-sort-direction")?.addEventListener("click", async () => { state.mediaLibraryDirection = state.mediaLibraryDirection === "asc" ? "desc" : "asc"; $("#media-library-sort-direction").textContent = state.mediaLibraryDirection === "asc" ? "升序" : "降序"; state.mediaLibraryPage = 1; const serverId = $("#media-library-server").value; const libraryId = state.mediaLibrarySelectedId; if (serverId && libraryId) { await reloadSelectedMediaLibrary(serverId); try { await api(`/api/servers/${serverId}/libraries/${libraryId}/settings`, { method: "PUT", body: JSON.stringify({ animation_mode: $("#media-library-animation-mode").value, sort_key: state.mediaLibrarySort, sort_direction: state.mediaLibraryDirection }) }); } catch (error) { setMessage("#media-library-message", error.message, true); } } });

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

async function loadTmdbSettings() {
  try {
    const settings = await api("/api/settings/tmdb");
    const status = $("#system-tmdb-key-status");
    if (status) status.textContent = settings.configured ? "已保存" : "未配置";
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

window.addEventListener("beforeunload", (event) => {
  if (!state.tagsDirty) return;
  event.preventDefault();
  event.returnValue = "";
});

$("#page-scroll-top")?.addEventListener("click", () => {
  (document.scrollingElement || document.documentElement).scrollTo({ top: 0, behavior: "smooth" });
});

$("#page-scroll-bottom")?.addEventListener("click", () => {
  const scrollingElement = document.scrollingElement || document.documentElement;
  scrollingElement.scrollTo({ top: scrollingElement.scrollHeight, behavior: "smooth" });
});

bindOverview();
bootstrap();
updateScheduleMode();
updateLocalizeAddedFilter();
