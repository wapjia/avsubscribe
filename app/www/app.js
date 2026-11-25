import { TrimApp } from "./vendor/trim-web-app.js";
import { APP_VERSION } from "./version.js";
import { filterLogEntries, matchesKeywords, selectLogRound, splitHttpLog } from "./log-utils.mjs";
import { activeRepairForVideo, repairPhaseLabel, videoRepairStatus } from "./repair-status.mjs";

const $ = (selector) => document.querySelector(selector);
const fnosApp = new TrimApp();
const dialog = $("#subscription-dialog");
const form = $("#subscription-form");
let subscriptions = [];
let activity = [];
let logEntries = [];
const expandedResponseLogs = new Set();
let busy = false;
let toastTimer = 0;
let activeView = "all";
let selectedSubscriptionId = "";
let existingVideos = [];
let existingForId = "";
let existingVideoCount = 0;
let existingVideosLoading = false;
let existingVideosError = "";
let existingVideosRequest = 0;
let actresses = [];
let actressesLoading = null;
let actressRequestRevision = 0;
let lastActressesRefresh = 0;
const actressDrafts = new Map();
const actressProfileQueue = new Map();
const subscriptionActressProfiles = new Map();
const subscriptionAvatarObserver = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries, observer) => {
  for (const { target, isIntersecting } of entries) {
    if (!isIntersecting || !target.isConnected) continue;
    preloadCover(target.actress.profile?.avatar_url);
    observer.unobserve(target);
  }
}) : null;
let actressProfileLoading = false;
let actressProfileRetryAt = 0;
let actressProfileRetryTimer = 0;
let actressProfileAnchor = null;
const actressProfilePopup = document.createElement("div");
actressProfilePopup.className = "actress-profile-popup";
actressProfilePopup.id = "actress-profile-popup";
actressProfilePopup.setAttribute("role", "tooltip");
actressProfilePopup.hidden = true;
document.body.append(actressProfilePopup);
const actressProfileObserver = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const button = entry.target;
    if (entry.isIntersecting) queueActressProfile(button);
    else if (actressProfileQueue.get(button.actress.source_name) === button) actressProfileQueue.delete(button.actress.source_name);
  }
}) : null;
let lastStatusState = "";
let currentStatus = {};
let repairJobs = [];
const awaitedRepairJobs = new Set();
let refreshInFlight = false;
let activityRefreshInFlight = false;
let subscriptionRefreshInFlight = false;
let subscriptionRevision = 0;
let logsLoaded = false;
let hasOlderLogs = false;
let olderLogsLoading = false;
let logSearchRevision = 0;
let logFilterTimer = 0;
let lastLogScopeKey = "";
let logViewportFillScheduled = false;
let lastSubscriptionRefresh = 0;
let lastActivityRefresh = 0;
let lastLogId = 0;
const LOG_PAGE_SIZE = 200;
let blacklistNumbers = [];
let searchKeywords = [];
let pathMappings = [];
const videoSubtitleRequests = new Map();
const videoFileInfoRequests = new Map();
const videoHardlinkRequests = new Map();
const coverPreloads = new Map();
const coverHoverPreview = document.createElement("div");
coverHoverPreview.className = "cover-hover-preview";
coverHoverPreview.setAttribute("aria-hidden", "true");
coverHoverPreview.hidden = true;
document.body.append(coverHoverPreview);
const hardlinkPreview = document.createElement("div");
hardlinkPreview.className = "hardlink-preview";
hardlinkPreview.id = "video-hardlink-preview";
hardlinkPreview.setAttribute("role", "tooltip");
hardlinkPreview.hidden = true;
document.body.append(hardlinkPreview);
let hardlinkAnchor = null;
let nfoLoad = 0;
const videoRefreshStates = new Map();
const appBase = window.location.pathname.replace(/\/$/, "");
$("#app-version").textContent = `v${APP_VERSION}`;

function appUrl(path) {
  return `${appBase}${path}`;
}

async function api(path, options = {}) {
  const response = await fetch(appUrl(path), options);
  const body = await response.text();
  let data;
  try {
    data = JSON.parse(body);
  } catch (_) {
    throw new Error(`应用接口返回了非 JSON 响应（HTTP ${response.status}），请检查 fnOS CGI 应用入口。`);
  }
  if (!response.ok || data.error) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { node.className = "toast"; }, 3200);
}

function stateLabel(state) {
  return ({
    downloading: "下载中", upgrading: "洗版下载中", completed: "已完成", failed: "失败",
    missing: "任务丢失", upgrade_available: "有新版", superseded: "已替换", submitted: "已提交",
    copy_failed: "复制失败", cleanup_failed: "清理失败"
  })[state] || state || "等待检查";
}

function makeButton(label, className, action, title = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  if (title) button.title = title;
  button.addEventListener("click", action);
  return button;
}

function subscriptionSearchKeyword(subscription) {
  return subscriptionSearchKeywords(subscription).join("，");
}

function subscriptionSearchKeywords(subscription) {
  const savedKeywords = Array.isArray(subscription?.search_keywords)
    ? subscription.search_keywords.map((value) => String(value || "").trim()).filter(Boolean)
    : [];
  if (savedKeywords.length) return savedKeywords;
  if (String(subscription?.search_keyword || "").trim()) return [String(subscription.search_keyword).trim()];
  const performer = String(subscription?.performer || "").trim();
  const legacyKeyword = String(subscription?.keywords || "").trim();
  return [[performer, legacyKeyword || "ltx"].filter(Boolean).join(" ")];
}

function renderSearchKeywordTags() {
  const container = $("#search-keyword-tags");
  const input = $("#search-keyword");
  container.replaceChildren();
  for (const keyword of searchKeywords) {
    const tag = document.createElement("span");
    tag.className = "blacklist-tag search-keyword-tag";
    const text = document.createElement("span");
    text.textContent = keyword;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "blacklist-remove";
    remove.textContent = "×";
    remove.title = `移除搜索关键字 ${keyword}`;
    remove.setAttribute("aria-label", `移除搜索关键字 ${keyword}`);
    remove.addEventListener("click", () => {
      searchKeywords = searchKeywords.filter((value) => value !== keyword);
      renderSearchKeywordTags();
      input.focus();
    });
    tag.append(text, remove);
    container.append(tag);
  }
  container.append(input);
}

function addSearchKeywords(value) {
  const keywords = String(value || "").split(/[，,]/).map((item) => item.trim()).filter(Boolean);
  for (const keyword of keywords) {
    if (keyword.length > 200) {
      toast("每组搜索关键字不能超过 200 个字符。", true);
      return false;
    }
    if (!searchKeywords.includes(keyword)) {
      if (searchKeywords.length >= 30) {
        toast("搜索关键字最多添加 30 组。", true);
        return false;
      }
      searchKeywords.push(keyword);
    }
  }
  $("#search-keyword").value = "";
  renderSearchKeywordTags();
  return true;
}

function useSystemBrowser(link) {
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  let opening = false;
  link.addEventListener("click", async (event) => {
    // Native fnOS WebViews need the host bridge; target=_blank can replace the app.
    if (!link.hasAttribute("href") || fnosApp.isWeb !== false) return;
    event.preventDefault();
    if (opening) return;
    opening = true;
    let timer;
    try {
      await Promise.race([
        fnosApp.openURL(link.href, "_blank", "noopener,noreferrer"),
        new Promise((_resolve, reject) => {
          timer = window.setTimeout(() => reject(new Error("手机端未响应打开浏览器请求，请检查飞牛客户端版本。")), 10000);
        })
      ]);
    } catch (error) {
      toast(`无法打开系统浏览器：${error.message}`, true);
    } finally {
      window.clearTimeout(timer);
      opening = false;
    }
  });
}

function renderNumber(cell, code, mteamId, subtitle = "", loadSubtitle = null) {
  const id = String(mteamId || "");
  if (!/^\d+$/.test(id)) {
    cell.textContent = code || "";
    return;
  }
  const link = document.createElement("a");
  link.href = `https://kp.m-team.cc/detail/${encodeURIComponent(id)}`;
  useSystemBrowser(link);
  link.textContent = code || "";
  link.title = subtitle || "在 M-Team 打开影片详情";
  link.className = "number-link";
  if (loadSubtitle) {
    let request;
    const fetchSubtitle = () => {
      if (request) return;
      link.title = "正在读取副标题…";
      request = loadSubtitle().then((value) => {
        subtitle = value || "";
        link.title = subtitle || "M-Team 未提供副标题";
      }).catch(() => {
        request = null;
        link.title = "副标题读取失败，点击打开 M-Team 详情";
      });
    };
    link.addEventListener("mouseenter", fetchSubtitle, { once: true });
    link.addEventListener("focus", fetchSubtitle);
  }
  cell.append(link);
}

function loadVideoSubtitle(subscriptionId, code) {
  const key = `${subscriptionId}:${code}`;
  if (!videoSubtitleRequests.has(key)) {
    const request = api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/subtitle`)
      .then((result) => result.subtitle || "")
      .catch((error) => {
        videoSubtitleRequests.delete(key);
        throw error;
      });
    videoSubtitleRequests.set(key, request);
  }
  return videoSubtitleRequests.get(key);
}

function fileTooltip(path, mediainfo, checked = false) {
  const header = `${path}\n\nM-Team MediaInfo`;
  if (!checked) return `${header}\n正在读取…`;
  const body = String(mediainfo || "").trim();
  if (!body) return `${header}\nM-Team 详情未提供 MediaInfo`;
  const maxLength = 30000;
  return `${header}\n${body.length > maxLength ? `${body.slice(0, maxLength)}\n…（内容过长，已截断）` : body}`;
}

function loadVideoFileInfo(subscriptionId, code, file) {
  const key = `${subscriptionId}:${file.path}`;
  if (!videoFileInfoRequests.has(key)) {
    const request = api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/file-info`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path })
    }).catch((error) => {
      if (videoFileInfoRequests.get(key) === request) videoFileInfoRequests.delete(key);
      throw error;
    });
    videoFileInfoRequests.set(key, request);
  }
  return videoFileInfoRequests.get(key);
}

function videoRefreshKey(subscriptionId, path) {
  return JSON.stringify([subscriptionId, path]);
}

function hideHardlinkPreview() {
  hardlinkAnchor?.removeAttribute("aria-describedby");
  hardlinkAnchor = null;
  hardlinkPreview.hidden = true;
  hardlinkPreview.replaceChildren();
}

function hideCoverHoverPreview() {
  coverHoverPreview.hidden = true;
  coverHoverPreview.replaceChildren();
}

function showCoverHoverPreview(thumbnail, urls) {
  if (window.matchMedia("(max-width: 600px)").matches || !urls.length) return;
  hideCoverHoverPreview();
  const currentImage = thumbnail.querySelector("img");
  if (currentImage?.complete && currentImage.naturalWidth > 0) {
    const immediate = currentImage.cloneNode(true);
    immediate.alt = "";
    immediate.removeAttribute("loading");
    immediate.removeAttribute("decoding");
    coverHoverPreview.append(immediate);
    coverHoverPreview.hidden = false;
    const anchor = thumbnail.getBoundingClientRect();
    const { width, height } = coverHoverPreview.getBoundingClientRect();
    const rightEdge = document.documentElement.clientWidth - width - 8;
    const left = anchor.right + 8 <= rightEdge ? anchor.right + 8 : anchor.left - width - 8;
    coverHoverPreview.style.left = `${Math.max(8, Math.min(left, rightEdge))}px`;
    coverHoverPreview.style.top = `${Math.max(8, Math.min(anchor.top, window.innerHeight - height - 8))}px`;
    return;
  }
  const preview = document.createElement("img");
  preview.alt = "";
  preview.loading = "eager";
  preview.decoding = "sync";
  preview.referrerPolicy = "no-referrer";
  preview.addEventListener("load", () => {
    if (preview.parentElement !== coverHoverPreview || !thumbnail.isConnected) return;
    coverHoverPreview.hidden = false;
    const anchor = thumbnail.getBoundingClientRect();
    const { width, height } = coverHoverPreview.getBoundingClientRect();
    const rightEdge = document.documentElement.clientWidth - width - 8;
    const left = anchor.right + 8 <= rightEdge ? anchor.right + 8 : anchor.left - width - 8;
    coverHoverPreview.style.left = `${Math.max(8, Math.min(left, rightEdge))}px`;
    coverHoverPreview.style.top = `${Math.max(8, Math.min(anchor.top, window.innerHeight - height - 8))}px`;
  });
  const alternatives = [...new Set(urls)].values();
  const loadNext = () => {
    if (preview.parentElement !== coverHoverPreview) return;
    const next = alternatives.next();
    if (next.done) hideCoverHoverPreview();
    else preview.src = next.value;
  };
  preview.addEventListener("error", loadNext);
  coverHoverPreview.append(preview);
  loadNext();
}

function preloadCover(url) {
  if (!url || coverPreloads.has(url) || window.matchMedia("(max-width: 600px)").matches) return;
  const image = new Image();
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.src = url;
  coverPreloads.set(url, image);
}

function positionHardlinkPreview() {
  if (!hardlinkAnchor?.isConnected) return hideHardlinkPreview();
  const bounds = hardlinkAnchor.getBoundingClientRect();
  const box = hardlinkPreview.getBoundingClientRect();
  const width = document.documentElement.clientWidth;
  const height = window.innerHeight;
  const left = bounds.left + box.width <= width - 8 ? bounds.left : Math.max(8, width - box.width - 8);
  hardlinkPreview.style.left = `${Math.min(left, Math.max(8, width - box.width - 8))}px`;
  const below = bounds.bottom + 8 + box.height <= height - 8;
  hardlinkPreview.style.top = `${Math.max(8, Math.min(below ? bounds.bottom + 8 : bounds.top - box.height - 8, height - box.height - 8))}px`;
}

