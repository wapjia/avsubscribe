import { TrimApp } from "./vendor/trim-web-app.js";
import { APP_VERSION } from "./version.js";
import { selectLogRound } from "./log-utils.mjs";

const $ = (selector) => document.querySelector(selector);
const fnosApp = new TrimApp();
const dialog = $("#subscription-dialog");
const form = $("#subscription-form");
let subscriptions = [];
let activity = [];
let logEntries = [];
let busy = false;
let toastTimer = 0;
let activeView = "all";
let selectedSubscriptionId = "";
let existingVideos = [];
let existingForId = "";
let existingVideoCount = 0;
let existingVideosLoading = false;
let lastStatusState = "";
let currentStatus = {};
let repairJobs = [];
const awaitedRepairJobs = new Set();
let refreshInFlight = false;
let subscriptionsLoaded = false;
let logsLoaded = false;
let lastSubscriptionRefresh = 0;
let lastActivityRefresh = 0;
let lastLogId = 0;
let blacklistNumbers = [];
let pathMappings = [];
const videoSubtitleRequests = new Map();
const videoFileInfoRequests = new Map();
const videoCoverRequests = new Map();
const coverPreview = document.createElement("div");
coverPreview.className = "cover-preview";
coverPreview.id = "video-cover-preview";
coverPreview.setAttribute("role", "tooltip");
coverPreview.hidden = true;
document.body.append(coverPreview);
let coverAnchor = null;
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

