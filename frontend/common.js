/* Small helpers shared by index.html, map.html and dashboard.html. */
(function (global) {
  "use strict";

  var ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  // Escapes text for safe use inside innerHTML / template strings and HTML
  // attributes. Every value that came from a citizen (location name,
  // description) or from the AI (summary, department) MUST go through this
  // before being put into the page - otherwise someone can submit a report
  // whose "location name" is <img src=x onerror=...> and run script in the
  // browser of everyone who opens the map or the dashboard.
  function esc(value) {
    return value == null ? "" : String(value).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  // Turns a stored photo_path / audio_path into a URL. Only absolute http(s)
  // URLs (Supabase Storage) and our own /media/<photos|audio>/<file> paths
  // (older reports saved to local disk) are allowed; anything else -> "".
  function mediaUrl(p) {
    if (!p) return "";
    p = String(p);
    if (/^https?:\/\//i.test(p)) return p;
    var rel = p.replace(/\\/g, "/").replace(/^\/+/, "").replace(/^uploads\//, "");
    return /^(photos|audio)\/[\w\-][\w.\-]*$/.test(rel) ? "/media/" + rel : "";
  }

  // A finite number, or the fallback (default 0).
  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  global.esc = esc;
  global.mediaUrl = mediaUrl;
  global.num = num;
})(window);