async function showHardlinkPreview(anchor, subscriptionId, code, file) {
  hideHardlinkPreview();
  hardlinkAnchor = anchor;
  anchor.setAttribute("aria-describedby", hardlinkPreview.id);
  hardlinkPreview.hidden = false;
  const heading = document.createElement("strong");
  heading.className = "hardlink-preview-title";
  heading.textContent = "硬链接路径";
  const paths = document.createElement("pre");
  paths.className = "hardlink-preview-paths";
  paths.textContent = "正在读取路径…";
  hardlinkPreview.replaceChildren(heading, paths);
  positionHardlinkPreview();
  const key = `${subscriptionId}:${file.path}`;
  if (!videoHardlinkRequests.has(key)) {
    const request = api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/hardlinks`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path })
    }).catch((error) => {
      videoHardlinkRequests.delete(key);
      throw error;
    });
    videoHardlinkRequests.set(key, request);
  }
  try {
    const result = await videoHardlinkRequests.get(key);
    if (hardlinkAnchor !== anchor) return;
    paths.textContent = (result.paths || []).join("\n") || "没有找到已索引的硬链接路径。";
    positionHardlinkPreview();
  } catch (error) {
    if (hardlinkAnchor !== anchor) return;
    paths.textContent = `读取硬链接路径失败：${error.message}`;
    positionHardlinkPreview();
  }
}

function sortCoverFiles(files) {
  return [...files].sort((a, b) => {
    for (let i = 0; i < 3; i++) {
      const difference = (b.rank?.[i] || 0) - (a.rank?.[i] || 0);
      if (difference) return difference;
    }
    return 0;
  });
}

window.addEventListener("resize", hideHardlinkPreview);
window.addEventListener("resize", hideCoverHoverPreview);
window.addEventListener("scroll", hideCoverHoverPreview, true);

const coverDialog = $("#cover-dialog");
const coverGallery = $("#cover-gallery");
const coverGalleryImages = $("#cover-gallery-images");
const coverGalleryMessage = $("#cover-gallery-message");
const coverOpenMessage = $("#cover-open-message");
let coverGalleryLoad = 0;

async function openGalleryImage(button, localPath) {
  const load = coverGalleryLoad;
  coverOpenMessage.hidden = true;
  button.disabled = true;
  try {
    if (fnosApp.isWeb && (fnosApp.isStandaloneWeb || window.parent === window)) {
      throw new Error("请在飞牛客户端中打开原图。");
    }
    const path = await localPath;
    if (!coverDialog.open || load !== coverGalleryLoad) return;
    if (!path) throw new Error("图片尚未保存到飞牛，请先检查订阅或修复数据。");
    await fnosApp.openFile(path);
  } catch (error) {
    if (coverDialog.open && load === coverGalleryLoad) {
      coverOpenMessage.textContent = `无法用飞牛打开图片：${error.message || error}`;
      coverOpenMessage.hidden = false;
    }
  } finally {
    button.disabled = false;
  }
}

function appendGalleryImage(label, url, lazy, localPath) {
  const figure = document.createElement("figure");
  const caption = document.createElement("figcaption");
  if (!url) {
    figure.className = "cover-gallery-missing";
    caption.textContent = `${label}暂不可用`;
    figure.append(caption);
    coverGalleryImages.append(figure);
    return;
  }
  const button = makeButton("", "gallery-image", (event) => openGalleryImage(event.currentTarget, localPath), `用飞牛打开${label}`);
  button.setAttribute("aria-label", button.title);
  const image = document.createElement("img");
  image.alt = "";
  image.loading = lazy ? "lazy" : "eager";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  caption.textContent = label;
  image.addEventListener("error", () => {
    caption.textContent = `${label}加载失败`;
  });
  image.src = url;
  button.append(image);
  figure.append(button, caption);
  coverGalleryImages.append(figure);
}

function appendGalleryMetadata(metadata) {
  if (!metadata) return;
  const entries = Object.entries(metadata.base_info || {}).filter(([label]) =>
    !metadata.series || !["系列", "シリーズ", "Series"].includes(label));
  if (metadata.series) entries.unshift(["系列", metadata.series]);
  if (!entries.length && !metadata.title && !metadata.text_data) return;
  const details = document.createElement("section");
  details.className = "cover-gallery-details";
  if (metadata.title) {
    const title = document.createElement("h3");
    title.className = "cover-gallery-title";
    title.textContent = metadata.title;
    details.append(title);
  }
  if (entries.length) {
    const list = document.createElement("dl");
    for (const [label, value] of entries) {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = Array.isArray(value) ? value.join("、") : value && typeof value === "object" ? JSON.stringify(value) : String(value ?? "");
      list.append(term, definition);
    }
    details.append(list);
  }
  if (metadata.text_data) {
    const title = document.createElement("h3");
    title.textContent = "影片简介";
    const synopsis = document.createElement("p");
    synopsis.textContent = metadata.text_data;
    details.append(title, synopsis);
  }
  coverGalleryImages.append(details);
}

async function openCoverViewer(code, url, subscriptionId, coverPathKey) {
  const load = ++coverGalleryLoad;
  $("#cover-title").textContent = code;
  coverOpenMessage.hidden = true;
  coverGalleryImages.replaceChildren();
  coverGalleryMessage.textContent = "正在加载截图…";
  coverGalleryMessage.hidden = false;
  if (fnosApp.isWeb === false) {
    coverDialog.classList.add("native-gallery");
    coverDialog.show();
  } else {
    coverDialog.classList.remove("native-gallery");
    coverDialog.showModal();
  }
  coverGallery.scrollTop = 0;
  coverGallery.focus({ preventScroll: true });

  const galleryRequest = api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/gallery`);
  const coverPath = coverPathKey ? galleryRequest.then((gallery) => gallery[coverPathKey], () => "") : "";
  appendGalleryImage("封面", url, false, coverPath);
  try {
    const { screenshots, metadata } = await galleryRequest;
    if (!coverDialog.open || load !== coverGalleryLoad) return;
    appendGalleryMetadata(metadata);
    for (const screenshot of screenshots) {
      appendGalleryImage(`截图 ${screenshot.position}`, appUrl(screenshot.url), true, screenshot.path);
    }
    coverGalleryMessage.textContent = "暂无已保存的截图。";
    coverGalleryMessage.hidden = screenshots.length > 0;
  } catch (error) {
    if (!coverDialog.open || load !== coverGalleryLoad) return;
    coverGalleryMessage.textContent = `读取截图失败：${error.message}`;
  }
}

$("#cover-close").addEventListener("click", () => coverDialog.close());
coverDialog.addEventListener("click", (event) => {
  if (event.target === coverDialog) coverDialog.close();
});
coverDialog.addEventListener("close", () => {
  coverGalleryLoad++;
  coverGalleryImages.replaceChildren();
});

function updateVideoRefreshButton(button, state) {
  const pending = state?.state === "pending";
  const success = state?.state === "success";
  const failed = state?.state === "error";
  const waiting = state?.state === "waiting";
  const skipped = state?.state === "skipped";
  button.disabled = Boolean(pending || state?.locked);
  button.classList.toggle("is-refreshing", Boolean(pending));
  button.classList.toggle("refresh-success", Boolean(success));
  button.classList.toggle("refresh-error", Boolean(failed));
  button.classList.toggle("refresh-waiting", Boolean(waiting || skipped));
  button.setAttribute("aria-busy", String(Boolean(pending)));
  const seconds = pending && state.showElapsed !== false ? Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000)) : 0;
  const label = pending ? `${state.phaseLabel || "修复中"}${seconds ? ` ${formatLogDuration(seconds)}` : "…"}`
    : state?.phaseLabel || (success ? "已修复" : failed ? "重试" : "");
  button.replaceChildren();
  const icon = document.createElement("span");
  icon.className = "video-refresh-icon";
  icon.textContent = success ? "✓" : failed ? "!" : waiting || skipped ? "·" : "↻";
  icon.setAttribute("aria-hidden", "true");
  button.append(icon);
  if (label) {
    const text = document.createElement("span");
    text.textContent = label;
    button.append(text);
  }
  button.title = pending ? (state.message || "正在核对 ID、刷新信息、检查字幕和图片，请稍候。")
    : success || state?.locked ? (state.message || "")
    : failed ? `${state.message}\n点击重试修复此视频数据。`
    : "修复此视频数据：归档到番号目录，核对 M-Team ID 和 MediaInfo，补齐字幕、封面和截图。";
  button.setAttribute("aria-label", `${button.dataset.filename}：${label || "修复此视频数据"}${failed ? `，${state.message}` : ""}`);
}

function currentVideoRepairStatus(button) {
  const { subscriptionId, code, path, refreshKey } = button.dataset;
  const job = activeRepairForVideo(repairJobs, subscriptionId, path);
  return job ? videoRepairStatus(job, code, path) : videoRefreshStates.get(refreshKey);
}

async function refreshVideoInformation(subscriptionId, code, file) {
  const key = videoRefreshKey(subscriptionId, file.path);
  if (videoRefreshStates.get(key)?.state === "pending" || activeRepairForVideo(repairJobs, subscriptionId, file.path)) return;
  videoRefreshStates.set(key, { state: "pending", startedAt: Date.now(), phaseLabel: "正在排队" });
  renderExistingVideos();
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/repair`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path, code })
    });
    videoRefreshStates.delete(key);
    awaitedRepairJobs.add(result.job_id);
    repairJobs = [...repairJobs.filter((job) => job.job_id !== result.job_id), result];
    renderDataRepairStatus(repairJobs);
    toast(result.message || "视频数据修复已排队。");
  } catch (error) {
    videoRefreshStates.set(key, { state: "error", message: error.message });
    toast(`${code} 修复失败：${error.message}`, true);
    renderExistingVideos();
  }
}

function renderBlacklistTags() {
  const container = $("#blacklist-tags");
  const input = $("#blacklist-input");
  container.replaceChildren();
  for (const code of blacklistNumbers) {
    const tag = document.createElement("span");
    tag.className = "blacklist-tag";
    const text = document.createElement("span");
    text.textContent = code;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "blacklist-remove";
    remove.textContent = "×";
    remove.title = `移除 ${code}`;
    remove.setAttribute("aria-label", `移除黑名单番号 ${code}`);
    remove.addEventListener("click", () => {
      blacklistNumbers = blacklistNumbers.filter((item) => item !== code);
      renderBlacklistTags();
      input.focus();
    });
    tag.append(text, remove);
    container.append(tag);
  }
  container.append(input);
}

function addBlacklistNumber() {
  const input = $("#blacklist-input");
  const value = input.value.trim();
  if (!value) return;
  const match = value.match(/(?:FC2[- _]?PPV[- _]*\d{3,8}|[A-Za-z0-9]{2,10}[- _]+\d{2,8})/i);
  if (!match || match[0] !== value) {
    toast("请输入有效番号，例如 SSNI-939", true);
    return;
  }
  const code = value.replace(/\s+/g, "-").replace(/_/g, "-").toUpperCase().replace(/^(FC2)-?(PPV)-?/, "FC2-PPV-");
  if (!blacklistNumbers.includes(code)) blacklistNumbers.push(code);
  input.value = "";
  renderBlacklistTags();
  input.focus();
}

function renderSubscriptions() {
  const list = $("#subscription-menu");
  const mobilePicker = $("#mobile-subscription-picker");
  subscriptionAvatarObserver?.disconnect();
  if (list.contains(actressProfileAnchor)) hideActressProfile();
  list.replaceChildren();
  mobilePicker.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = subscriptions.length ? `选择订阅（${subscriptions.length}）` : "暂无订阅";
  mobilePicker.append(placeholder);
  for (const item of subscriptions) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.performer} (${Number(item.video_count) || 0})`;
    mobilePicker.append(option);
  }
  mobilePicker.value = selectedSubscriptionId;
  if (!subscriptions.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有订阅";
    list.append(empty);
  } else {
    for (const item of subscriptions) {
      const row = document.createElement("div");
      row.className = `subscription-nav-row${activeView === "subscription" && selectedSubscriptionId === item.id ? " active" : ""}`;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "subscription-nav";
      const dot = document.createElement("span");
      dot.className = "subscription-dot";
      const label = document.createElement("span");
      label.className = "subscription-nav-label";
      label.dataset.subscriptionId = item.id;
      label.textContent = `${item.performer}(${Number(item.video_count) || 0})`;
      button.append(dot, label);
      button.addEventListener("click", () => showSubscription(item.id));
      bindSubscriptionActressProfile(button, item, true);
      if (button.actress.profile?.avatar_url) subscriptionAvatarObserver?.observe(button);
      const actions = document.createElement("div");
      actions.className = "subscription-nav-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "subscription-action";
      edit.textContent = "✎";
      edit.title = `快速编辑 ${item.performer}`;
      edit.setAttribute("aria-label", edit.title);
      edit.addEventListener("click", () => openSubscription(item));
      const check = document.createElement("button");
      check.type = "button";
      check.className = "subscription-action quick-check";
      check.textContent = "↻";
      check.title = `立即检查 ${item.performer}`;
      check.dataset.defaultTitle = check.title;
      check.setAttribute("aria-label", check.title);
      check.disabled = busy;
      check.addEventListener("click", () => runScan(item.id));
      actions.append(edit, check);
      row.append(button, actions);
      list.append(row);
    }
  }
  renderPage();
}

function hideActressProfile() {
  actressProfileAnchor?.removeAttribute("aria-describedby");
  actressProfileAnchor = null;
  actressProfilePopup.hidden = true;
}

function showActressProfile(button) {
  hideActressProfile();
  actressProfileAnchor = button;
  button.setAttribute("aria-describedby", actressProfilePopup.id);
  const actress = button.actress;
  const profile = actress.profile;
  const title = [...new Set([actress.chinese_name || actress.display_name, profile?.name || actress.source_name, profile?.romanized_name]
    .map((name) => name?.trim()).filter(Boolean))].join(" / ");
  const years = profile?.active_start
    ? `${profile.active_start}${profile.active_end ? `–${profile.active_end}` : " 起"}`
    : profile?.active_end || "";
  const fields = [["名称", title], ["日文读音", profile?.reading], ["生日", profile?.birthday],
    ["身高", profile?.height_cm ? `${profile.height_cm} cm` : ""], ["出身", profile?.origin],
    ["血型", profile?.blood_type], ["活跃年份", years]];
  const list = document.createElement("dl");
  for (const [key, value] of fields) {
    if (!profile && key !== "名称") continue;
    const row = document.createElement("div");
    const label = document.createElement("dt");
    const data = document.createElement("dd");
    label.textContent = key;
    data.textContent = value || "—";
    row.append(label, data);
    list.append(row);
  }
  const content = [list];
  if (!profile) {
    const status = document.createElement("p");
    status.textContent = button.profileError || (actress.profile_pending ? "正在读取资料…" : "暂无资料");
    content.push(status);
  }
  const thumbnail = button.querySelector("img");
  const url = profile?.avatar_url;
  const photo = url ? (thumbnail?.getAttribute("src") === url ? thumbnail.cloneNode(false) : createActressImage(url)) : null;
  actressProfilePopup.classList.toggle("has-photo", Boolean(photo));
  if (photo) {
    photo.className = "actress-profile-photo";
    photo.loading = "eager";
    photo.addEventListener("error", () => {
      if (photo.parentNode !== actressProfilePopup) return;
      photo.remove();
      actressProfilePopup.classList.remove("has-photo");
    }, { once: true });
    content.unshift(photo);
  }
  actressProfilePopup.replaceChildren(...content);
  actressProfilePopup.hidden = false;
  positionActressProfile();
}

