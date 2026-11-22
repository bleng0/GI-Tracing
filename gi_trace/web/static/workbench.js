/* Admin workbench editor. Loaded from /static, so the dashboard's strict CSP
   (default-src 'self') applies and no inline script is needed. */
(() => {
  const form = document.getElementById("wb-form");
  if (!form) return;

  const modeInput = document.getElementById("wb-mode");
  const code = document.getElementById("wb-code");
  const gutter = document.getElementById("wb-gutter");
  const highlight = document.getElementById("wb-highlight");
  const hint = document.getElementById("wb-hint");
  const runButton = document.getElementById("wb-run");
  const op = document.getElementById("wb-op");
  const collection = document.getElementById("wb-collection");
  // Keep each language's editor text when switching, so a sample or a half-
  // written query is not lost just because the admin looked at the other mode.
  const drafts = {sql: "", nosql: ""};
  let activeMode = modeInput.value;
  drafts[activeMode] = code.value;
  const keywords = [
    "SELECT", "FROM", "WHERE", "WITH", "AS", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "ON",
    "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END", "DISTINCT", "UNION", "ALL",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ASC", "DESC"
  ].join("|");
  const sqlToken = new RegExp(
    "(--[^\\n]*|'(?:''|[^'])*'|\"(?:[^\"])*\"|\\b(?:" + keywords + ")\\b|\\b\\d+(?:\\.\\d+)?\\b)", "gi");
  const jsonToken = /("(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?\b)/g;

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[ch]);
  }

  function paint(raw, mode) {
    const token = mode === "sql" ? sqlToken : jsonToken;
    token.lastIndex = 0;
    let out = "", last = 0, match;
    while ((match = token.exec(raw)) !== null) {
      if (!match[0]) continue;
      out += escapeHtml(raw.slice(last, match.index));
      const value = match[0];
      const head = value[0];
      let cls = "tok-keyword";
      if (value.startsWith("--")) cls = "tok-comment";
      else if (head === "'" || head === '"') cls = "tok-string";
      else if (/^-?\d/.test(value)) cls = "tok-number";
      out += '<span class="' + cls + '">' + escapeHtml(value) + "</span>";
      last = match.index + value.length;
    }
    return out + escapeHtml(raw.slice(last)) + "\n";
  }

  function sync() {
    const mode = modeInput.value;
    highlight.innerHTML = paint(code.value, mode);
    const lines = code.value.split("\n").length;
    gutter.textContent = Array.from({ length: lines }, (_, i) => String(i + 1)).join("\n");
    highlight.scrollTop = gutter.scrollTop = code.scrollTop;
    highlight.scrollLeft = code.scrollLeft;
  }

  function setMode(mode) {
    if (mode !== activeMode) {
      drafts[activeMode] = code.value;
      activeMode = mode;
      const sample = document.querySelector(".wb-sample-" + mode);
      code.value = drafts[mode] || (sample ? sample.dataset.code || "" : "");
    }
    modeInput.value = mode;
    document.querySelectorAll(".wb-mode").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    });
    document.querySelectorAll(".wb-sample-sql").forEach((button) => { button.hidden = mode !== "sql"; });
    document.querySelectorAll(".wb-sample-nosql").forEach((button) => { button.hidden = mode !== "nosql"; });
    document.getElementById("wb-sql-schema").hidden = mode !== "sql";
    document.getElementById("wb-nosql-schema").hidden = mode !== "nosql";
    document.getElementById("wb-op-wrap").hidden = mode !== "nosql";
    document.getElementById("wb-collection-wrap").hidden = mode !== "nosql";
    hint.textContent = mode === "sql"
      ? "SELECT or WITH · one statement · no semicolons"
      : "find: JSON filter · aggregate: JSON array of read-only stages";
    sync();
  }

  code.addEventListener("input", sync);
  code.addEventListener("scroll", sync);
  code.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const start = code.selectionStart, end = code.selectionEnd;
      if (event.shiftKey) {
        const lineStart = code.value.lastIndexOf("\n", start - 1) + 1;
        const breakAt = code.value.indexOf("\n", Math.max(start, end));
        const lineEnd = breakAt === -1 ? code.value.length : breakAt;
        const block = code.value.slice(lineStart, lineEnd);
        const parts = block.split("\n");
        const changed = parts.map((part) => part.replace(/^ {1,2}/, "")).join("\n");
        code.value = code.value.slice(0, lineStart) + changed + code.value.slice(lineEnd);
        const removed = block.length - changed.length;
        code.selectionStart = Math.max(lineStart, start - removed);
        code.selectionEnd = Math.max(lineStart, end - removed);
      } else {
        code.value = code.value.slice(0, start) + "  " + code.value.slice(end);
        code.selectionStart = code.selectionEnd = start + 2;
      }
      sync();
    } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      runButton.click();
    }
  });

  document.querySelectorAll(".wb-mode").forEach((button) => {
    button.addEventListener("click", () => {
      setMode(button.dataset.mode);
      code.focus();
    });
  });
  document.querySelectorAll(".wb-sample").forEach((button) => {
    button.addEventListener("click", () => {
      setMode(button.dataset.mode);
      if (button.dataset.op) op.value = button.dataset.op;
      if (button.dataset.collection) collection.value = button.dataset.collection;
      code.value = button.dataset.code || "";
      sync();
      code.focus();
    });
  });
  document.querySelectorAll(".wb-view").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".wb-view").forEach((other) => {
        other.setAttribute("aria-pressed", String(other === button));
      });
      const table = document.getElementById("wb-table-view");
      const json = document.getElementById("wb-json-view");
      if (table) table.hidden = button.dataset.view !== "table";
      if (json) json.hidden = button.dataset.view !== "json";
    });
  });

  setMode(modeInput.value);
})();