function useSystemBrowser(link) {
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  let opening = false;
  link.addEventListener("click", async (event) => {
    // Native fnOS WebViews need the host bridge; target=_blank can replace the app.
    if (fnosApp.isWeb !== false) return;
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

function hideCoverPreview() {
  coverAnchor?.removeAttribute("aria-describedby");
  coverAnchor = null;
  coverPreview.hidden = true;
  coverPreview.replaceChildren();
}

function positionCoverPreview() {
  if (!coverAnchor?.isConnected) return hideCoverPreview();
  const bounds = coverAnchor.getBoundingClientRect();
  const box = coverPreview.getBoundingClientRect();
  const width = document.documentElement.clientWidth;
  const height = window.innerHeight;
  const left = bounds.right + 10 + box.width <= width - 8 ? bounds.right + 10 : Math.max(8, bounds.left - box.width - 10);
  coverPreview.style.left = `${Math.min(left, Math.max(8, width - box.width - 8))}px`;
  coverPreview.style.top = `${Math.max(8, Math.min(bounds.top, height - box.height - 8))}px`;
}

function attachCoverPreview(anchor, code, files, subscriptionId) {
  anchor.tabIndex = 0;
  const show = async () => {
    hideCoverPreview();
    coverAnchor = anchor;
    anchor.setAttribute("aria-describedby", coverPreview.id);
    coverPreview.hidden = false;
    const file = [...files].filter((item) => item.mteam_id).sort((a, b) => {
      for (let i = 0; i < 3; i++) {
        const difference = (b.rank?.[i] || 0) - (a.rank?.[i] || 0);
        if (difference) return difference;
      }
      return 0;
    })[0];
    const caption = document.createElement("div");
    caption.className = "cover-caption";
    caption.textContent = file?.subtitle || file?.rank_title || code;
    const message = document.createElement("div");
    message.className = "cover-message";
    message.textContent = file ? "正在加载封面…" : "暂无关联 M-Team ID，请先修复数据。";
    coverPreview.replaceChildren(caption, message);
    positionCoverPreview();
    if (!file) return;
    try {
      let url = file.cover_url || "";
      if (!url && !file.cover_checked) {
        const key = `${subscriptionId}:${file.path}:${file.mteam_id}`;
        if (!videoCoverRequests.has(key)) {
          const request = api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos/${encodeURIComponent(code)}/cover`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path })
          }).catch((error) => { videoCoverRequests.delete(key); throw error; });
          videoCoverRequests.set(key, request);
        }
        const result = await videoCoverRequests.get(key);
        file.cover_url = result.cover_url || "";
        file.cover_checked = Boolean(result.cover_checked);
        url = file.cover_url;
      }
      if (coverAnchor !== anchor) return;
      if (!url || !/^https?:\/\//i.test(url)) {
        message.textContent = "M-Team 详情暂无封面。";
        return;
      }
      const image = document.createElement("img");
      image.alt = `${code} 封面`;
      image.referrerPolicy = "no-referrer";
      image.addEventListener("load", () => {
        if (coverAnchor !== anchor) return;
        message.remove();
        positionCoverPreview();
      });
      image.addEventListener("error", () => {
        if (coverAnchor !== anchor) return;
        image.remove();
        message.textContent = "封面图片加载失败。";
        positionCoverPreview();
      });
      coverPreview.insertBefore(image, caption);
      image.src = url;
      positionCoverPreview();
    } catch (error) {
      if (coverAnchor === anchor) message.textContent = `封面读取失败：${error.message}`;
    }
  };
  anchor.addEventListener("mouseenter", show);
  anchor.addEventListener("focus", show);
  anchor.addEventListener("mouseleave", hideCoverPreview);
  anchor.addEventListener("blur", hideCoverPreview);
}
window.addEventListener("resize", hideCoverPreview);
window.addEventListener("scroll", hideCoverPreview, true);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideCoverPreview(); });

function updateVideoRefreshButton(button, state) {
  const pending = state?.state === "pending";
  const success = state?.state === "success";
  const failed = state?.state === "error";
  button.disabled = Boolean(pending);
  button.classList.toggle("is-refreshing", Boolean(pending));
  button.classList.toggle("refresh-success", Boolean(success));
  button.classList.toggle("refresh-error", Boolean(failed));
  button.setAttribute("aria-busy", String(Boolean(pending)));
  const seconds = pending ? Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000)) : 0;
  const label = pending ? `${state.phaseLabel || "修复中"}${seconds ? ` ${formatLogDuration(seconds)}` : "…"}`
    : success ? "已修复" : failed ? "重试" : "";
  button.replaceChildren();
  const icon = document.createElement("span");
  icon.className = "video-refresh-icon";
  icon.textContent = success ? "✓" : failed ? "!" : "↻";
  icon.setAttribute("aria-hidden", "true");
  button.append(icon);
  if (label) {
    const text = document.createElement("span");
    text.textContent = label;
    button.append(text);
  }
  button.title = pending ? (state.message || "正在核对 ID、刷新信息及检查字幕，请稍候。")
    : success ? state.message
    : failed ? `${state.message}\n点击重试修复此视频数据。`
    : "修复此视频数据：先核对并修正 M-Team ID，再刷新信息和 MediaInfo；中文字幕且缺少字幕时自动下载。";
  button.setAttribute("aria-label", `${button.dataset.filename}：${label || "修复此视频数据"}${failed ? `，${state.message}` : ""}`);
}

async function refreshVideoInformation(subscriptionId, code, file) {
  const key = videoRefreshKey(subscriptionId, file.path);
  if (videoRefreshStates.get(key)?.state === "pending" || repairJobs.some((job) =>
    job.subscription_id === subscriptionId && ["queued", "running"].includes(job.state) && (!job.path || job.path === file.path))) return;
  videoRefreshStates.set(key, { state: "pending", startedAt: Date.now(), phaseLabel: "正在排队" });
  renderExistingVideos();
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/repair`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: file.path, code })
    });
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
      button.title = [item.performer, item.keywords].filter(Boolean).join(" ");
      const dot = document.createElement("span");
      dot.className = "subscription-dot";
      const label = document.createElement("span");
      label.className = "subscription-nav-label";
      label.dataset.subscriptionId = item.id;
      label.textContent = `${item.performer}(${Number(item.video_count) || 0})`;
      label.title = `本地影片 ${Number(item.video_count) || 0} 个（按视频文件统计）`;
      button.append(dot, label);
      button.addEventListener("click", () => showSubscription(item.id));
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

function renderPage() {
  const settings = activeView === "settings";
  const all = activeView === "all";
  $("#settings-view").hidden = !settings;
  $("#subscription-view").hidden = settings || all;
  $("#dashboard-activity").hidden = !all;
  $("#mobile-subscription-bar").hidden = settings;
  $("#log-section").hidden = settings;
  $("#clear-logs").hidden = settings || (activeView === "subscription" && !subscriptions.some((item) => item.id === selectedSubscriptionId));
  $("#clear-logs").textContent = activeView === "subscription" ? "清理当前女优日志" : "清理全部日志";
  for (const button of document.querySelectorAll(".nav-item")) {
    const active = button.dataset.view === activeView;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  $("#selected-title").textContent = selected?.performer || "订阅已删除";
  const description = $("#selected-description");
  description.replaceChildren();
  if (selected) {
    const searchTerms = [selected.performer, selected.keywords].map((value) => String(value || "").trim()).filter(Boolean);
    const searchLink = document.createElement("a");
    searchLink.className = "subscription-search-link";
    searchLink.textContent = searchTerms.join(" · ");
    searchLink.href = `https://kp.m-team.cc/browse/adult?keyword=${encodeURIComponent(searchTerms.join(" "))}`;
    useSystemBrowser(searchLink);
    searchLink.title = `在 M-Team 搜索：${searchTerms.join(" ")}`;
    searchLink.setAttribute("aria-label", searchLink.title);
    description.append(searchLink);
  } else {
    description.textContent = "查看订阅设置";
  }
  $("#selected-scan").hidden = all;
  $("#selected-repair-data").hidden = all || !selected;
  $("#selected-edit").hidden = all || !selected;
  $("#selected-delete").hidden = all || !selected;
  const summary = $("#subscription-summary");
  summary.replaceChildren();
  if (selected) {
    const details = [["搜索词", [selected.performer, selected.keywords].filter(Boolean).join(" ")], ["飞牛目录", selected.download_dir], ["下载器路径", mapDownloaderPath(selected.download_dir)], ["自动洗版", selected.wash_enabled ? "开启" : "关闭"]];
    for (const [labelText, valueText] of details) {
      const item = document.createElement("div");
      item.className = "detail-item";
      if (labelText === "飞牛目录" || labelText === "下载器路径") item.classList.add("detail-path");
      if (labelText === "自动洗版") item.classList.add("detail-wash");
      const label = document.createElement("span");
      label.textContent = labelText;
      const value = document.createElement("strong");
      value.textContent = valueText;
      value.title = valueText;
      item.append(label, value);
      summary.append(item);
    }
    if (existingForId !== selected.id) loadExistingVideos(selected.id);
  } else {
    existingForId = "";
    existingVideos = [];
    existingVideoCount = 0;
    existingVideosLoading = false;
  }
  renderActivity();
  renderLogs(logEntries);
}

function showSubscription(id) {
  selectedSubscriptionId = id;
  activeView = "subscription";
  existingForId = "";
  renderSubscriptions();
}

function renderExistingVideos(errorMessage = "") {
  hideCoverPreview();
  const body = $("#existing-body");
  body.replaceChildren();
  $("#existing-count").textContent = `${existingVideoCount} 个视频`;
  if (!existingVideos.length) {
    const row = document.createElement("tr");
    row.className = "existing-empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty";
    cell.textContent = errorMessage || (existingVideosLoading ? "正在加载本地影片…" : existingVideoCount > 0 ? `已扫描到 ${existingVideoCount} 个视频文件，但未能从文件名或所在文件夹识别番号。` : "暂未找到本地视频。");
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of existingVideos) {
    const row = document.createElement("tr");
    row.className = "existing-number-row";
    const codeCell = document.createElement("td");
    codeCell.textContent = item.code || "";
    codeCell.className = "number-group-cell";
    attachCoverPreview(codeCell, item.code, item.files || [], selectedSubscriptionId);
    if ((item.files || []).length > 1) {
      const count = document.createElement("span");
      count.className = "number-file-count";
      count.textContent = `${item.files.length} 个版本`;
      codeCell.append(count);
    }
    row.append(codeCell);

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
      versionRow.append(versionLine);
      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.className = "video-refresh";
      refresh.dataset.refreshKey = videoRefreshKey(subscriptionId, file.path);
      refresh.dataset.filename = file.name;
      const activeRepair = repairJobs.find((job) => job.subscription_id === subscriptionId && ["queued", "running"].includes(job.state) && (!job.path || job.path === file.path));
      if (activeRepair) {
        const phaseLabel = activeRepair.state === "queued" ? "排队中" : ({
          scan: "扫描视频", matching: "核对 ID", details: "刷新信息", subtitles: "检查字幕", waiting_retry: "等待限流结束"
        })[activeRepair.phase] || "修复中";
        videoRefreshStates.set(refresh.dataset.refreshKey, {
          state: "pending", startedAt: Date.parse(activeRepair.started_at || activeRepair.queued_at) || Date.now(),
          phaseLabel, message: activeRepair.message, jobId: activeRepair.job_id
        });
      }
      updateVideoRefreshButton(refresh, videoRefreshStates.get(refresh.dataset.refreshKey));
      refresh.addEventListener("click", () => refreshVideoInformation(subscriptionId, item.code, file));
      versionRow.append(refresh);
      const subtitlePaths = file.subtitle_paths || [];
      if (subtitlePaths.length) {
        const marker = document.createElement("span");
        marker.className = "subtitle-marker";
        marker.textContent = "CC";
        marker.title = `本地字幕路径：\n${subtitlePaths.join("\n")}`;
        marker.setAttribute("aria-label", `存在 ${subtitlePaths.length} 个本地字幕文件`);
        versionRow.append(marker);
      }
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
        if (videoRefreshStates.get(videoRefreshKey(subscriptionId, file.path))?.state === "pending") return;
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

async function loadExistingVideos(subscriptionId) {
  existingForId = subscriptionId;
  existingVideos = [];
  existingVideoCount = 0;
  existingVideosLoading = true;
  renderExistingVideos();
  try {
    const result = await api(`/api/subscriptions/${encodeURIComponent(subscriptionId)}/videos`);
    if (selectedSubscriptionId !== subscriptionId) return;
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
    if (selectedSubscriptionId === subscriptionId) {
      existingVideosLoading = false;
      renderExistingVideos(`读取目录失败：${error.message}`);
    }
  }
}

function renderLogs(entries) {
  const list = $("#log-list");
  list.replaceChildren();
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  const visibleEntries = activeView === "subscription"
    ? entries.filter((entry) => selected && entry.subscription === selected.performer)
    : entries;
  const roundsWithVisibleLogs = new Set(visibleEntries
    .filter((entry) => entry.event !== "run_separator" && entry.round_id)
    .map((entry) => entry.round_id));
  const entriesWithContent = visibleEntries.filter((entry) =>
    entry.event !== "run_separator" || !entry.round_id || roundsWithVisibleLogs.has(entry.round_id));
  const entriesToRender = compactLogStages(uniqueRoundSeparators(entriesWithContent));
  $("#log-count").textContent = `${entriesToRender.length} 条`;
  if (!entriesToRender.length) {
    const empty = document.createElement("li");
    empty.className = "log-empty";
    empty.textContent = "等待检查任务。";
    list.append(empty);
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
        row.append(makeButton("复制本轮", "text-button log-copy-round", () => copyLogRound(entry.id, entry.round_id, entriesWithContent), "复制本轮可见日志"));
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
    const text = document.createElement("span");
    text.textContent = formattedLogEntry(entry);
    message.append(category, text);
    row.append(time, source, message);
    list.append(row);
  }
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
  subtitle_download_completed: "字幕", subtitle_download_failed: "字幕",
  subtitle_not_found: "字幕", subtitle_directory_unavailable: "字幕",
  subtitle_id_missing: "字幕", subtitle_metadata_failed: "字幕",
  subtitle_collection_completed: "字幕汇总", subtitle_collection_failed: "字幕汇总",
  local_video_info_refreshed: "本地视频", mteam_id_backfill: "M-Team ID",
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
  if (/^repair_/.test(event)) return "修复";
  if (/^debug_/.test(event)) return "排查";
  if (/^subtitle_/.test(event)) return "字幕";
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
  const text = roundEntries.map(logLine).join("\n");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const input = document.createElement("textarea");
      input.value = text;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error("浏览器未允许访问剪贴板");
    }
    toast("本轮日志已复制");
  } catch (error) {
    toast(`复制日志失败：${error.message}`, true);
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
    renderLogs(logEntries);
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
    state.lastElementChild.textContent = `限流重试 ${formatCountdown(seconds)}`;
  } else {
    state.lastElementChild.textContent = running ? "正在检查" : status.state === "failed" ? "检查失败" : "后台待命";
  }
  if (running) {
    nextScan.textContent = "";
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
  const retryLabel = retrying ? `M-Team 冷却中，${formatCountdown(Math.max(0, Math.ceil(Number(currentStatus.retry_at || 0) - Date.now() / 1000)))} 后自动重试` : "";
  sidebar.disabled = busy;
  sidebar.textContent = busy ? "…" : "↻";
  sidebar.title = retrying ? retryLabel : busy ? "检查中" : "立即检查";
  sidebar.setAttribute("aria-label", sidebar.title);
  sidebar.setAttribute("aria-busy", String(busy));
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

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const now = Date.now();
    const loadSubscriptions = !subscriptionsLoaded || now - lastSubscriptionRefresh >= 30000;
    const loadActivity = activeView === "all" && now - lastActivityRefresh >= 15000;
    const logsPath = logsLoaded ? `/api/logs?after_id=${lastLogId}` : "/api/logs";
    const [nextSubscriptions, status, nextActivity, nextLogs, repairs] = await Promise.all([
      loadSubscriptions ? api("/api/subscriptions") : Promise.resolve(null),
      api("/api/status"),
      loadActivity ? api("/api/activity") : Promise.resolve(null),
      api(logsPath),
      api("/api/repair-jobs")
    ]);
    if (nextSubscriptions) {
      subscriptions = nextSubscriptions;
      subscriptionsLoaded = true;
      lastSubscriptionRefresh = Date.now();
    }
    if (nextActivity) {
      activity = nextActivity;
      lastActivityRefresh = Date.now();
    }
    if (!logsLoaded || nextLogs.length) {
      logEntries = [...logEntries, ...nextLogs].slice(-500);
      logsLoaded = true;
      lastLogId = logEntries.reduce((maximum, entry) => Math.max(maximum, Number(entry.id) || 0), lastLogId);
      renderLogs(logEntries);
    }
    if (activeView === "subscription" && !subscriptions.some((item) => item.id === selectedSubscriptionId)) {
      activeView = "all";
      selectedSubscriptionId = "";
    }
    if (nextSubscriptions) renderSubscriptions();
    renderStatus(status);
    if (nextActivity) renderActivity();
    renderDataRepairStatus(repairs);
    if (selectedSubscriptionId && ["completed", "failed"].includes(status.state) && ["running", "queued"].includes(lastStatusState)) {
      loadExistingVideos(selectedSubscriptionId);
    }
    lastStatusState = status.state;
    busy = status.state === "running" || status.state === "queued" || status.state === "waiting_retry";
    renderScanButtons();
  } catch (error) {
    $("#scheduler-state").dataset.state = "failed";
    $("#scheduler-state").lastElementChild.textContent = "连接异常";
  } finally {
    refreshInFlight = false;
  }
}

function renderDataRepairStatus(jobs) {
  const previouslyActive = repairJobs.some((job) => ["queued", "running"].includes(job.state));
  repairJobs = jobs || [];
  const button = $("#selected-repair-data");
  const allActive = repairJobs.filter((job) => ["queued", "running"].includes(job.state));
  const active = repairJobs.filter((job) => job.subscription_id === selectedSubscriptionId && ["queued", "running"].includes(job.state));
  const repairCooldown = repairJobs.find((job) => ["queued", "running"].includes(job.state) && job.phase === "waiting_retry");
  renderRetryCountdown(repairCooldown);
  if (allActive.length) {
    const state = $("#scheduler-state");
    state.dataset.state = repairCooldown ? "waiting_retry" : "running";
    state.lastElementChild.textContent = repairCooldown ? "修复等待限流恢复" : "正在修复数据";
    $("#next-scan-time").textContent = "";
  } else {
    renderStatus(currentStatus);
  }
  const phaseLabel = (job) => job.state === "queued" ? "排队中" : ({
    scan: "扫描视频", matching: "核对 ID", details: "刷新信息", subtitles: "检查字幕", waiting_retry: "等待限流结束"
  })[job.phase] || "修复中";
  button.disabled = active.length > 0;
  const current = active[0];
  if (current) {
    const progress = current.phase === "waiting_retry"
      ? ` ${formatCountdown(Math.max(0, Math.ceil(Number(current.retry_at || 0) - Date.now() / 1000)))}`
      : current.total > 0 ? ` ${current.completed}/${current.total}` : "";
    button.textContent = `${phaseLabel(current)}${progress}…`;
    button.title = current.message;
  } else {
    button.textContent = "修复数据";
    button.title = "先逐文件核对并修正 M-Team ID，再刷新影片信息和 MediaInfo；中文字幕缺少配套字幕时下载。已有字幕保留，缺 ID 或匹配冲突时跳过并记录原因。";
  }
  button.setAttribute("aria-live", "polite");
  for (const group of existingVideos) {
    for (const file of group.files || []) {
      const job = active.find((value) => !value.path || value.path === file.path);
      const key = videoRefreshKey(selectedSubscriptionId, file.path);
      if (job) videoRefreshStates.set(key, { state: "pending", startedAt: Date.parse(job.started_at || job.queued_at) || Date.now(), phaseLabel: phaseLabel(job), message: job.message, jobId: job.job_id });
      else if (videoRefreshStates.get(key)?.state === "pending" && videoRefreshStates.get(key)?.jobId) videoRefreshStates.delete(key);
    }
  }
  for (const job of repairJobs) {
    if (!awaitedRepairJobs.has(job.job_id) || !["completed", "failed"].includes(job.state)) continue;
    awaitedRepairJobs.delete(job.job_id);
    videoFileInfoRequests.clear();
    videoCoverRequests.clear();
    videoSubtitleRequests.clear();
    if (job.path) {
      const outcome = job.outcomes?.[job.path];
      const failed = job.state === "failed" || ["failed", "partial", "skipped"].includes(outcome?.state);
      videoRefreshStates.set(videoRefreshKey(job.subscription_id, job.path), {
        state: failed ? "error" : "success", message: outcome?.message || job.message, completedAt: Date.now()
      });
    }
    toast(job.message, job.state === "failed" || Number(job.summary?.failed) > 0);
    if (selectedSubscriptionId === job.subscription_id) loadExistingVideos(job.subscription_id);
  }
  if (active.length || previouslyActive) renderExistingVideos();
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

function openSubscription(item = null) {
  form.reset();
  $("#subscription-id").value = item?.id || "";
  $("#dialog-title").textContent = item ? "编辑订阅" : "新建订阅";
  $("#performer").value = item?.performer || "";
  $("#keywords").value = item?.keywords || "";
  $("#download-dir").value = item?.download_dir || "";
  $("#wash-enabled").checked = item?.wash_enabled !== false;
  blacklistNumbers = [...(item?.blacklist || [])];
  renderBlacklistTags();
  dialog.showModal();
  $("#performer").focus();
}

async function saveSubscription(event) {
  event.preventDefault();
  const id = $("#subscription-id").value;
  const payload = {
    performer: $("#performer").value.trim(), keywords: $("#keywords").value.trim(),
    download_dir: $("#download-dir").value.trim(), wash_enabled: $("#wash-enabled").checked,
    blacklist: [...blacklistNumbers]
  };
  const button = $("#submit-subscription");
  button.disabled = true;
  try {
    await api(id ? `/api/subscriptions/${encodeURIComponent(id)}` : "/api/subscriptions", {
      method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    existingForId = "";
    dialog.close();
    await refresh();
    toast(id ? "订阅已更新" : "订阅已创建");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function removeSubscription(item) {
  if (!window.confirm(`删除“${item.performer}”订阅？已加入下载器的任务不会被删除。`)) return;
  try {
    await api(`/api/subscriptions/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    if (selectedSubscriptionId === item.id) {
      selectedSubscriptionId = "";
      activeView = "all";
    }
    await refresh();
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
  $("#debug-mode").checked = Boolean(settings.debug_mode);
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

async function saveSettings(clearToken = false) {
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
    debug_mode: $("#debug-mode").checked,
    clear_mteam_token: clearToken
  };
  if ($("#mteam-token").value) payload.mteam_token = $("#mteam-token").value;
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
    $("#debug-mode").checked = Boolean(settings.debug_mode);
    $("#mteam-request-interval").value = settings.mteam_request_interval_seconds;
    renderPathMappings();
    renderPage();
    toast(clearToken ? "令牌已清除" : "设置已保存");
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
    renderPage();
    renderSubscriptions();
  });
}
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
$("#selected-edit").addEventListener("click", () => {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  if (selected) openSubscription(selected);
});
$("#selected-delete").addEventListener("click", () => {
  const selected = subscriptions.find((item) => item.id === selectedSubscriptionId);
  if (selected) removeSubscription(selected);
});
$("#clear-logs").addEventListener("click", clearLogs);
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
  if (event.key === "Escape" && $("#log-section").classList.contains("is-fullscreen")) {
    setLogFullscreen(false);
  }
});
$("#dialog-close").addEventListener("click", () => dialog.close());
$("#dialog-cancel").addEventListener("click", () => dialog.close());
$("#scan-now").addEventListener("click", () => runScan());
$("#save-settings").addEventListener("click", () => saveSettings());
$("#save-settings-footer").addEventListener("click", () => saveSettings());
$("#add-path-mapping").addEventListener("click", () => {
  pathMappings.push({ local: "", remote: "" });
  renderPathMappings();
  $("#path-mapping-list input:last-of-type")?.focus();
});
$("#clear-token").addEventListener("click", () => saveSettings(true));
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
  for (const [key, state] of videoRefreshStates) {
    if (state.state === "success" && Date.now() - state.completedAt >= 6000) videoRefreshStates.delete(key);
  }
  for (const button of document.querySelectorAll(".video-refresh")) {
    updateVideoRefreshButton(button, videoRefreshStates.get(button.dataset.refreshKey));
  }
  renderRetryCountdown(repairJobs.find((job) => ["queued", "running"].includes(job.state) && job.phase === "waiting_retry"));
  if (currentStatus.state === "waiting_retry") {
    renderStatus(currentStatus);
    renderScanButtons();
  }
}, 1000);