function positionActressProfile() {
  if (!actressProfileAnchor) return;
  const anchor = actressProfileAnchor.getBoundingClientRect();
  if (anchor.bottom <= 0 || anchor.top >= window.innerHeight || anchor.right <= 0 || anchor.left >= window.innerWidth) {
    hideActressProfile();
    return;
  }
  const panel = actressProfilePopup.getBoundingClientRect();
  const left = anchor.right + panel.width + 8 <= window.innerWidth ? anchor.right + 8
    : anchor.left - panel.width - 8 >= 8 ? anchor.left - panel.width - 8
    : anchor.left;
  const top = left === anchor.left ? anchor.bottom + 6 : anchor.top;
  actressProfilePopup.style.left = `${Math.max(8, Math.min(left, window.innerWidth - panel.width - 8))}px`;
  actressProfilePopup.style.top = `${Math.max(8, Math.min(top, window.innerHeight - panel.height - 8))}px`;
}

function createActressImage(url, loading = "eager") {
  const image = document.createElement("img");
  image.alt = "";
  image.loading = loading;
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.src = url;
  return image;
}

function paintActressAvatar(button) {
  const url = button.actress.profile?.avatar_url;
  if (!url || button.dataset.avatarUrl === url) return;
  button.dataset.avatarUrl = url;
  const image = createActressImage(url, "lazy");
  image.addEventListener("load", () => button.replaceChildren(image), { once: true });
  button.append(image);
}

function queueActressProfile(button) {
  if (!button.actress.profile_pending) return;
  actressProfileQueue.set(button.actress.source_name, button);
  void loadQueuedActressProfile();
}

async function loadQueuedActressProfile() {
  if (actressProfileLoading || activeView !== "actresses" || !actressProfileQueue.size) return;
  if (Date.now() < actressProfileRetryAt) {
    if (!actressProfileRetryTimer) actressProfileRetryTimer = window.setTimeout(() => {
      actressProfileRetryTimer = 0;
      void loadQueuedActressProfile();
    }, actressProfileRetryAt - Date.now());
    return;
  }
  const [sourceName, button] = actressProfileQueue.entries().next().value;
  actressProfileQueue.delete(sourceName);
  if (!button.isConnected || !button.actress.profile_pending) {
    void loadQueuedActressProfile();
    return;
  }
  actressProfileLoading = true;
  try {
    const result = await api("/api/actresses/profile", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_name: sourceName }),
    });
    const current = actresses.find((actress) => actress.source_name === sourceName);
    if (current) Object.assign(current, result);
    Object.assign(button.actress, result);
    for (const avatar of $("#actress-body").querySelectorAll(".actress-avatar")) {
      if (avatar.actress.source_name !== sourceName) continue;
      Object.assign(avatar.actress, result);
      avatar.profileError = result.profile_pending ? "未配置 M-Team 令牌" : "";
      paintActressAvatar(avatar);
      updateActressShowcaseLink(avatar.closest("tr")?.querySelector(".actress-showcase-link"), result.profile);
      if (actressProfileAnchor === avatar) showActressProfile(avatar);
    }
    if (result.profile_pending) actressProfileRetryAt = Date.now() + 60000;
  } catch (error) {
    button.profileError = error.message;
    if (actressProfileAnchor === button) showActressProfile(button);
    actressProfileRetryAt = Date.now() + 60000;
  } finally {
    actressProfileLoading = false;
    void loadQueuedActressProfile();
  }
}

function makeActressAvatar(actress) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "actress-avatar";
  button.actress = actress;
  button.setAttribute("aria-label", `${actress.source_name}的资料`);
  const fallback = document.createElement("span");
  fallback.setAttribute("aria-hidden", "true");
  fallback.textContent = Array.from(actress.source_name)[0] || "·";
  button.append(fallback);
  paintActressAvatar(button);
  bindActressProfileTrigger(button, () => queueActressProfile(button));
  if (actress.profile_pending) actressProfileObserver?.observe(button);
  return button;
}

function bindActressProfileTrigger(button, loadProfile, hoverOnly = false) {
  const reveal = () => {
    showActressProfile(button);
    loadProfile();
  };
  button.addEventListener("pointerenter", (event) => { if (event.pointerType === "mouse") reveal(); });
  button.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse" && actressProfileAnchor === button && (hoverOnly || !button.matches(":focus-visible"))) hideActressProfile();
  });
  if (hoverOnly) return;
  button.addEventListener("focus", () => { if (button.matches(":focus-visible")) reveal(); });
  button.addEventListener("blur", () => { if (actressProfileAnchor === button) hideActressProfile(); });
  button.addEventListener("click", () => { if (actressProfileAnchor === button) hideActressProfile(); else reveal(); });
}

function subscriptionActressProfileState(subscription) {
  let state = subscriptionActressProfiles.get(subscription.id);
  if (!state || state.actress.source_name !== subscription.performer) {
    state = { actress: { source_name: subscription.performer, display_name: subscription.performer, profile_pending: true }, cached: null };
    subscriptionActressProfiles.set(subscription.id, state);
  }
  const cached = subscription.actress_profile;
  if (cached && state.cached !== cached) {
    if (JSON.stringify(cached) !== JSON.stringify(state.cached)) Object.assign(state.actress, cached);
    state.cached = cached;
  }
  return state;
}

function bindSubscriptionActressProfile(button, subscription, hoverOnly = false) {
  const state = subscriptionActressProfileState(subscription);
  button.actress = state.actress;
  bindActressProfileTrigger(button, async () => {
    if (!state.actress.profile_pending) return;
    button.profileError = "";
    if (!state.request) {
      const path = `/api/subscriptions/${encodeURIComponent(subscription.id)}/actress-profile`;
      state.request = api(path, { method: "POST" }).then((result) => {
        Object.assign(state.actress, result);
      }).finally(() => { state.request = null; });
    }
    try {
      await state.request;
      if (state.actress.profile_pending) button.profileError = "未配置 M-Team 令牌";
    } catch (error) {
      button.profileError = error.message;
    }
    if (actressProfileAnchor === button) showActressProfile(button);
  }, hoverOnly);
}

function renderSubscriptionTitle(subscription) {
  const title = $("#selected-title");
  const existing = title.firstElementChild;
  if (existing && existing.dataset.subscriptionId === subscription?.id && existing.textContent === subscription.performer) {
    subscriptionActressProfileState(subscription);
    return;
  }
  if (actressProfileAnchor === existing) hideActressProfile();
  if (!subscription) {
    title.textContent = "订阅已删除";
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "subscription-actress-name";
  button.dataset.subscriptionId = subscription.id;
  button.textContent = subscription.performer;
  bindSubscriptionActressProfile(button, subscription);
  title.replaceChildren(button);
}

document.addEventListener("pointerdown", (event) => {
  if (actressProfileAnchor && !actressProfileAnchor.contains(event.target) && !actressProfilePopup.contains(event.target)) hideActressProfile();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideActressProfile(); });
window.addEventListener("scroll", (event) => {
  if (!(event.target instanceof Node && actressProfilePopup.contains(event.target))) positionActressProfile();
}, true);
window.addEventListener("resize", hideActressProfile);

function updateActressShowcaseLink(link, profile) {
  if (!link) return;
  const id = String(profile?.showcase_id ?? "");
  if (/^\d+$/.test(id)) {
    link.href = `https://kp.m-team.cc/showcaseDetail?id=${id}`;
    link.title = "在 M-Team 查看女优资料";
  } else {
    link.removeAttribute("href");
    link.removeAttribute("title");
  }
}

function renderActresses(message = "") {
  const body = $("#actress-body");
  hideActressProfile();
  actressProfileObserver?.disconnect();
  actressProfileQueue.clear();
  body.replaceChildren();
  renderActressMergeOptions();
  const groups = new Map();
  for (const actress of actresses) {
    const canonicalName = actress.canonical_name || actress.source_name;
    if (!groups.has(canonicalName)) groups.set(canonicalName, []);
    groups.get(canonicalName).push(actress);
  }
  const count = $("#actress-count");
  count.textContent = `${groups.size} 位`;
  if (!groups.size) {
    const row = document.createElement("tr");
    row.className = "actress-empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty";
    cell.textContent = message || "暂无女优";
    row.append(cell);
    body.append(row);
    return;
  }
  const orderedGroups = [...groups].map(([canonicalName, members]) => {
    const actress = members.find((member) => member.source_name === canonicalName) || members[0];
    return { canonicalName, members, actress, hasChineseName: Boolean(actress.chinese_name?.trim()) };
  }).sort((a, b) => Number(a.hasChineseName) - Number(b.hasChineseName));
  for (const { canonicalName, members, actress } of orderedGroups) {
    const aliases = members.filter((member) => member.source_name !== canonicalName);
    const row = document.createElement("tr");
    const source = document.createElement("td");
    source.className = "actress-name actress-name-cell";
    const identity = document.createElement("div");
    identity.className = "actress-identity";
    const names = document.createElement("div");
    const name = document.createElement("strong");
    const showcase = document.createElement("a");
    showcase.className = "actress-showcase-link";
    showcase.textContent = canonicalName;
    updateActressShowcaseLink(showcase, actress.profile);
    useSystemBrowser(showcase);
    name.append(showcase);
    names.append(name);
    if (aliases.length) {
      const aliasList = document.createElement("span");
      aliasList.className = "actress-aliases";
      aliasList.textContent = `别名：${aliases.map((alias) => alias.source_name).join("、")}`;
      names.append(aliasList);
    }
    identity.append(makeActressAvatar(actress), names);
    source.append(identity);
    const translationCell = document.createElement("td");
    translationCell.className = "actress-translation-cell";
    const translationRow = document.createElement("div");
    translationRow.className = "actress-translation-row";
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 120;
    input.value = actressDrafts.has(actress.source_name)
      ? actressDrafts.get(actress.source_name)
      : actress.chinese_name || "";
    input.placeholder = "填写中文名";
    input.setAttribute("aria-label", `${actress.source_name} 的中文名`);
    input.addEventListener("input", () => actressDrafts.set(actress.source_name, input.value));
    const state = document.createElement("span");
    state.className = "actress-save-state";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "secondary";
    save.textContent = "保存";
    save.addEventListener("click", () => saveActressTranslation(actress.source_name, input.value, save, state));
    translationRow.append(input, state);
    translationCell.append(translationRow);
    const action = document.createElement("td");
    action.className = "actress-action-cell";
    const subscribe = document.createElement("button");
    subscribe.type = "button";
    subscribe.className = "secondary actress-action-button actress-subscribe";
    const chineseName = actressDrafts.has(actress.source_name) ? actressDrafts.get(actress.source_name).trim() : (actress.chinese_name || "").trim();
    const sourceNames = new Set(members.map((member) => member.source_name));
    const alreadySubscribed = subscriptions.some((subscription) =>
      sourceNames.has(subscription.performer) || subscription.performer === chineseName
    );
    subscribe.textContent = alreadySubscribed ? "已添加" : "添加订阅";
    subscribe.classList.toggle("is-added", alreadySubscribed);
    subscribe.disabled = !chineseName || alreadySubscribed;
    subscribe.title = alreadySubscribed ? "该女优已添加订阅" : chineseName ? "添加此女优订阅" : "请先填写并保存中文名，再添加订阅";
    subscribe.addEventListener("click", () => openSubscription(null, chineseName));
    const subscribeWrap = document.createElement("span");
    subscribeWrap.className = "actress-subscribe-wrap";
    subscribeWrap.title = subscribe.title;
    subscribeWrap.append(subscribe);
    const search = document.createElement("a");
    search.className = "secondary actress-action-button actress-mteam-search";
    search.textContent = "M-Team 搜索";
    search.href = `https://kp.m-team.cc/browse/adult?keyword=${encodeURIComponent(`${chineseName} ltx`)}`;
    search.target = "_blank";
    search.rel = "noopener noreferrer";
    search.setAttribute("aria-label", `在 M-Team 搜索 ${chineseName} ltx`);
    useSystemBrowser(search);
    search.setAttribute("aria-disabled", String(!chineseName));
    if (!chineseName) search.removeAttribute("href");
    save.classList.add("actress-action-button", "actress-save");
    action.append(save, subscribeWrap, search);
    row.append(source, translationCell, action);
    body.append(row);
  }
}

function renderActressMergeOptions() {
  const sourceSelect = $("#actress-merge-source");
  const targetSelect = $("#actress-merge-target");
  const previousSource = sourceSelect.value;
  const previousTarget = targetSelect.value;
  const actressesByName = new Map(actresses.map((actress) => [actress.source_name, actress]));
  const displayName = (actress) => {
    const canonicalName = actress.canonical_name || actress.source_name;
    const canonical = actressesByName.get(canonicalName);
    const chineseName = (canonical?.chinese_name || actress.chinese_name || "").trim();
    return chineseName ? `${actress.source_name}（${chineseName}）` : actress.source_name;
  };
  sourceSelect.replaceChildren();
  targetSelect.replaceChildren();
  const targetEmpty = document.createElement("option");
  targetEmpty.value = "";
  targetEmpty.textContent = "不归并（独立名称）";
  targetSelect.append(targetEmpty);
  for (const actress of actresses) {
    const sourceOption = document.createElement("option");
    sourceOption.value = actress.source_name;
    sourceOption.textContent = displayName(actress);
    sourceSelect.append(sourceOption);
    if (!actress.canonical_name) {
      const targetOption = document.createElement("option");
      targetOption.value = actress.source_name;
      targetOption.textContent = displayName(actress);
      targetSelect.append(targetOption);
    }
  }
  if ([...sourceSelect.options].some((option) => option.value === previousSource)) sourceSelect.value = previousSource;
  if ([...targetSelect.options].some((option) => option.value === previousTarget)) targetSelect.value = previousTarget;
}

async function loadActresses() {
  if (actressesLoading) return actressesLoading;
  const revision = actressRequestRevision;
  lastActressesRefresh = Date.now();
  actressesLoading = api("/api/actresses").then((result) => {
    if (revision !== actressRequestRevision) return;
    actresses = result;
    if (activeView === "actresses" && !actressEditorIsActive()) renderActresses();
  }).catch((error) => {
    toast(`读取女优列表失败：${error.message}`, true);
  }).finally(() => {
    actressesLoading = null;
  });
  return actressesLoading;
}

function actressEditorIsActive() {
  return Boolean(actressProfileAnchor || document.querySelector("#actress-body input:focus, #actress-body .actress-save:disabled"));
}

async function saveActressTranslation(sourceName, chineseName, button, state) {
  if (button.disabled) return;
  chineseName = chineseName.trim();
  actressRequestRevision++;
  button.disabled = true;
  button.textContent = "保存中";
  state.textContent = "";
  try {
    const result = await api("/api/actresses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_name: sourceName, chinese_name: chineseName })
    });
    actresses = actresses.map((actress) => actress.source_name === sourceName ? { ...actress, chinese_name: result.chinese_name } : actress);
    actressDrafts.delete(sourceName);
    const input = button.closest("tr")?.querySelector("input");
    if (input) input.value = result.chinese_name || "";
    state.textContent = result.chinese_name ? "已保存" : "已清除";
    if (activeView === "actresses") renderActresses();
    toast(result.chinese_name ? `${sourceName} 的中文名已保存。` : `${sourceName} 的手动译名已清除。`);
  } catch (error) {
    state.textContent = "保存失败";
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "保存";
  }
}

