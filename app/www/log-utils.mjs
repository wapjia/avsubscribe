function isSeparator(entry, roundId, phase) {
  return entry?.event === "run_separator"
    && entry.round_id === roundId
    && (entry.message || "").includes(phase === "start" ? "开始" : "结束");
}

export function filterLogEntries(entries, matches = null) {
  const matchingLogs = new Set(entries.filter((entry) =>
    entry.event !== "run_separator" && (!matches || matches(entry))));
  const rounds = new Set([...matchingLogs].map((entry) => entry.round_id).filter(Boolean));
  return entries.filter((entry) => entry.event === "run_separator"
    ? (entry.round_id ? rounds.has(entry.round_id) : !matches)
    : matchingLogs.has(entry));
}

export function matchesKeywords(values, query) {
  const keywords = String(query || "").normalize("NFKC").toLowerCase().split(/\s+/).filter(Boolean);
  const searchable = values.map((value) => String(value || "").normalize("NFKC").toLowerCase());
  return keywords.every((keyword) => searchable.some((value) => value.includes(keyword)));
}

export function splitHttpLog(message) {
  const firstBreak = message.indexOf("\n");
  if (firstBreak < 0) return { heading: message, parameters: "", response: "" };
  const heading = message.slice(0, firstBreak);
  const body = message.slice(firstBreak + 1);
  const marker = "请求参数：";
  if (!body.startsWith(marker)) return { heading, parameters: "", response: body };
  const nextBreak = body.indexOf("\n");
  return {
    heading,
    parameters: body.slice(marker.length, nextBreak < 0 ? body.length : nextBreak),
    response: nextBreak < 0 ? "" : body.slice(nextBreak + 1),
  };
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
