// Shared timestamp formatting. The backend sends ISO-8601 UTC strings
// (e.g. "2026-09-04T21:05:02.076873Z"), so new Date() parses them as UTC and
// these toLocale* calls render them in the viewer's local timezone. Centralized
// here so every view formats time the same way.

function parse(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

// Same calendar day? (compares local Y/M/D)
function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

// Chat message timestamps: time only today, "Sep 4 3:04 PM" otherwise.
export function timeStr(iso) {
  const d = parse(iso);
  if (!d) return "";
  const now = new Date();
  if (sameDay(d, now)) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return (
    d.toLocaleDateString([], { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  );
}

// "September 4, 2026" (member join date).
export function dateOnly(iso) {
  const d = parse(iso);
  if (!d) return "—";
  return d.toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" });
}

// "Sep 4, 2026, 3:04 PM" (last-active / general timestamps).
export function dateAndTime(iso) {
  const d = parse(iso);
  if (!d) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

// "Sep 4, 2026" (gallery file dates).
export function fileDate(iso) {
  const d = parse(iso);
  if (!d) return "";
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