async function mergeActress(sourceName, canonicalName, button, state) {
  if (button.disabled) return;
  button.disabled = true;
  state.textContent = "保存中…";
  try {
    await api("/api/actresses/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_name: sourceName, canonical_name: canonicalName }),
    });
    actressRequestRevision++;
    if (actressesLoading) await actressesLoading;
    await loadActresses();
    state.textContent = "归并已保存";
  } catch (error) {
    state.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderPage() {
  const settings = activeView === "settings";
  const all = activeView === "all";
  const actressPage = activeView === "actresses";
  if (!actressPage) actressProfileQueue.clear();
  const profileView = actressPage ? "#actresses-view" : activeView === "subscription" ? "#subscription-view" : null;
  if (actressProfileAnchor && !actressProfileAnchor.closest("#subscription-menu")
    && (!profileView || !actressProfileAnchor.closest(profileView))) hideActressProfile();
  $("#settings-view").hidden = !settings;
  $("#actresses-view").hidden = !actressPage;
  $("#subscription-view").hidden = settings || all || actressPage;
  $("#dashboard-activity").hidden = !all;
  $("#mobile-subscription-bar").hidden = settings || actressPage;
  $("#log-section").hidden = settings || actressPage;
  $("#clear-logs").hidden = settings || actressPage || (activeView === "subscription" && !subscriptions.some((item) => item.id === selectedSubscriptionId));
  $("#clear-logs").textContent = activeView === "subscription" ? "清理当前女优日志" : "清理全部日志";
  for (const button of document.querySelectorAll(".nav-item")) {
    const active = button.dataset.view === activeView;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  renderSubscriptionTitle(selected);
  const description = $("#selected-description");
  description.replaceChildren();
  if (!selected) description.textContent = "查看订阅设置";
  $("#selected-scan").hidden = all;
  $("#selected-repair-data").hidden = all || !selected;
  $("#selected-edit").hidden = all || !selected;
  $("#selected-delete").hidden = all || !selected;
  const summary = $("#subscription-summary");
  summary.replaceChildren();
  if (selected) {
    const downloadPath = mapDownloaderPath(selected.download_dir).replace(/\/$/, "");
    const details = [["搜索关键字", subscriptionSearchKeywords(selected)], ["飞牛目录", selected.download_dir], ["下载器路径", `${downloadPath}/〈番号〉`], ["自动洗版", selected.wash_enabled ? "开启" : "关闭"]];
    for (const [labelText, valueText] of details) {
      const item = document.createElement("div");
      item.className = "detail-item";
      if (labelText === "飞牛目录" || labelText === "下载器路径") item.classList.add("detail-path");
      if (labelText === "自动洗版") item.classList.add("detail-wash");
      const label = document.createElement("span");
      label.textContent = labelText;
      const value = document.createElement("strong");
      if (labelText === "搜索关键字") {
        value.className = "search-keyword-links";
        for (const [index, keyword] of valueText.entries()) {
          const link = document.createElement("a");
          link.className = "subscription-search-link";
          link.textContent = keyword;
          link.href = "https://kp.m-team.cc/browse/adult?keyword=" + encodeURIComponent(keyword);
          link.title = "在 M-Team 搜索：" + keyword;
          link.setAttribute("aria-label", link.title);
          useSystemBrowser(link);
          value.append(link);
          if (index < valueText.length - 1) value.append(document.createTextNode("，"));
        }
      } else {
        value.textContent = valueText;
        value.title = valueText;
      }
      item.append(label, value);
      summary.append(item);
    }
    if (existingForId !== selected.id) loadExistingVideos(selected.id);
  } else {
    existingForId = "";
    existingVideos = [];
    existingVideoCount = 0;
    existingVideosLoading = false;
    existingVideosError = "";
  }
  renderActivity();
  renderLogs(logEntries);
  const scopeKey = activeView === "subscription"
    ? `subscription:${selected?.performer || ""}`
    : activeView === "all" ? "all" : "";
  if (scopeKey && scopeKey !== lastLogScopeKey) {
    lastLogScopeKey = scopeKey;
    void reloadLogsForFilters();
  }
}

function showSubscription(id) {
  selectedSubscriptionId = id;
  activeView = "subscription";
  existingForId = "";
  renderSubscriptions();
}

async function openVideoNfo(subscriptionId, code, file) {
  const request = ++nfoLoad;
  const viewer = $("#nfo-dialog");
  const title = $("#nfo-title");
  const message = $("#nfo-message");
  const content = $("#nfo-content");
  title.textContent = file.name;
  message.textContent = "正在读取 NFO…";
  message.hidden = false;
  content.textContent = "";
  content.hidden = true;
  if (!viewer.open) viewer.showModal();
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/nfo`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path })
    });
    if (!viewer.open || request !== nfoLoad) return;
    title.textContent = result.name;
    content.textContent = result.content;
    content.hidden = !result.content;
    message.textContent = "NFO 文件为空。";
    message.hidden = Boolean(result.content);
  } catch (error) {
    if (viewer.open && request === nfoLoad) message.textContent = error.message;
  }
}

function galleryThumbnail(item, subscriptionId) {
  const poster = item.poster_image ? appUrl(item.poster_image) : "";
  const localCover = item.cover_image ? appUrl(item.cover_image) : "";
  const remoteCover = sortCoverFiles(item.files || []).find((file) => /^https?:\/\//i.test(file.cover_url || ""))?.cover_url || "";
  const preview = item.screenshot_thumbnail ? appUrl(item.screenshot_thumbnail) : "";
  const covers = [localCover, remoteCover].filter(Boolean);
  preloadCover(covers[0]);
  const thumbnails = [...new Set([poster, ...covers, preview].filter(Boolean))];
  const failed = new Set();
  let displayed = "";
  const thumbnail = document.createElement("button");
  thumbnail.type = "button";
  thumbnail.className = "cover-thumbnail";
  thumbnail.setAttribute("aria-label", `查看 ${item.code} 封面和截图`);
  const image = document.createElement("img");
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  function showNextThumbnail() {
    displayed = thumbnails.shift() || "";
    thumbnail.classList.toggle("is-poster", Boolean(displayed && displayed === poster));
    if (displayed) {
      image.src = displayed;
      thumbnail.append(image);
    } else {
      image.remove();
      thumbnail.classList.add("cover-thumbnail-unavailable");
    }
  }
  image.addEventListener("error", () => {
    failed.add(displayed);
    showNextThumbnail();
  });
  showNextThumbnail();
  thumbnail.addEventListener("mouseenter", () => {
    showCoverHoverPreview(thumbnail, [displayed, ...covers, poster].filter((url) => url && !failed.has(url)));
  });
  thumbnail.addEventListener("mouseleave", hideCoverHoverPreview);
  thumbnail.addEventListener("click", () => {
    hideCoverHoverPreview();
    const coverUrl = (displayed && !failed.has(displayed))
      ? displayed
      : [...covers, poster].find((url) => url && !failed.has(url)) || "";
    const pathKey = coverUrl === localCover ? "cover_path" : coverUrl === poster ? "poster_path" : "";
    openCoverViewer(item.code, coverUrl, subscriptionId, pathKey);
  });
  return thumbnail;
}

function renderVideoNumber(code) {
  const label = document.createElement("span");
  label.className = "video-number-label";
  const text = document.createElement("span");
  text.textContent = code;
  label.append(text);
  if (!code) return label;
  const button = makeButton("", "video-number-copy", async () => {
    try {
      await writeClipboardText(code);
      toast(`已复制番号 ${code}`);
    } catch (error) {
      toast(`复制番号失败：${error.message}`, true);
    }
  }, `复制番号 ${code}`);
  button.setAttribute("aria-label", button.title);
  // Lucide Copy icon; see vendor/lucide-LICENSE.txt.
  button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
  label.append(button);
  return label;
}

function renderExistingVideos() {
  hideCoverHoverPreview();
  const body = $("#existing-body");
  body.replaceChildren();
  const numberKey = (value) => value.normalize("NFKC").toLowerCase().replace(/[\s_\-‐‑–—]+/g, "");
  const query = $("#existing-keyword").value;
  const numberQuery = query.normalize("NFKC").split(/\s+/).map(numberKey).join(" ");
  const visibleVideos = query.trim()
    ? existingVideos.filter((item) => matchesKeywords([numberKey(item.code || "")], numberQuery))
    : existingVideos;
  const visibleCount = visibleVideos.reduce((count, item) => count + (item.files || []).length, 0);
  $("#existing-count").textContent = query.trim() && existingVideos.length
    ? `${visibleCount} / ${existingVideoCount} 个视频` : `${existingVideoCount} 个视频`;
  if (!visibleVideos.length) {
    const row = document.createElement("tr");
    row.className = "existing-empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = existingVideosError || (existingVideosLoading ? "正在加载本地影片…" :
      existingVideos.length ? "没有匹配的番号。" :
      existingVideoCount > 0 ? `已扫描到 ${existingVideoCount} 个视频文件，但未能从文件名或所在文件夹识别番号。` : "暂未找到本地视频。");
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of visibleVideos) {
    const row = document.createElement("tr");
    row.className = "existing-number-row";
    const codeCell = document.createElement("td");
    codeCell.append(renderVideoNumber(item.code || ""));
    codeCell.className = "number-group-cell";
    row.append(codeCell);

    const coverCell = document.createElement("td");
    coverCell.className = "cover-cell";
    if (item.poster_image || item.cover_image || item.has_screenshots) {
      row.classList.add("has-local-video-artwork");
      coverCell.append(galleryThumbnail(item, selectedSubscriptionId));
    } else {
      coverCell.textContent = "—";
    }
    row.append(coverCell);

    const versionsCell = document.createElement("td");
    versionsCell.className = "version-list-cell";
    const filesCell = document.createElement("td");
    filesCell.className = "filename-cell";
    const subscriptionId = selectedSubscriptionId;
    for (const [fileIndex, file] of (item.files || []).entries()) {
      const rank = file.rank || [0, 0, 0];
      const rankNames = [file.rank_source, `${["其他", "1080p", "4K"][rank[0]] || "其他"}`, (["非中文", "中文字幕", "内嵌中文"][rank[1]] || "非中文"), ({ 1: "x264", 2: "x265" })[rank[2]] || "其他"].filter(Boolean);
      const versionLine = document.createElement(file.mteam_id ? "a" : "div");
      versionLine.className = "version-line";
      versionLine.textContent = rankNames.join(" · ");
      versionLine.title = rankNames.join(" · ");
      if (file.mteam_id) {
        const mainTitle = (file.rank_title || "").trim();
        const subtitle = (file.subtitle_raw ?? file.subtitle ?? "").trim();
        const titleLines = [];
        if (mainTitle) titleLines.push(`主标题：${mainTitle}`);
        if (subtitle && subtitle !== mainTitle) titleLines.push(`副标题：${subtitle}`);
        versionLine.title = titleLines.join("\n") || `在 M-Team 打开影片详情 ${file.mteam_id}`;
        versionLine.href = `https://kp.m-team.cc/detail/${encodeURIComponent(file.mteam_id)}`;
        useSystemBrowser(versionLine);
      }
      const versionRow = document.createElement("div");
      versionRow.className = `version-row${fileIndex ? " additional-version" : ""}`;
      versionRow.style.setProperty("--file-index", fileIndex);
      const actions = document.createElement("div");
      actions.className = "version-actions";
      if (!fileIndex) {
        const mobileNumber = document.createElement("div");
        mobileNumber.className = "mobile-video-number";
        mobileNumber.append(renderVideoNumber(item.code || ""));
        if (item.files.length > 1) {
          const count = document.createElement("span");
          count.className = "number-file-count";
          count.textContent = `${item.files.length} 个版本`;
          mobileNumber.append(count);
        }
        actions.append(mobileNumber);
      }
      const markers = document.createElement("span");
      markers.className = "version-markers";
      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.className = "video-refresh";
      refresh.dataset.refreshKey = videoRefreshKey(subscriptionId, file.path);
      refresh.dataset.filename = file.name;
      refresh.dataset.subscriptionId = subscriptionId;
      refresh.dataset.path = file.path;
      refresh.dataset.code = item.code;
      updateVideoRefreshButton(refresh, currentVideoRepairStatus(refresh));
      refresh.addEventListener("click", () => refreshVideoInformation(subscriptionId, item.code, file));
      markers.append(refresh);
      const subtitlePaths = file.subtitle_paths || [];
      const hasHardlinks = file.hardlink_count > 1;
      if (subtitlePaths.length || file.has_nfo || hasHardlinks) {
        if (subtitlePaths.length) {
          const marker = document.createElement("span");
          marker.className = "subtitle-marker";
          marker.textContent = "CC";
          marker.title = `本地字幕路径：\n${subtitlePaths.join("\n")}`;
          marker.setAttribute("aria-label", `存在 ${subtitlePaths.length} 个本地字幕文件`);
          markers.append(marker);
        }
        if (file.has_nfo) {
          const marker = makeButton("NFO", "nfo-marker", () => openVideoNfo(subscriptionId, item.code, file), "查看 NFO 内容");
          marker.setAttribute("aria-label", `查看 ${file.name} 的 NFO`);
          markers.append(marker);
        }
        if (hasHardlinks) {
          const marker = document.createElement("span");
          marker.className = "hardlink-marker";
          marker.textContent = "硬链接";
          marker.tabIndex = 0;
          marker.title = "悬停查看此视频的硬链接路径";
          marker.setAttribute("aria-label", `查看 ${file.name} 的硬链接路径`);
          marker.addEventListener("mouseenter", () => showHardlinkPreview(marker, subscriptionId, item.code, file));
          marker.addEventListener("focus", () => showHardlinkPreview(marker, subscriptionId, item.code, file));
          marker.addEventListener("mouseleave", hideHardlinkPreview);
          marker.addEventListener("blur", hideHardlinkPreview);
          markers.append(marker);
        }
      }
      actions.append(markers);
      versionRow.append(versionLine, actions);
      versionsCell.append(versionRow);

      const line = document.createElement("button");
      line.type = "button";
      line.className = "filename-line filename-play";
      line.style.setProperty("--file-index", fileIndex);
      line.title = file.mteam_id
        ? fileTooltip(file.path, file.mediainfo, file.mediainfo_checked)
        : `${file.path}\n\n无关联 M-Team ID，无法读取详情 MediaInfo`;
      line.textContent = file.name;
      line.setAttribute("aria-label", `用飞牛播放器播放 ${file.name}`);
      const fetchFileInfo = () => {
        if (file.mediainfo_checked || !file.mteam_id || !selectedSubscriptionId) return;
        if (videoRefreshStates.get(videoRefreshKey(subscriptionId, file.path))?.state === "pending"
            || activeRepairForVideo(repairJobs, subscriptionId, file.path)) return;
        line.title = fileTooltip(file.path, "", false);
        const request = loadVideoFileInfo(subscriptionId, item.code, file);
        request.then((result) => {
          if (videoFileInfoRequests.get(`${subscriptionId}:${file.path}`) !== request) return;
          file.mediainfo = result.mediainfo || "";
          file.mediainfo_checked = true;
          line.title = fileTooltip(file.path, file.mediainfo, true);
        }).catch((error) => {
          if (videoFileInfoRequests.get(`${subscriptionId}:${file.path}`) !== request) return;
          line.title = `${file.path}\n\nM-Team MediaInfo 读取失败：${error.message}`;
        });
      };
      line.addEventListener("mouseenter", fetchFileInfo);
      line.addEventListener("focus", fetchFileInfo);
      line.addEventListener("click", async () => {
        try {
          if (fnosApp.isStandaloneWeb) throw new Error("请从飞牛桌面应用中打开本应用。");
          await fnosApp.openFile(file.path);
        } catch (error) {
          toast(`无法打开飞牛播放器：${error.message}`, true);
        }
      });
      filesCell.append(line);
    }
    row.append(versionsCell, filesCell);
    body.append(row);
  }
}

