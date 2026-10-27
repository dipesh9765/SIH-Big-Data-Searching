"use strict";

// All text from the server goes through textContent / createTextNode, never innerHTML:
// document content is untrusted, and a stored "<script>" must stay inert.

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function highlighted(snippet, ranges) {
  const p = el("p", "snippet");
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start < cursor) continue;
    p.append(document.createTextNode(snippet.slice(cursor, start)));
    p.append(el("mark", null, snippet.slice(start, end)));
    cursor = end;
  }
  p.append(document.createTextNode(snippet.slice(cursor)));
  return p;
}

async function refreshStats() {
  const s = await api("/v1/stats");
  $("stats").textContent =
    `${s.documents.toLocaleString()} documents · ${s.terms.toLocaleString()} terms · ` +
    `${s.postings.toLocaleString()} postings`;
  $("file").accept = s.accepted_extensions.join(",");
  $("accepted").textContent =
    `Accepted: ${s.accepted_extensions.join(" ")} · up to ${Math.round(s.max_upload_bytes / 2 ** 20)} MB`;
}

async function refreshDocs() {
  const docs = await api("/v1/documents?limit=100");
  const list = $("docs");
  list.replaceChildren();
  if (docs.length === 0) list.append(el("li", "muted", "Nothing indexed yet."));
  for (const doc of docs) {
    const item = el("li");
    item.append(el("span", "doc-title", doc.title));
    item.append(el("span", "muted", ` ${doc.tokens.toLocaleString()} tokens`));
    const remove = el("button", "link", "Delete");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      await api(`/v1/documents/${doc.id}`, { method: "DELETE" });
      await Promise.all([refreshDocs(), refreshStats()]);
      $("results").replaceChildren();
      $("summary").textContent = "";
    });
    item.append(remove);
    list.append(item);
  }
}

$("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const params = new URLSearchParams({ q: $("q").value, mode: $("mode").value, limit: "20" });
  const results = $("results");
  try {
    const data = await api(`/v1/search?${params}`);
    results.replaceChildren();
    $("summary").textContent =
      data.total === 0 ? "No matches." : `${data.total} result${data.total === 1 ? "" : "s"} in ${data.took_ms} ms`;
    for (const hit of data.results) {
      const item = el("li", "result");
      item.append(el("h3", null, hit.document.title));
      item.append(highlighted(hit.snippet, hit.highlights));
      item.append(el("p", "muted", `${hit.document.source_name} · score ${hit.score.toFixed(3)}`));
      results.append(item);
    }
  } catch (error) {
    $("summary").textContent = error.message;
  }
});

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("file").files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  $("upload-msg").textContent = "Indexing…";
  try {
    const doc = await api("/v1/documents", { method: "POST", body: form });
    $("upload-msg").textContent = doc.duplicate
      ? `"${doc.title}" was already indexed.`
      : `Indexed "${doc.title}" (${doc.tokens.toLocaleString()} tokens).`;
    $("upload-form").reset();
    await Promise.all([refreshDocs(), refreshStats()]);
  } catch (error) {
    $("upload-msg").textContent = error.message;
  }
});

Promise.all([refreshStats(), refreshDocs()]).catch((error) => {
  $("stats").textContent = error.message;
});
