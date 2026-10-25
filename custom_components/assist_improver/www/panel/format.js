// Plain formatting, shared by every view: HTML escaping, counts, times, sizes, durations and the developer view's
// addresses. Every dynamic string a view shows goes through esc().

export const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

// "1 fix", "2 fixes", "3 conversations".
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : /(?:s|x|z|ch|sh)$/.test(word) ? "es" : "s"}`;

// Fills {name}-style parts of a TEXT entry.
export const fill = (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");

// "A", "A and B", "A, B and C".
export const listed = (names) => names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names.join("");

export const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}\u00a0min ago`;  // Never "1" at a line's end and "h ago" on the next.
  if (s < 86400) return `${Math.round(s / 3600)}\u00a0h ago`;
  return new Date(iso).toLocaleDateString(undefined, {month: "short", day: "numeric"});
};

// A class name from untrusted text (a log level, a span's status).
export const cls = (s) => String(s ?? "").replace(/[^A-Za-z0-9_-]/g, "");

export const bytes = (n) => n == null ? "–" : n < 1000 ? `${n}\u00a0B` : n < 1e6 ? `${(n / 1e3).toFixed(1)}\u00a0kB`
  : n < 1e9 ? `${(n / 1e6).toFixed(1)}\u00a0MB` : `${(n / 1e9).toFixed(2)}\u00a0GB`;

export const dur = (ms) => {
  if (ms == null || !isFinite(ms)) return "–";
  if (ms === 0) return "0\u00a0ms";
  if (ms < 1) return `${ms.toFixed(2)}\u00a0ms`;
  if (ms < 1000) return `${Math.round(ms)}\u00a0ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}\u00a0s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m\u00a0${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}s`;
  return `${Math.floor(ms / 3600000)}h\u00a0${String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0")}m`;
};

export const num = (n) => n == null ? "–" : Number(n).toLocaleString();

export const isIso = (v) => typeof v === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(v);

export const stamp = (iso, unit = "s") => {
  // Local time to the minute, second or millisecond (unit "min", "s" or "ms"), with the date unless it's today.
  const d = new Date(iso);
  if (!iso || isNaN(d)) return iso ? String(iso) : "–";
  const time = d.toLocaleTimeString(undefined, {hour12: false, hour: "2-digit", minute: "2-digit",
                                                ...(unit === "min" ? {} : {second: "2-digit"})})
    + (unit === "ms" ? `.${String(d.getMilliseconds()).padStart(3, "0")}` : "");
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString(undefined, {month: "short", day: "numeric"})} ${time}`;
};

export const between = (from, to) => from && to ? Date.parse(to) - Date.parse(from) : null;

// The panel's addresses: home, a review, the settings, and a developer view tab.
export const reviewUrl = (id) => id ? `/assist-improver?id=${encodeURIComponent(id)}` : "/assist-improver";
export const SETTINGS_URL = "/assist-improver?view=settings";
export const devUrl = (tab, extra = {}) => {
  const params = new URLSearchParams({view: "developer", tab});
  for (const [key, value] of Object.entries(extra)) if (value) params.set(key, value);
  return `/assist-improver?${params}`;
};