async function loadExistingVideos(subscriptionId, { keepRows = false } = {}) {
  const request = ++existingVideosRequest;
  if (!keepRows || existingForId !== subscriptionId) {
    existingVideos = [];
    existingVideoCount = 0;
  }
  existingForId = subscriptionId;
  existingVideosLoading = true;
  existingVideosError = "";
  renderExistingVideos();
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos`);
    if (selectedSubscriptionId !== subscriptionId || request !== existingVideosRequest) return;
    existingVideos = result.videos || [];
    existingVideoCount = result.video_count || 0;
    const subscription = subscriptions.find((item) => item.id === subscriptionId);
    if (subscription) subscription.video_count = existingVideos.reduce((count, item) => count + (item.files || []).length, 0);
    for (const label of document.querySelectorAll(".subscription-nav-label")) {
      if (subscription && label.dataset.subscriptionId === subscriptionId) {
        label.textContent = `${subscription.performer}(${Number(subscription.video_count) || 0})`;
        label.title = `本地影片 ${Number(subscription.video_count) || 0} 个（按视频文件统计）`;
      }
    }
    existingVideosLoading = false;
    renderExistingVideos();
  } catch (error) {
    if (selectedSubscriptionId === subscriptionId && request === existingVideosRequest) {
      existingVideosLoading = false;
      existingVideosError = `读取目录失败：${error.message}`;
      renderExistingVideos();
    }
  }
}

function renderLogs(entries) {
  const list = $("#log-list");
  list.replaceChildren();
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  const scopedEntries = activeView === "subscription"
    ? entries.filter((entry) => selected && entry.subscription === selected.performer)
    : entries;
  const select = $("#log-category-filter");
  const category = select.value;
  const categories = new Set(scopedEntries.filter((entry) => entry.event !== "run_separator")
    .map((entry) => logCategory(entry.event)));
  if (category) categories.add(category);
  const options = [...new Set([...select.options].slice(1).map((option) => option.value).concat([...categories]))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
  if (options.join("\n") !== [...select.options].slice(1).map((option) => option.value).join("\n")) {
    select.replaceChildren(new Option("全部类型", ""), ...options.map((value) => new Option(value, value)));
    select.value = category;
  }
  const keyword = $("#log-keyword").value;
  const filtering = category || keyword.trim();
  const matchingEntries = filterLogEntries(scopedEntries, filtering ? (entry) => {
    const entryCategory = logCategory(entry.event);
    if (category && entryCategory !== category) return false;
    return !keyword.trim() || matchesKeywords([
      entry.subscription || logEventLabel(entry.event), entryCategory, entry.message, formatLogMessage(entry)
    ], keyword);
  } : null);
  const entriesToRender = compactLogStages(uniqueRoundSeparators(matchingEntries));
  $("#log-count").textContent = `${entriesToRender.filter((entry) => entry.event !== "run_separator").length} 条`;
  if (!entriesToRender.length) {
    const empty = document.createElement("li");
    empty.className = "log-empty";
    empty.textContent = filtering ? "没有符合筛选条件的日志。" : "等待检查任务。";
    list.append(empty);
    scheduleLogViewportFill();
    return;
  }
  const copyButtons = new Set();
  for (const entry of [...entriesToRender].reverse()) {
    const row = document.createElement("li");
    if (entry.event === "run_separator") {
      row.className = "log-entry separator";
      const label = document.createElement("span");
      label.textContent = entry.message;
      row.append(label);
      if (entry.round_id && !copyButtons.has(`${entry.round_id}:${entry.message.includes("开始") ? "start" : "end"}`)) {
        copyButtons.add(`${entry.round_id}:${entry.message.includes("开始") ? "start" : "end"}`);
        row.append(makeButton("复制本轮", "text-button log-copy-round", () => copyLogRound(entry.id, entry.round_id, scopedEntries), "复制本轮日志"));
      }
      list.append(row);
      continue;
    }
    row.className = `log-entry ${entry.level || "info"}`;
    const time = document.createElement("time");
    const date = new Date(entry.time);
    time.textContent = Number.isNaN(date.getTime()) ? entry.time : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    const source = document.createElement("span");
    source.className = "log-source";
    source.textContent = entry.subscription || logEventLabel(entry.event);
    const message = document.createElement("span");
    message.className = "log-message";
    const category = document.createElement("span");
    category.className = "log-category";
    category.textContent = logCategory(entry.event);
    let text;
    if (entry.event === "debug_http_response" && entry.message.includes("\n")) {
      const { heading, parameters, response } = splitHttpLog(entry.message);
      text = document.createElement("details");
      text.className = "log-response";
      text.open = expandedResponseLogs.has(entry.id);
      text.addEventListener("toggle", () => {
        if (text.open) expandedResponseLogs.add(entry.id);
        else expandedResponseLogs.delete(entry.id);
      });
      const summary = document.createElement("summary");
      summary.textContent = heading;
      text.append(summary);
      if (parameters) {
        const label = document.createElement("div");
        label.className = "log-request-label";
        label.textContent = "请求参数";
        const request = document.createElement("pre");
        request.textContent = parameters;
        text.append(label, request);
      }
      if (response) {
        const body = document.createElement("pre");
        body.textContent = response;
        const copy = makeButton("复制 JSON", "text-button log-copy-json",
          () => copyLogText(response, "接口响应 JSON 已复制"), "复制这条接口响应的 JSON");
        text.append(copy, body);
      }
    } else {
      text = document.createElement("span");
      text.textContent = formattedLogEntry(entry);
    }
    message.append(category, text);
    row.append(time, source, message);
    list.append(row);
  }
  scheduleLogViewportFill();
}

async function loadOlderLogs() {
  if (!hasOlderLogs || olderLogsLoading || !logEntries.length) return;
  const revision = logSearchRevision;
  olderLogsLoading = true;
  const status = $("#log-history-status");
  status.textContent = "正在加载更早日志…";
  const list = $("#log-list");
  try {
    const beforeId = Math.min(...logEntries.map((entry) => Number(entry.id) || Infinity));
    const params = logFilterParams();
    params.set("limit", String(LOG_PAGE_SIZE));
    params.set("before_id", String(beforeId));
    const older = await api(`/api/logs?${params}`);
    if (revision !== logSearchRevision) return;
    const known = new Set(logEntries.map((entry) => Number(entry.id)));
    logEntries = [...older.filter((entry) => !known.has(Number(entry.id))), ...logEntries];
    hasOlderLogs = older.length === LOG_PAGE_SIZE;
    renderLogs(logEntries);
    status.textContent = hasOlderLogs ? "继续向下滚动可加载更早日志" : "已加载全部日志";
  } catch (error) {
    if (revision === logSearchRevision) {
      status.textContent = "加载失败，滚动到底部可重试";
      toast(`加载更早日志失败：${error.message}`, true);
    }
  } finally {
    olderLogsLoading = false;
    if (revision === logSearchRevision && !hasOlderLogs && !status.textContent.includes("失败")) {
      status.textContent = "已加载全部日志";
    }
  }
}

function scheduleLogViewportFill() {
  if (logViewportFillScheduled) return;
  logViewportFillScheduled = true;
  requestAnimationFrame(() => {
    logViewportFillScheduled = false;
    const list = $("#log-list");
    if (list.clientHeight && list.scrollHeight <= list.clientHeight + 48 && hasOlderLogs) {
      void loadOlderLogs();
    }
  });
}

$("#log-list").addEventListener("scroll", () => {
  const list = $("#log-list");
  if (list.scrollTop + list.clientHeight >= list.scrollHeight - 96) {
    void loadOlderLogs();
  }
});

function logFilterParams() {
  const params = new URLSearchParams();
  const keyword = $("#log-keyword").value.trim();
  const category = $("#log-category-filter").value;
  if (keyword) params.set("keyword", keyword);
  if (category) params.set("category", category);
  if (activeView === "subscription") {
    const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
    if (selected) params.set("subscription", selected.performer);
  }
  return params;
}

async function reloadLogsForFilters() {
  const revision = ++logSearchRevision;
  const params = logFilterParams();
  params.set("limit", String(LOG_PAGE_SIZE));
  try {
    const entries = await api(`/api/logs?${params}`);
    if (revision !== logSearchRevision) return;
    logEntries = entries;
    hasOlderLogs = entries.length === LOG_PAGE_SIZE;
    logsLoaded = true;
    lastLogId = Math.max(lastLogId, ...entries.map((entry) => Number(entry.id) || 0));
    renderLogs(logEntries);
    $("#log-history-status").textContent = hasOlderLogs
      ? "向下滚动到底部自动加载更早日志"
      : "已加载全部符合条件的日志";
  } catch (error) {
    if (revision === logSearchRevision) toast(`搜索日志失败：${error.message}`, true);
  }
}

function scheduleLogsFilterReload(delay = 0) {
  window.clearTimeout(logFilterTimer);
  logFilterTimer = window.setTimeout(() => void reloadLogsForFilters(), delay);
}

const LOG_EVENT_LABELS = {
  downloading: "下载任务", started: "检查状态", scheduler_started: "后台服务",
  completed: "检查结果", failed: "检查失败", queued: "已排队",
  subscription_started: "开始检查", subscription_completed: "检查完成", subscription_failed: "检查失败",
  downloader_sync_started: "下载器", downloader_sync_completed: "下载器",
  downloader_auth_started: "下载器", downloader_auth_completed: "下载器",
  downloader_query_started: "下载器", downloader_query_completed: "下载器",
  local_scan_started: "本地扫描", local_scan_completed: "本地扫描",
  mteam_search_started: "M-Team 搜索", mteam_search_completed: "M-Team 搜索",
  retry_waiting: "限流等待", waiting_retry: "限流等待", retry_resuming: "继续检查", rate_limited: "限流",
  debug_decision: "检查依据", debug_performance: "性能统计", release: "资源处理", duplicate_task: "已有任务",
  debug_http_response: "接口响应", debug_artwork: "本地资料",
  subtitle_download_completed: "字幕", subtitle_download_failed: "字幕",
  subtitle_not_found: "字幕", subtitle_directory_unavailable: "字幕",
  subtitle_id_missing: "字幕", subtitle_metadata_failed: "字幕",
  subtitle_collection_completed: "字幕汇总", subtitle_collection_failed: "字幕汇总",
  local_video_info_refreshed: "本地视频", mteam_id_backfill: "M-Team ID",
  local_cover_sync: "封面", local_cover_cleanup_completed: "封面", local_cover_cleanup_failed: "封面",
  local_screenshot_sync: "截图", local_screenshot_cleanup_failed: "截图",
  local_nfo_sync: "NFO", local_artwork_sync_started: "本地资料",
  dmm_translation_failed: "翻译", dmm_actress_sync: "女优资料",
  mteam_id_backfill_started: "开始补全", mteam_id_backfill_progress: "补全进度",
  mteam_id_backfill_completed: "补全完成", mteam_id_backfill_failed: "补全失败",
  mteam_id_backfill_corrected: "ID 修正",
  local_wash_completed: "本地洗版", local_wash_failed: "本地洗版", local_wash_skipped: "本地洗版",
  orphan_sidecars_cleaned: "清理", existing_task_directory_recheck: "任务校验",
  existing_task_directory_missing: "任务校验", request_interrupted: "已中断",
  scheduler_error: "后台服务", scheduler_recovered: "后台服务", state_error: "状态错误",
  downloader_location_changed: "下载器", duplicate_task_recheck: "下载器", duplicate_task_paused_missing: "下载器",
  repair_queued: "修复排队", repair_started: "开始修复", repair_cooldown_check: "修复状态", repair_retry_waiting: "修复等待", repair_scan_started: "本地扫描", repair_scan_completed: "本地扫描", repair_matching_started: "ID 核对", repair_matching_completed: "ID 核对", repair_detail_started: "读取详情", repair_detail_completed: "保存详情", repair_subtitle_check: "字幕检查", repair_file_completed: "文件修复", repair_progress: "修复进度", repair_completed: "修复完成", repair_failed: "修复失败", repair_file_failed: "文件修复", repair_id_corrected: "ID 修正", repair_subtitle: "字幕修复", repair_downloader_failed: "ID 核对",
  run_separator: "检查轮次", status: "状态"
};

function logEventLabel(event) {
  return LOG_EVENT_LABELS[event] || "后台";
}

function logCategory(event) {
  if (event === "debug_http_response") return "接口响应";
  if (/^repair_archive_/.test(event)) return "归档";
  if (/^repair_/.test(event)) return "修复";
  if (event === "debug_artwork") return "本地资料";
  if (/^debug_/.test(event)) return "排查";
  if (/^subtitle_/.test(event)) return "字幕";
  if (/^local_cover_/.test(event)) return "封面";
  if (/^local_screenshot_/.test(event)) return "截图";
  if (event === "local_nfo_sync") return "NFO";
  if (event === "local_artwork_sync_started") return "本地资料";
  if (event === "dmm_actress_sync") return "女优资料";
  if (event === "dmm_translation_failed") return "翻译";
  if (/^local_(scan|video|wash)/.test(event) || event === "orphan_sidecars_cleaned") return "本地";
  if (/^mteam_/.test(event)) return "M-Team";
  if (/^downloader_/.test(event) || /^existing_task_/.test(event) || /^duplicate_task/.test(event)) return "下载器";
  if (/^retry_|^waiting_retry$|rate_limited/.test(event)) return "等待";
  if (/^scheduler_|state_error/.test(event)) return "系统";
  if (event === "release") return "资源";
  if (["started", "completed", "failed", "queued", "subscription_started", "subscription_completed", "subscription_failed", "request_interrupted"].includes(event)) return "检查";
  return "状态";
}

function formatLogMessage(entry) {
  let message = String(entry.message || "");
  const exact = {
    "后台调度器已启动。": "后台服务已启动。",
    "开始检查订阅。": "开始本轮检查。",
    "开始搜索。": "开始检查此订阅。",
    "订阅检查完成。": "此订阅检查完成。",
    "正在请求 M-Team 搜索结果。": "正在搜索 M-Team。",
    "下载器任务状态同步完成。": "下载器任务状态已同步。",
    "qBittorrent 登录成功，正在读取任务状态。": "已连接 qBittorrent。",
    "正在登录 qBittorrent Web API。": "正在连接 qBittorrent。",
    "检查任务已加入队列。": "检查已加入队列，等待执行。"
  };
  if (exact[message]) return exact[message];
  if (entry.event === "retry_waiting" || entry.event === "waiting_retry") {
    message = message.replace(/(\d+) 秒后/g, (_match, seconds) => `${formatLogDuration(Number(seconds))} 后`);
  }
  if (entry.event === "local_scan_completed") return message.replace("本地扫描完成，识别到 ", "扫描完成：").replace(" 个视频、", " 个视频，").replace(" 个番号。", " 个番号。");
  if (entry.event === "mteam_search_completed") return message.replace("M-Team 搜索返回 ", "找到 ").replace(" 条资源。", " 条资源。");
  if (entry.event === "downloader_query_completed") return message.replace("qBittorrent 返回 ", "读取到 ").replace(" 个任务。", " 个下载任务。");
  if (entry.event === "downloader_sync_started") return message.replace("正在同步下载器任务状态", "正在同步下载任务");
  if (entry.event === "release") {
    return message.replace(/\b(upgrade_available|copy_failed|cleanup_failed|missing|completed|failed|downloading|submitted|superseded)\b/, (state) => ({
      upgrade_available: "可升级", copy_failed: "复制失败", cleanup_failed: "清理失败", missing: "任务丢失",
      completed: "已完成", failed: "失败", downloading: "下载中", submitted: "已提交", superseded: "已替换"
    })[state]);
  }
  return message;
}

function formatLogDuration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes ? `${minutes} 分` : ""}${remainder ? `${remainder} 秒` : ""}` || "0 秒";
}

