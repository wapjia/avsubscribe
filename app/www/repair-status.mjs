export function repairPhaseLabel(job) {
  return job.state === "queued" ? "排队中" : ({
    scan: "扫描视频", archive: "归档影片", matching: "核对 ID", details: "刷新信息",
    subtitles: "检查字幕", images: "核对图片", waiting_retry: "等待限流结束"
  })[job.phase] || "修复中";
}

export function activeRepairForVideo(jobs, subscriptionId, path) {
  return jobs.find((job) => job.subscription_id === subscriptionId && ["queued", "running"].includes(job.state)
    && (!job.path || job.path === path || job.archived_path === path));
}

export function videoRepairStatus(job, code, path, now = Date.now()) {
  const image = job.image_progress;
  const outcome = job.outcomes?.[job.path ? (job.archived_path || path) : path];
  let state = job.state === "queued" ? "waiting" : "pending";
  let phaseLabel = job.state === "queued" ? "等待修复" : job.phase === "waiting_retry" ? repairPhaseLabel(job) : "修复中";
  let message = job.message;
  let showElapsed = true;

  if (["failed", "skipped"].includes(outcome?.state)) {
    state = outcome.state === "failed" ? "error" : "skipped";
    phaseLabel = outcome.state === "failed" ? "修复失败" : "已跳过";
    message = outcome.message;
  } else if (image?.results && Object.hasOwn(image.results, code)) {
    const errors = Number(image.results[code]) || 0;
    state = errors || outcome?.state === "partial" ? "error" : "success";
    phaseLabel = errors ? "图片处理有失败" : outcome?.state === "partial" ? "字幕处理有失败" : "已修复";
    message = errors ? `${code} 的图片或 NFO 有 ${errors} 项处理失败。`
      : outcome?.message || `${code} 的本轮修复已完成。`;
  } else if (job.state === "running" && (job.phase === "images" || image?.current_code)) {
    showElapsed = false;
    if (image?.current_code !== code) {
      state = "waiting";
      phaseLabel = "等待修复";
      message = "影片资料已核对，等待检查封面、截图和 NFO。";
    }
  }
  return {
    state, phaseLabel, message, showElapsed, locked: true,
    startedAt: Date.parse(job.started_at || job.queued_at) || now,
  };
}
