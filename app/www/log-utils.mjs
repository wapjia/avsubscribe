function isSeparator(entry, roundId, phase) {
  return entry?.event === "run_separator"
    && entry.round_id === roundId
    && (entry.message || "").includes(phase === "start" ? "开始" : "结束");
}

export function selectLogRound(entries, separatorId, roundId) {
  const selected = entries.findIndex((entry) => entry.id === separatorId);
  if (selected < 0 || !roundId || entries[selected].event !== "run_separator") {
    return entries.filter((entry) => entry.round_id === roundId);
  }

  const selectedPhase = (entries[selected].message || "").includes("开始") ? "start" : "end";
  let start = selected;
  if (selectedPhase === "start") {
    while (start > 0 && isSeparator(entries[start - 1], roundId, "start")) start -= 1;
  } else {
    while (start >= 0 && !isSeparator(entries[start], roundId, "start")) start -= 1;
    if (start < 0) return entries.filter((entry) => entry.round_id === roundId);
    while (start > 0 && isSeparator(entries[start - 1], roundId, "start")) start -= 1;
  }

  let end = start;
  while (end < entries.length && !isSeparator(entries[end], roundId, "end")) {
    if (end > start && isSeparator(entries[end], roundId, "start")) {
      return entries.filter((entry) => entry.round_id === roundId);
    }
    end += 1;
  }
  if (end >= entries.length) return entries.filter((entry) => entry.round_id === roundId);
  while (end + 1 < entries.length && isSeparator(entries[end + 1], roundId, "end")) end += 1;
  return entries.slice(start, end + 1).filter((entry) => entry.round_id === roundId);
}