function formattedLogEntry(entry) {
  const message = formatLogMessage(entry);
  return entry.duplicate_count ? `${message}（连续重复 ${entry.duplicate_count} 次，已合并）` : message;
}

function compactLogStages(entries) {
  const finished = new Set();
  const pairKeys = {
    downloader_auth_started: "downloader", downloader_auth_completed: "downloader",
    downloader_query_started: "downloader", downloader_query_completed: "downloader",
    downloader_sync_started: "sync", downloader_sync_completed: "sync",
    local_scan_started: "scan", local_scan_completed: "scan",
    mteam_search_started: "search", mteam_search_completed: "search"
  };
  const terminalEvent = { downloader: "downloader_query_completed", sync: "downloader_sync_completed", scan: "local_scan_completed", search: "mteam_search_completed" };
  const scope = (entry, pair) => entry.round_id && pair ? `${entry.round_id}:${entry.subscription || ""}:${pair}` : "";
  for (const entry of entries) {
    const pair = pairKeys[entry.event];
    if (pair && entry.event.endsWith("_completed")) finished.add(scope(entry, pair));
    if (entry.event === "local_scan_completed") finished.add(scope(entry, "scan"));
    if (entry.event === "mteam_search_completed") finished.add(scope(entry, "search"));
    if (entry.event === "downloader_query_completed") finished.add(scope(entry, "downloader"));
    if (entry.event === "downloader_sync_completed") finished.add(scope(entry, "sync"));
  }
  const compacted = entries.filter((entry) => {
    const pair = pairKeys[entry.event];
    if (!pair || !finished.has(scope(entry, pair))) return true;
    return entry.event === terminalEvent[pair];
  });
  const output = [];
  for (const entry of compacted) {
    const previous = output[output.length - 1];
    const duplicate = previous && entry.round_id && entry.event !== "run_separator" &&
      previous.event === entry.event && previous.round_id === entry.round_id &&
      previous.subscription === entry.subscription && previous.message === entry.message &&
      Math.abs(Date.parse(entry.time) - Date.parse(previous.time)) <= 3000;
    if (duplicate) {
      previous.duplicate_count = (previous.duplicate_count || 1) + 1;
    } else {
      output.push({ ...entry });
    }
  }
  return output;
}

function uniqueRoundSeparators(entries) {
  if (activeView === "subscription") return entries;
  const seen = new Set();
  return entries.filter((entry) => {
    if (entry.event !== "run_separator" || !entry.round_id) return true;
    const phase = entry.message.includes("开始") ? "start" : "end";
    const key = `${entry.round_id}:${phase}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function logLine(entry) {
  const date = new Date(entry.time);
  const time = Number.isNaN(date.getTime()) ? entry.time : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const source = entry.subscription || logEventLabel(entry.event);
  return `${time} ${source} · ${logCategory(entry.event)} · ${formattedLogEntry(entry)}`;
}

async function copyLogRound(separatorId, roundId, visibleEntries) {
  const selectedRound = selectLogRound(visibleEntries, separatorId, roundId);
  const roundEntries = compactLogStages(uniqueRoundSeparators(selectedRound));
  await copyLogText(roundEntries.map(logLine).join("\n"), "本轮日志已复制");
}

async function copyLogText(text, successMessage) {
  try {
    await writeClipboardText(text);
    toast(successMessage);
  } catch (error) {
    toast(`复制日志失败：${error.message}`, true);
  }
}

async function writeClipboardText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fnOS may restrict the clipboard API inside the app iframe.
    }
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.readOnly = true;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  try {
    input.select();
    input.setSelectionRange(0, input.value.length);
    if (!document.execCommand("copy")) throw new Error("浏览器未允许访问剪贴板");
  } finally {
    input.remove();
  }
}

async function clearLogs() {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  const scoped = activeView === "subscription" && selected;
  const scopeLabel = scoped ? `“${selected.performer}”的日志` : "全部日志";
  if (!window.confirm(`确定清理${scopeLabel}吗？此操作无法撤销。`)) return;
  const query = scoped ? `?subscription=${encodeURIComponent(selected.performer)}` : "";
  const button = $("#clear-logs");
  button.disabled = true;
  try {
    const result = await api(`/api/logs${query}`, { method: "DELETE" });
    logEntries = scoped
      ? logEntries.filter((entry) => entry.subscription !== selected.performer)
      : [];
    hasOlderLogs = false;
    logsLoaded = true;
    renderLogs(logEntries);
    $("#log-history-status").textContent = "已清理日志";
    toast(`已清理 ${result.deleted} 条${scoped ? `${selected.performer}的` : ""}日志`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderActivity() {
  const body = $("#activity-body");
  body.replaceChildren();
  const visibleActivity = activity;
  $("#activity-count").textContent = `${visibleActivity.length} 项`;
  if (!visibleActivity.length) {
    const row = document.createElement("tr");
    row.className = "activity-empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty";
    cell.textContent = "暂无下载任务";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of visibleActivity) {
    const row = document.createElement("tr");
    row.className = "activity-row";
    const values = [item.subscription, item.code, item.title];
    for (let index = 0; index < values.length; index += 1) {
      const value = values[index];
      const cell = document.createElement("td");
      if (index === 1) renderNumber(cell, item.code, item.mteam_id);
      else cell.textContent = value || "";
      row.append(cell);
    }
    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge${item.state === "completed" ? " completed" : ["failed", "missing", "copy_failed", "cleanup_failed"].includes(item.state) ? " failed" : item.pending ? " upgrading" : ""}`;
    badge.textContent = item.pending && item.state === "downloading" ? "洗版下载中" : stateLabel(item.state);
    status.append(badge);
    row.append(status);
    const progress = document.createElement("td");
    const label = document.createElement("div");
    label.className = "progress-label";
    label.textContent = item.state === "completed" ? "完成" : `${Math.round(item.progress || 0)}%`;
    const bar = document.createElement("progress");
    bar.max = 100;
    bar.value = item.progress || 0;
    progress.append(label, bar);
    row.append(progress);
    const message = document.createElement("td");
    message.textContent = item.message || "";
    row.append(message);
    body.append(row);
  }
}

function renderStatus(status) {
  currentStatus = status;
  const state = $("#scheduler-state");
  const nextScan = $("#next-scan-time");
  const waitingRetry = status.state === "waiting_retry";
  const running = status.state === "running" || status.state === "queued" || waitingRetry;
  state.dataset.state = running ? "running" : status.state || "idle";
  if (waitingRetry) {
    const seconds = Math.max(0, Math.ceil(Number(status.retry_at || 0) - Date.now() / 1000));
    state.dataset.state = "waiting_retry";
    state.lastElementChild.textContent = status.stop_requested ? "正在停止检查" : `限流重试 ${formatCountdown(seconds)}`;
  } else {
    state.lastElementChild.textContent = status.stop_requested ? "正在停止检查" : running ? "正在检查" : status.state === "stopped" ? "检查已停止" : status.state === "failed" ? "检查失败" : "后台待命";
  }
  if (running) {
    nextScan.textContent = Number(status.queued_count) > 0 ? `另有 ${status.queued_count} 项检查排队` : "";
  } else if (Number(status.queued_count) > 0) {
    nextScan.textContent = `${status.queued_count} 项检查排队`;
  } else if (!status.auto_scan) {
    nextScan.textContent = "定时检查未启用";
  } else if (Number(status.next_scan_at) > 0) {
    const date = new Date(Number(status.next_scan_at) * 1000);
    nextScan.textContent = Number.isNaN(date.getTime()) ? "" : `下次检查 ${date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`;
  } else {
    nextScan.textContent = "等待首次检查";
  }
}

function formatCountdown(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours) return `${hours}小时${minutes ? `${minutes}分` : ""}${remainder}秒`;
  if (minutes) return `${minutes}分${remainder}秒`;
  return `${remainder}秒`;
}

function renderScanButtons() {
  const sidebar = $("#scan-now");
  const selected = $("#selected-scan");
  const selectedHelp = selected.dataset.help || selected.title;
  selected.dataset.help = selectedHelp;
  const retrying = currentStatus.state === "waiting_retry";
  const canStop = ["running", "waiting_retry"].includes(currentStatus.state) && Boolean(currentStatus.run_id);
  const stopRequested = Boolean(currentStatus.stop_requested);
  const retryLabel = retrying ? `M-Team 冷却中，${formatCountdown(Math.max(0, Math.ceil(Number(currentStatus.retry_at || 0) - Date.now() / 1000)))} 后自动重试` : "";
  sidebar.disabled = busy && (!canStop || stopRequested);
  sidebar.textContent = canStop && !stopRequested ? "■" : busy ? "…" : "↻";
  sidebar.title = stopRequested ? "停止请求已提交，当前操作完成后将停止" : canStop ? "停止本轮检查" : busy ? "检查任务排队中" : "立即检查";
  sidebar.setAttribute("aria-label", sidebar.title);
  sidebar.setAttribute("aria-busy", String(busy && !canStop));
  sidebar.classList.toggle("is-stop-action", canStop && !stopRequested);
  selected.disabled = busy;
  selected.textContent = retrying ? "等待限流结束" : busy ? "检查中…" : "立即检查";
  selected.title = [retryLabel || (busy ? "检查中，请在下方日志查看进度。" : ""), selectedHelp].filter(Boolean).join("\n\n");
  selected.setAttribute("aria-label", retrying ? "等待限流结束" : busy ? "检查中" : "立即检查");
  selected.setAttribute("aria-busy", String(busy));
  for (const button of document.querySelectorAll(".quick-check")) {
    button.disabled = busy;
    button.textContent = busy ? "…" : "↻";
    button.title = retryLabel || (busy ? "检查任务运行中" : button.dataset.defaultTitle);
    button.setAttribute("aria-label", button.title);
  }
  const mobileCheck = $("#mobile-subscription-check");
  mobileCheck.disabled = busy || !selectedSubscriptionId;
  mobileCheck.title = retryLabel || (busy ? "检查任务运行中" : selectedSubscriptionId ? "立即检查所选订阅" : "请先选择订阅");
  mobileCheck.setAttribute("aria-label", mobileCheck.title);
  $("#mobile-subscription-edit").disabled = !subscriptions.some((item) => item.id === selectedSubscriptionId);
}

