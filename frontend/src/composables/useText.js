// Render chat message text as HTML with autolinked URLs.
//
// Invite-only app with trusted users, so this is intentionally simple: find
// words that are http(s) URLs and wrap them in <a target="_blank"> so people
// don't have to know to do it themselves. Opening in a new tab/window keeps the
// PWA/app context intact. Message text is user-provided, so it is HTML-escaped
// before the URL wrap (the escaped & in query strings is the correct href form).

function esc(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// A URL is a run of non-whitespace characters starting with http:// or https://.
const URL_RE = /https?:\/\/[^\s]+/g;

// Trim trailing sentence punctuation so "check this out." doesn't link the period.
function trimTrailing(url) {
  return url.replace(/[.,!?;:]+$/, "");
}

export function linkifyHtml(text) {
  if (!text) return "";
  return esc(String(text)).replace(URL_RE, (raw) => {
    const url = trimTrailing(raw);
    // If trimming left nothing usable (e.g. a bare "https://" with no host),
    // show the raw text instead of an empty link.
    if (!/^https?:\/\//.test(url)) return raw;
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`;
  });
}
