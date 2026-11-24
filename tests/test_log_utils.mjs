import assert from "node:assert/strict";
import test from "node:test";
import { filterLogEntries, matchesKeywords, selectLogRound, splitHttpLog } from "../app/www/log-utils.mjs";

test("request parameters stay separate from the response JSON when copying", () => {
  const heading = "M-Team /dmm/dmmInfo · HTTP 200(缓存) · CAWB-040 · 耗时 1.04 秒";
  const parameters = '{"dmmCode": "cawb040"}';
  const response = '{\n  "code": "0"\n}';
  assert.deepEqual(splitHttpLog(`${heading}\n请求参数：${parameters}\n${response}`), { heading, parameters, response });
  assert.deepEqual(splitHttpLog(`${heading}\n${response}`), { heading, parameters: "", response });
  assert.deepEqual(splitHttpLog(`${heading}\n请求参数：${parameters}`), { heading, parameters, response: "" });
});

const separator = (id, round_id, message, subscription = "Actress") => ({
  id, round_id, event: "run_separator", message, subscription
});
const line = (id, round_id, message, subscription = "Actress") => ({
  id, round_id, event: "status", message, subscription
});

test("copying a round returns only the selected run when several runs are visible", () => {
  const entries = [
    separator(1, "round-1", "本轮检查开始"), line(2, "round-1", "first run"), separator(3, "round-1", "本轮检查结束"),
    separator(4, "round-2", "本轮检查开始"), line(5, "round-2", "second run"), separator(6, "round-2", "本轮检查结束"),
  ];
  assert.deepEqual(selectLogRound(entries, 6, "round-2").map((entry) => entry.message), [
    "本轮检查开始", "second run", "本轮检查结束",
  ]);
});

test("separator occurrence disambiguates reused round ids", () => {
  const entries = [
    separator(1, "same-id", "本轮检查开始"), line(2, "same-id", "old run"), separator(3, "same-id", "本轮检查结束"),
    separator(4, "same-id", "本轮检查开始"), line(5, "same-id", "new run"), separator(6, "same-id", "本轮检查结束"),
  ];
  assert.deepEqual(selectLogRound(entries, 4, "same-id").map((entry) => entry.message), [
    "本轮检查开始", "new run", "本轮检查结束",
  ]);
  assert.deepEqual(selectLogRound(entries, 3, "same-id").map((entry) => entry.message), [
    "本轮检查开始", "old run", "本轮检查结束",
  ]);
});

test("multi-subscription separators still copy the complete shared run", () => {
  const entries = [
    separator(1, "round", "本轮检查开始", "Actress A"), separator(2, "round", "本轮检查开始", "Actress B"),
    line(3, "round", "first subscription", "Actress A"), line(4, "round", "second subscription", "Actress B"),
    separator(5, "round", "本轮检查结束", "Actress A"), separator(6, "round", "本轮检查结束", "Actress B"),
  ];
  assert.deepEqual(selectLogRound(entries, 6, "round").map((entry) => entry.id), [1, 2, 3, 4, 5, 6]);
});

test("malformed boundary falls back to strict round id filtering", () => {
  const entries = [line(1, "round-1", "older"), line(2, "round-2", "selected")];
  assert.deepEqual(selectLogRound(entries, 99, "round-2").map((entry) => entry.message), ["selected"]);
});

test("keyword filtering requires every space-separated fuzzy term", () => {
  assert.equal(matchesKeywords(["IPZZ-079", "封面下载失败（HTTP 403）"], "IPZZ 079"), true);
  assert.equal(matchesKeywords(["IPZZ-079", "封面下载失败（HTTP 403）"], "IPZZ 403"), true);
  assert.equal(matchesKeywords(["IPZZ-079", "封面下载失败（HTTP 403）"], "IPZZ 404"), false);
  assert.equal(matchesKeywords(["IPZZ-079"], "ipzz 079"), true);
});

test("filtered logs keep separators only for rounds with matching log entries", () => {
  const entries = [
    separator(1, "round-1", "本轮检查开始"), line(2, "round-1", "命中 IPZZ-079"),
    separator(3, "round-1", "本轮检查结束"),
    separator(4, "round-2", "本轮检查开始"), line(5, "round-2", "其他番号"),
    separator(6, "round-2", "本轮检查结束"),
  ];
  assert.deepEqual(filterLogEntries(entries, (entry) => entry.message.includes("IPZZ")).map((entry) => entry.id), [1, 2, 3]);
});