async function refreshSubscriptions() {
  const now = Date.now();
  if (subscriptionRefreshInFlight || now - lastSubscriptionRefresh < 30000) return;
  const revision = subscriptionRevision;
  lastSubscriptionRefresh = now;
  subscriptionRefreshInFlight = true;
  try {
    const next = await api("/api/subscriptions");
    if (revision !== subscriptionRevision) {
      lastSubscriptionRefresh = 0;
      return;
    }
    subscriptions = next;
    if (activeView === "subscription" && !subscriptions.some((item) => item.id === selectedSubscriptionId)) {
      activeView = "all";
      selectedSubscriptionId = "";
    }
    renderSubscriptions();
  } catch (error) {
    if (revision === subscriptionRevision) toast(`读取订阅失败：${error.message}`, true);
  } finally {
    subscriptionRefreshInFlight = false;
  }
}

async function refresh() {
  void refreshSubscriptions();
  void refreshDownloadActivity();
  if (activeView === "actresses" && !actressEditorIsActive() && Date.now() - lastActressesRefresh >= 60000) {
    void loadActresses();
  }
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const initialLogParams = logFilterParams();
    initialLogParams.set("limit", String(LOG_PAGE_SIZE));
    const logParams = logsLoaded ? logFilterParams() : initialLogParams;
    if (logsLoaded) {
      logParams.set("after_id", String(lastLogId));
      logParams.set("limit", String(LOG_PAGE_SIZE));
    }
    const logsPath = `/api/logs?${logParams}`;
    const [status, nextLogs, repairs] = await Promise.all([
      api("/api/status"),
      api(logsPath),
      api("/api/repair-jobs")
    ]);
    if (!logsLoaded || nextLogs.length) {
      if (!logsLoaded) hasOlderLogs = nextLogs.length === LOG_PAGE_SIZE;
      const known = new Set(logEntries.map((entry) => Number(entry.id)));
      logEntries = [...logEntries, ...nextLogs.filter((entry) => !known.has(Number(entry.id)))];
      logsLoaded = true;
      lastLogId = logEntries.reduce((maximum, entry) => Math.max(maximum, Number(entry.id) || 0), lastLogId);
      renderLogs(logEntries);
      if (hasOlderLogs) $("#log-history-status").textContent = "向下滚动到底部自动加载更早日志";
      else if (!logEntries.length) $("#log-history-status").textContent = "已加载全部符合条件的日志";
    }
    renderStatus(status);
    renderDataRepairStatus(repairs);
    if (selectedSubscriptionId && ["completed", "failed", "stopped"].includes(status.state) && ["running", "queued", "waiting_retry"].includes(lastStatusState)) {
      loadExistingVideos(selectedSubscriptionId);
    }
    lastStatusState = status.state;
    busy = ["running", "queued", "waiting_retry"].includes(status.state) || Number(status.queued_count) > 0;
    renderScanButtons();
  } catch (error) {
    $("#scheduler-state").dataset.state = "failed";
    $("#scheduler-state").lastElementChild.textContent = "连接异常";
  } finally {
    refreshInFlight = false;
  }
}

async function refreshDownloadActivity() {
  if (activeView !== "all" || activityRefreshInFlight || Date.now() - lastActivityRefresh < 15000) return;
  activityRefreshInFlight = true;
  lastActivityRefresh = Date.now();
  try {
    activity = await api("/api/activity");
    if (activeView === "all") renderActivity();
  } catch {
    // Keep the last list while the activity endpoint is unavailable.
  } finally {
    activityRefreshInFlight = false;
  }
}

function renderDataRepairStatus(jobs) {
  const previouslyActive = new Set(repairJobs.filter((job) => ["queued", "running"].includes(job.state)).map((job) => job.job_id));
  repairJobs = jobs || [];
  const button = $("#selected-repair-data");
  const allActive = repairJobs.filter((job) => ["queued", "running"].includes(job.state));
  const active = repairJobs.filter((job) => job.subscription_id === selectedSubscriptionId && ["queued", "running"].includes(job.state));
  const repairCooldown = repairJobs.find((job) => ["queued", "running"].includes(job.state) && job.phase === "waiting_retry");
  renderRetryCountdown(repairCooldown);
  if (allActive.length && !["running", "waiting_retry"].includes(currentStatus.state)) {
    const state = $("#scheduler-state");
    state.dataset.state = repairCooldown ? "waiting_retry" : "running";
    state.lastElementChild.textContent = repairCooldown ? "修复等待限流恢复" : "正在修复数据";
    $("#next-scan-time").textContent = "";
  } else {
    renderStatus(currentStatus);
  }
  button.disabled = active.length > 0;
  const current = active[0];
  if (current) {
    const progress = current.phase === "waiting_retry"
      ? ` ${formatCountdown(Math.max(0, Math.ceil(Number(current.retry_at || 0) - Date.now() / 1000)))}`
      : current.total > 0 ? ` ${current.completed}/${current.total}` : "";
    button.textContent = `${repairPhaseLabel(current)}${progress}…`;
    button.title = current.message;
  } else {
    button.textContent = "修复数据";
    button.title = "将当前订阅已有影片归档到各自番号目录，核对 M-Team ID、刷新影片信息和 MediaInfo、补齐缺少的中文字幕，并在视频旁保存封面、海报和截图。";
  }
  button.setAttribute("aria-live", "polite");
  let reloadSelected = false;
  for (const job of repairJobs) {
    if (!["completed", "failed"].includes(job.state)) continue;
    const awaited = awaitedRepairJobs.has(job.job_id);
    if (!awaited && !previouslyActive.has(job.job_id)) continue;
    awaitedRepairJobs.delete(job.job_id);
    videoFileInfoRequests.clear();
    videoSubtitleRequests.clear();
    if (job.path) {
      const outcome = job.outcomes?.[job.archived_path || job.path];
      const imageFailed = Number(job.summary?.image_failed) > 0;
      const failed = job.state === "failed" || imageFailed || ["failed", "partial", "skipped"].includes(outcome?.state);
      const state = { state: failed ? "error" : "success",
        message: imageFailed ? job.message : (outcome?.message || job.message), completedAt: Date.now() };
      videoRefreshStates.set(videoRefreshKey(job.subscription_id, job.archived_path || job.path), state);
      if (job.archived_path) videoRefreshStates.delete(videoRefreshKey(job.subscription_id, job.path));
    }
    if (awaited) toast(job.message, job.state === "failed" || Number(job.summary?.failed) > 0
      || Number(job.summary?.image_failed) > 0 || Number(job.summary?.archive_failed) > 0);
    if (selectedSubscriptionId === job.subscription_id) reloadSelected = true;
  }
  if (active.length || previouslyActive.size) {
    for (const refresh of document.querySelectorAll(".video-refresh")) {
      updateVideoRefreshButton(refresh, currentVideoRepairStatus(refresh));
    }
  }
  if (reloadSelected) loadExistingVideos(selectedSubscriptionId, { keepRows: true });
}

function renderRetryCountdown(repairCooldown = null) {
  const countdown = $("#repair-retry-countdown");
  if (!countdown) return;
  if (repairCooldown) {
    const remaining = Math.max(0, Math.ceil(Number(repairCooldown.retry_at || 0) - Date.now() / 1000));
    countdown.textContent = `修复限流重试 ${formatCountdown(remaining)} 后继续`;
    countdown.hidden = false;
  } else if (currentStatus.state === "waiting_retry") {
    const remaining = Math.max(0, Math.ceil(Number(currentStatus.retry_at || 0) - Date.now() / 1000));
    countdown.textContent = `限流恢复倒计时 ${formatCountdown(remaining)}`;
    countdown.hidden = false;
  } else {
    countdown.textContent = "";
    countdown.hidden = true;
  }
}

function openSubscription(item = null, initialPerformer = "") {
  form.reset();
  $("#subscription-save-error").hidden = true;
  $("#subscription-id").value = item?.id || "";
  $("#dialog-title").textContent = item ? "编辑订阅" : "新建订阅";
  $("#performer").value = item?.performer || initialPerformer;
  searchKeywords = Array.isArray(item?.search_keywords)
    ? item.search_keywords.map((value) => String(value || "").trim()).filter(Boolean)
    : item?.search_keyword ? [String(item.search_keyword).trim()] : [];
  const legacyKeyword = String(item?.keywords || "").trim();
  if (item && !searchKeywords.length) searchKeywords = [[item.performer, legacyKeyword || "ltx"].filter(Boolean).join(" ")];
  $("#search-keyword").value = item ? "" : (initialPerformer ? `${initialPerformer} ltx` : "");
  $("#search-keyword").dataset.autoGenerated = String(!item);
  renderSearchKeywordTags();
  $("#download-dir").value = item?.download_dir || "";
  $("#exclude-non-chinese").checked = item?.exclude_non_chinese !== false;
  $("#wash-enabled").checked = item?.wash_enabled !== false;
  blacklistNumbers = [...(item?.blacklist || [])];
  renderBlacklistTags();
  dialog.showModal();
  $("#performer").focus();
}

