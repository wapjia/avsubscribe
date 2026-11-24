import assert from "node:assert/strict";
import test from "node:test";
import { activeRepairForVideo, videoRepairStatus } from "../app/www/repair-status.mjs";

test("the picture phase shows overall repair status per number", () => {
  const job = {
    state: "running", phase: "images",
    message: "正在核对 ABC-002 的封面和截图。",
    image_progress: { current_code: "ABC-002", results: { "ABC-001": 0, "ABC-004": 1 } },
  };
  const row = (code) => videoRepairStatus(job, code, `/media/${code}.mp4`);
  assert.equal(row("ABC-001").state, "success");
  assert.equal(row("ABC-001").phaseLabel, "已修复");
  assert.equal(row("ABC-001").locked, true);
  assert.equal(row("ABC-002").phaseLabel, "修复中");
  assert.equal(row("ABC-002").message, job.message);
  assert.equal(row("ABC-002").showElapsed, false);
  assert.equal(row("ABC-003").state, "waiting");
  assert.equal(row("ABC-003").phaseLabel, "等待修复");
  assert.equal(row("ABC-004").state, "error");
  job.phase = "waiting_retry";
  assert.equal(row("ABC-001").state, "success");
  assert.equal(row("ABC-002").phaseLabel, "等待限流结束");
  assert.equal(row("ABC-003").state, "waiting");
});

test("single file repair matches the moved file but never another copy of the same number", () => {
  const job = {
    state: "running", phase: "images", subscription_id: "sub",
    path: "/media/old.mp4", archived_path: "/media/ABC-001/old.mp4",
    image_progress: { current_code: "", results: { "ABC-001": 0 } },
    outcomes: { "/media/ABC-001/old.mp4": { state: "partial", message: "字幕下载失败" } },
  };
  assert.equal(activeRepairForVideo([job], "sub", job.path), job);
  assert.equal(activeRepairForVideo([job], "sub", job.archived_path), job);
  assert.equal(activeRepairForVideo([job], "sub", "/media/ABC-001/other.mp4"), undefined);
  assert.equal(activeRepairForVideo([job], "other", job.archived_path), undefined);
  assert.equal(videoRepairStatus(job, "ABC-001", job.path).phaseLabel, "字幕处理有失败");
  job.state = "completed";
  assert.equal(activeRepairForVideo([job], "sub", job.archived_path), undefined);
});
