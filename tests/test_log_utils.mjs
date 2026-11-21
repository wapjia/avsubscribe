import assert from "node:assert/strict";
import test from "node:test";
import { selectLogRound } from "../app/www/log-utils.mjs";

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