async function saveSubscription(event) {
  event.preventDefault();
  const id = $("#subscription-id").value;
  const performer = $("#performer").value.trim();
  const pendingSearchKeywords = $("#search-keyword").value.trim();
  const keywordsToSave = [...searchKeywords];
  if (pendingSearchKeywords) {
    for (const keyword of pendingSearchKeywords.split(/[，,]/).map((value) => value.trim()).filter(Boolean)) {
      if (!keywordsToSave.includes(keyword)) keywordsToSave.push(keyword);
    }
  }
  if (!keywordsToSave.length) keywordsToSave.push(`${performer} ltx`);
  const payload = {
    performer, search_keywords: keywordsToSave,
    download_dir: $("#download-dir").value.trim(), wash_enabled: $("#wash-enabled").checked,
    exclude_non_chinese: $("#exclude-non-chinese").checked,
    blacklist: [...blacklistNumbers]
  };
  const button = $("#submit-subscription");
  if (button.disabled) return;
  const message = $("#subscription-save-error");
  message.hidden = true;
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const saved = await api(id ? `/api/subscriptions/${encodeURIComponent(id)}` : "/api/subscriptions", {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    subscriptionRevision++;
    const index = subscriptions.findIndex((item) => item.id === saved.id);
    const previous = subscriptions[index];
    if (index === -1) {
      subscriptions.push(saved);
    } else {
      subscriptions[index] = { ...saved, video_count: previous.download_dir === saved.download_dir ? previous.video_count : 0 };
    }
    if (id && id === selectedSubscriptionId && previous?.download_dir !== saved.download_dir) existingForId = "";
    dialog.close();
    renderSubscriptions();
    if (activeView === "actresses") renderActresses();
    toast(id ? "订阅已更新" : "订阅已创建");
  } catch (error) {
    if (dialog.open) {
      message.textContent = error.message;
      message.hidden = false;
    } else {
      toast(error.message, true);
    }
  } finally {
    button.disabled = false;
    button.textContent = "保存订阅";
  }
}

async function removeSubscription(item) {
  if (!window.confirm(`删除“${item.performer}”订阅？已加入下载器的任务不会被删除。`)) return;
  try {
    await api(`/api/subscriptions/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    subscriptionRevision++;
    subscriptions = subscriptions.filter((entry) => entry.id !== item.id);
    if (selectedSubscriptionId === item.id) {
      selectedSubscriptionId = "";
      activeView = "all";
    }
    renderSubscriptions();
    toast("订阅已删除");
  } catch (error) { toast(error.message, true); }
}

async function runScan(subscriptionId = "") {
  if (busy) {
    toast("已有检查任务正在运行");
    return;
  }
  busy = true;
  renderScanButtons();
  toast("正在提交检查请求…");
  try {
    await api(subscriptionId ? `/api/subscriptions/${encodeURIComponent(subscriptionId)}/scan` : "/api/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    toast("检查任务已加入队列");
    await refresh();
  } catch (error) {
    toast(error.message, true);
    busy = false;
    renderScanButtons();
  }
}

async function stopScan() {
  const runId = currentStatus.run_id;
  if (!["running", "waiting_retry"].includes(currentStatus.state) || !runId || currentStatus.stop_requested) return;
  try {
    await api("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: runId }) });
    if (currentStatus.run_id === runId && ["running", "waiting_retry"].includes(currentStatus.state)) {
      renderStatus({ ...currentStatus, stop_requested: true });
      renderScanButtons();
    }
    toast("已提交停止请求，当前操作完成后将停止");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderAzureSettings(settings) {
  const configured = Boolean(settings.azure_key_configured);
  const input = $("#azure-key");
  input.value = "";
  input.placeholder = configured ? "已保存，留空保留" : "粘贴 Azure 密钥";
  $("#azure-region").value = settings.azure_region || "";
  $("#azure-key-state").textContent = configured ? "密钥已保存" : "未配置";
  $("#azure-key-state").dataset.state = configured ? "ok" : "";
  $("#clear-azure-key").disabled = !configured;
}

async function loadSettings() {
  const settings = await api("/api/settings");
  const client = settings.downloader;
  $("#mteam-state").textContent = settings.mteam_token_configured ? "令牌已保存" : "未配置";
  $("#mteam-state").dataset.state = settings.mteam_token_configured ? "ok" : "";
  $("#client-type").value = client.type;
  $("#client-url").value = client.url;
  $("#client-user").value = client.username;
  $("#client-password").value = "";
  pathMappings = (client.path_mappings || []).map((mapping) => ({ local: mapping.local, remote: mapping.remote }));
  $("#local-multi-disk-wash").checked = Boolean(settings.local_multi_disk_wash);
  $("#cleanup-orphan-sidecars").checked = Boolean(settings.cleanup_orphan_sidecars);
  $("#subtitle-collection-dir").value = settings.subtitle_collection_dir || "";
  renderAzureSettings(settings);
  $("#debug-mode").checked = Boolean(settings.debug_mode);
  $("#log-retention-days").value = settings.log_retention_days || 15;
  renderPathMappings();
  $("#downloader-state").textContent = client.url ? "已配置" : "未配置";
  $("#auto-scan").checked = settings.auto_scan;
  $("#poll-interval").value = settings.poll_interval_minutes;
  $("#mteam-request-interval").value = settings.mteam_request_interval_seconds;
  renderPage();
}

function mapDownloaderPath(path) {
  if (!path || !path.startsWith("/")) return path || "";
  const normalize = (value) => value.length > 1 ? value.replace(/\/+$/, "") : value;
  const localPath = normalize(path);
  const matches = pathMappings
    .filter((mapping) => mapping.local?.startsWith("/") && mapping.remote?.startsWith("/"))
    .map((mapping) => ({ local: normalize(mapping.local), remote: normalize(mapping.remote) }))
    .filter((mapping) => localPath === mapping.local || localPath.startsWith(`${mapping.local}/`))
    .sort((a, b) => b.local.length - a.local.length);
  if (!matches.length) return path;
  const mapping = matches[0];
  const suffix = localPath.slice(mapping.local.length).replace(/^\/+/, "");
  return suffix ? `${mapping.remote}/${suffix}` : mapping.remote;
}

function renderPathMappings() {
  const list = $("#path-mapping-list");
  list.replaceChildren();
  pathMappings.forEach((mapping, index) => {
    const row = document.createElement("div");
    row.className = "path-mapping-row";
    const localLabel = document.createElement("label");
    localLabel.textContent = "飞牛本地路径";
    const localInput = document.createElement("input");
    localInput.type = "text";
    localInput.placeholder = "/vol2/1000/media";
    localInput.value = mapping.local;
    localInput.setAttribute("aria-label", `第 ${index + 1} 条映射的飞牛本地路径`);
    localInput.addEventListener("input", () => { pathMappings[index].local = localInput.value; });
    localLabel.append(localInput);
    const arrow = document.createElement("span");
    arrow.className = "path-mapping-arrow";
    arrow.textContent = "→";
    arrow.setAttribute("aria-hidden", "true");
    const remoteLabel = document.createElement("label");
    remoteLabel.textContent = "下载器路径";
    const remoteInput = document.createElement("input");
    remoteInput.type = "text";
    remoteInput.placeholder = "/media";
    remoteInput.value = mapping.remote;
    remoteInput.setAttribute("aria-label", `第 ${index + 1} 条映射的下载器路径`);
    remoteInput.addEventListener("input", () => { pathMappings[index].remote = remoteInput.value; });
    remoteLabel.append(remoteInput);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-button path-mapping-remove";
    remove.textContent = "×";
    remove.title = "删除路径映射";
    remove.setAttribute("aria-label", `删除第 ${index + 1} 条路径映射`);
    remove.addEventListener("click", () => {
      pathMappings.splice(index, 1);
      renderPathMappings();
    });
    row.append(localLabel, arrow, remoteLabel, remove);
    list.append(row);
  });
}

async function saveSettings({ clearToken = false, clearAzureKey = false } = {}) {
  const buttons = [$("#save-settings"), $("#save-settings-footer")].filter(Boolean);
  for (const button of buttons) button.disabled = true;
  const payload = {
    downloader: {
      type: $("#client-type").value, url: $("#client-url").value.trim(),
      username: $("#client-user").value.trim(), password: $("#client-password").value,
      path_mappings: pathMappings
    },
    poll_interval_minutes: Number($("#poll-interval").value),
    mteam_request_interval_seconds: Number($("#mteam-request-interval").value), auto_scan: $("#auto-scan").checked,
    local_multi_disk_wash: $("#local-multi-disk-wash").checked,
    cleanup_orphan_sidecars: $("#cleanup-orphan-sidecars").checked,
    subtitle_collection_dir: $("#subtitle-collection-dir").value.trim(),
    azure_region: $("#azure-region").value.trim(),
    clear_azure_key: clearAzureKey,
    debug_mode: $("#debug-mode").checked,
    log_retention_days: Number($("#log-retention-days").value),
    clear_mteam_token: clearToken
  };
  if ($("#mteam-token").value) payload.mteam_token = $("#mteam-token").value;
  if ($("#azure-key").value) payload.azure_key = $("#azure-key").value;
  try {
    const settings = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    $("#mteam-token").value = "";
    $("#mteam-state").textContent = settings.mteam_token_configured ? "令牌已保存" : "未配置";
    $("#mteam-state").dataset.state = settings.mteam_token_configured ? "ok" : "";
    $("#downloader-state").textContent = settings.downloader.url ? "已配置" : "未配置";
    $("#client-password").value = "";
    pathMappings = (settings.downloader.path_mappings || []).map((mapping) => ({ local: mapping.local, remote: mapping.remote }));
    $("#local-multi-disk-wash").checked = Boolean(settings.local_multi_disk_wash);
    $("#cleanup-orphan-sidecars").checked = Boolean(settings.cleanup_orphan_sidecars);
    $("#subtitle-collection-dir").value = settings.subtitle_collection_dir || "";
    renderAzureSettings(settings);
    $("#debug-mode").checked = Boolean(settings.debug_mode);
    $("#mteam-request-interval").value = settings.mteam_request_interval_seconds;
    renderPathMappings();
    renderPage();
    toast(clearToken ? "令牌已清除" : clearAzureKey ? "Azure 密钥已清除" : "设置已保存");
  } catch (error) { toast(error.message, true); }
  finally { for (const button of buttons) button.disabled = false; }
}

async function testConnection(endpoint, target) {
  const node = $(target);
  node.textContent = "正在测试";
  node.dataset.state = "";
  try {
    if (endpoint === "/api/mteam/test" && $("#mteam-token").value) await saveSettings();
    if (endpoint === "/api/downloader/test") await saveSettings();
    const result = await api(endpoint, { method: "POST" });
    node.textContent = result.message;
    node.dataset.state = "ok";
  } catch (error) {
    node.textContent = error.message;
    node.dataset.state = "error";
  }
}

form.addEventListener("submit", saveSubscription);
$("#performer").addEventListener("input", (event) => {
  const searchKeyword = $("#search-keyword");
  if (searchKeyword.dataset.autoGenerated === "true" && searchKeywords.length === 0) {
    searchKeyword.value = `${event.currentTarget.value.trim()} ltx`.trim();
  }
});
$("#search-keyword").addEventListener("input", (event) => {
  event.currentTarget.dataset.autoGenerated = "false";
});
$("#search-keyword").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === "," || event.key === "，") {
    event.preventDefault();
    if (addSearchKeywords(event.currentTarget.value)) event.currentTarget.dataset.autoGenerated = "false";
  } else if (event.key === "Backspace" && !event.currentTarget.value && searchKeywords.length) {
    searchKeywords.pop();
    renderSearchKeywordTags();
  }
});
$("#blacklist-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === ",") {
    event.preventDefault();
    addBlacklistNumber();
  } else if (event.key === "Backspace" && !event.currentTarget.value && blacklistNumbers.length) {
    blacklistNumbers.pop();
    renderBlacklistTags();
  }
});
$("#add-subscription").addEventListener("click", () => openSubscription());
$("#mobile-add-subscription").addEventListener("click", () => openSubscription());
$("#mobile-subscription-picker").addEventListener("change", (event) => {
  if (event.currentTarget.value) showSubscription(event.currentTarget.value);
  else {
    selectedSubscriptionId = "";
    activeView = "all";
    renderSubscriptions();
  }
});
$("#mobile-subscription-edit").addEventListener("click", () => {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  if (selected) openSubscription(selected);
});
$("#mobile-subscription-check").addEventListener("click", () => {
  if (selectedSubscriptionId) runScan(selectedSubscriptionId);
});
for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => {
    activeView = button.dataset.view;
    if (activeView === "all") selectedSubscriptionId = "";
    renderSubscriptions();
    if (activeView === "actresses") void loadActresses();
  });
}
$("#add-actress").addEventListener("click", (event) => {
  const form = $("#actress-add-form");
  form.hidden = !form.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!form.hidden));
  if (!form.hidden) $("#actress-source-name").focus();
});
$("#actress-add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  if (submit.disabled) return;
  const errorMessage = $("#actress-add-error");
  errorMessage.hidden = true;
  submit.disabled = true;
  try {
    const result = await api("/api/actresses/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_name: $("#actress-source-name").value,
        chinese_name: $("#actress-chinese-name").value,
      }),
    });
    form.reset();
    form.hidden = true;
    $("#add-actress").setAttribute("aria-expanded", "false");
    if (actressesLoading) await actressesLoading;
    await loadActresses();
    toast(`已添加 ${result.source_name}。`);
  } catch (error) {
    errorMessage.textContent = error.message;
    errorMessage.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
$("#actress-merge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  await mergeActress($("#actress-merge-source").value, $("#actress-merge-target").value, button, $("#actress-merge-state"));
});
$("#selected-scan").addEventListener("click", () => runScan(selectedSubscriptionId));
$("#selected-repair-data").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!selectedSubscriptionId || button.disabled) return;
  button.disabled = true;
  button.textContent = "正在排队…";
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(selectedSubscriptionId)}/repair`, { method: "POST" });
    awaitedRepairJobs.add(result.job_id);
    repairJobs = [...repairJobs.filter((job) => job.job_id !== result.job_id), result];
    renderDataRepairStatus(repairJobs);
    toast(result.message || "数据修复已排队。");
  } catch (error) {
    renderDataRepairStatus(repairJobs);
    toast(error.message, true);
  }
});
$("#existing-toggle").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const expanded = button.getAttribute("aria-expanded") === "true";
  button.setAttribute("aria-expanded", String(!expanded));
  $("#existing-content").hidden = expanded;
});
for (const [inputId, clearId, render, resultsSelector] of [
  ["existing-keyword", "clear-existing-keyword", renderExistingVideos, ".existing-section .table-wrap"],
]) {
  const input = $(`#${inputId}`);
  const clear = $(`#${clearId}`);
  const update = () => {
    clear.hidden = !input.value;
    render();
    $(resultsSelector).scrollTop = 0;
  };
  input.addEventListener("input", update);
  clear.addEventListener("click", () => {
    input.value = "";
    input.focus();
    update();
  });
}
const logKeywordInput = $("#log-keyword");
const logKeywordClear = $("#clear-log-keyword");
const updateLogKeyword = () => {
  logKeywordClear.hidden = !logKeywordInput.value;
  scheduleLogsFilterReload(250);
  $("#log-list").scrollTop = 0;
};
logKeywordInput.addEventListener("input", updateLogKeyword);
logKeywordClear.addEventListener("click", () => {
  logKeywordInput.value = "";
  logKeywordInput.focus();
  updateLogKeyword();
});
$("#selected-edit").addEventListener("click", () => {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  if (selected) openSubscription(selected);
});
$("#selected-delete").addEventListener("click", () => {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  if (selected) removeSubscription(selected);
});
$("#clear-logs").addEventListener("click", clearLogs);
$("#log-category-filter").addEventListener("change", () => {
  scheduleLogsFilterReload();
  $("#log-list").scrollTop = 0;
});
function setLogFullscreen(enabled) {
  const section = $("#log-section");
  const button = $("#log-fullscreen");
  section.classList.toggle("is-fullscreen", enabled);
  document.body.classList.toggle("log-fullscreen-open", enabled);
  button.setAttribute("aria-pressed", String(enabled));
  button.setAttribute("aria-label", enabled ? "退出全屏日志" : "全屏查看日志");
  button.title = enabled ? "退出全屏日志（Esc）" : "全屏查看日志";
  button.textContent = enabled ? "×" : "⛶";
  if (enabled) $("#log-list").focus({ preventScroll: true });
}
$("#log-fullscreen").addEventListener("click", () => {
  setLogFullscreen(!$("#log-section").classList.contains("is-fullscreen"));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideCoverHoverPreview();
    if ($("#log-section").classList.contains("is-fullscreen")) setLogFullscreen(false);
  }
});
$("#dialog-close").addEventListener("click", () => dialog.close());
$("#dialog-cancel").addEventListener("click", () => dialog.close());
$("#scan-now").addEventListener("click", () => {
  if (["running", "waiting_retry"].includes(currentStatus.state) && currentStatus.run_id) stopScan();
  else runScan();
});
$("#save-settings").addEventListener("click", () => saveSettings());
$("#save-settings-footer").addEventListener("click", () => saveSettings());
$("#add-path-mapping").addEventListener("click", () => {
  pathMappings.push({ local: "", remote: "" });
  renderPathMappings();
  $("#path-mapping-list input:last-of-type")?.focus();
});
$("#clear-token").addEventListener("click", () => saveSettings({ clearToken: true }));
$("#clear-azure-key").addEventListener("click", () => saveSettings({ clearAzureKey: true }));
$("#test-mteam").addEventListener("click", () => testConnection("/api/mteam/test", "#mteam-state"));
$("#test-downloader").addEventListener("click", () => testConnection("/api/downloader/test", "#downloader-state"));
$("#toggle-token").addEventListener("click", (event) => {
  const input = $("#mteam-token");
  input.type = input.type === "password" ? "text" : "password";
  event.currentTarget.title = input.type === "password" ? "显示令牌" : "隐藏令牌";
});

loadSettings().catch((error) => toast(error.message, true));
refresh();
window.setInterval(refresh, 5000);
window.setInterval(() => {
  const expired = new Set();
  for (const [key, state] of videoRefreshStates) {
    if (state.state === "success" && Date.now() - state.completedAt >= 6000) {
      videoRefreshStates.delete(key);
      expired.add(key);
    }
  }
  for (const button of document.querySelectorAll(".video-refresh.is-refreshing, .video-refresh.refresh-success")) {
    const state = currentVideoRepairStatus(button);
    if ((state?.state === "pending" && state.showElapsed !== false) || expired.has(button.dataset.refreshKey)) {
      updateVideoRefreshButton(button, state);
    }
  }
  renderRetryCountdown(repairJobs.find((job) => ["queued", "running"].includes(job.state) && job.phase === "waiting_retry"));
  if (currentStatus.state === "waiting_retry") {
    renderStatus(currentStatus);
    renderScanButtons();
  }
}, 1000);
