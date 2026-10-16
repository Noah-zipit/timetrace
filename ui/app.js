/* timetrace UI — live build, no mock data.
   Every number, label, and verdict rendered here comes from the timetrace
   API (FalkorDB-backed). If the API is unreachable, the page shows an
   honest error and renders nothing fabricated.

   Contract:
     GET  /api/stats             -> repo name, commit count, db status
     GET  /api/timeline          -> commits[] {commit, author, timestamp, epoch, ...}
     GET  /api/state?t=<epoch>   -> modules[] {id, name, n_functions}, totals
     POST /api/query {question, t} -> {verdict, summary, proof_chain[], meta}
*/

"use strict";

var API = ""; // same origin: the API serves this UI at "/"

/* ---------- dom ---------- */
var $ = function (id) { return document.getElementById(id); };

var repoName = $("repo-name");
var commitCount = $("commit-count");
var dbDot = $("db-dot");
var dbLabel = $("db-label");
var apiError = $("api-error");
var scrubber = $("scrubber");
var stateLabel = $("state-label");
var stateMeta = $("state-meta");
var stateLoading = $("state-loading");
var stateSummary = $("state-summary");
var treeLoading = $("tree-loading");
var moduleTree = $("module-tree");
var askForm = $("ask-form");
var askInput = $("ask-input");
var askButton = $("ask-button");
var askLoading = $("ask-loading");
var verdictCard = $("verdict-card");
var verdictBadge = $("verdict-badge");
var verdictSummary = $("verdict-summary");
var proofChain = $("proof-chain");
var queriesRun = $("queries-run");
var verdictMeta = $("verdict-meta");

/* ---------- state ---------- */
var timeline = [];        // commits from /api/timeline, ascending
var currentIndex = -1;    // scrubber position
var currentState = null;  // last /api/state payload
var selectedModule = null;// {id, name}
var inflight = 0;         // request counter: stale responses are dropped

/* ---------- helpers ---------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function shortSha(sha) {
  return String(sha || "").slice(0, 7);
}

function fmtDate(iso) {
  var d = new Date(iso);
  if (isNaN(d.getTime())) { return "—"; }
  var p = function (n) { return String(n).padStart(2, "0"); };
  return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
    " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + " UTC";
}

function authorShort(author) {
  var m = /^([^<]+?)\s*<[^>]*>$/.exec(String(author || ""));
  return m ? m[1].trim() : String(author || "—");
}

function verdictClass(verdict) {
  var v = String(verdict || "").toUpperCase();
  if (v.indexOf("RISKY") === 0) { return "risky"; }
  if (v.indexOf("CHANGE WITH CARE") === 0) { return "care"; }
  if (v.indexOf("SAFE TO CHANGE") === 0) { return "safe"; }
  return "unknown";
}

function show(el) { el.hidden = false; }
function hide(el) { el.hidden = true; }

/* ---------- error state ---------- */
function apiDown() {
  show(apiError);
  dbDot.className = "dot down";
  dbLabel.textContent = "FalkorDB: unreachable";
  scrubber.disabled = true;
  askButton.disabled = true;
  hide(stateLoading);
  hide(treeLoading);
  hide(askLoading);
}

/* ---------- top bar ---------- */
function renderStats(stats) {
  repoName.textContent = stats.repo || "—";
  commitCount.textContent = (stats.n_commits == null ? "—" : stats.n_commits) + " commits";
  var ok = stats.connected === true;
  dbDot.className = "dot " + (ok ? "ok" : "down");
  dbLabel.textContent = "FalkorDB: " + (ok ? "connected" : "down");
}

/* ---------- timeline ---------- */
function renderScrubberLabel() {
  var c = timeline[currentIndex];
  if (!c) { return; }
  stateLabel.textContent = fmtDate(c.timestamp && c.timestamp.iso);
  stateMeta.textContent = (c.short || shortSha(c.commit)) + "  ·  " + authorShort(c.author);
}

function fmtEpoch(epoch) {
  if (epoch == null) { return "—"; }
  return fmtDate(new Date(Number(epoch) * 1000).toISOString());
}

async function fetchTimeline() {
  show(stateLoading);
  var res = await fetch(API + "/api/timeline");
  hide(stateLoading);
  if (!res.ok) { throw new Error("timeline " + res.status); }
  var data = await res.json();
  timeline = data.entries || [];
  if (!timeline.length) { throw new Error("timeline empty"); }
  scrubber.max = String(timeline.length - 1);
  scrubber.value = String(timeline.length - 1);
  scrubber.disabled = false;
  currentIndex = timeline.length - 1;
  renderScrubberLabel();
}

/* ---------- code state ---------- */
async function fetchState(idx) {
  var my = ++inflight;
  var c = timeline[idx];
  if (!c) { return; }
  show(treeLoading);
  moduleTree.innerHTML = "";
  try {
    var res = await fetch(API + "/api/state?t=" + encodeURIComponent(c.timestamp.epoch));
    if (!res.ok) { throw new Error("state " + res.status); }
    var data = await res.json();
    if (my !== inflight) { return; } // dropped: a newer scrub already won
    currentState = data;
    renderModules(data);
    var mods = (data.slice && data.slice.modules) || [];
    if (mods.length && !selectedModule) {
      selectModule(mods[0].id, mods[0].name);
    }
  } finally {
    if (my === inflight) { hide(treeLoading); }
  }
}

function renderModules(data) {
  var slice = data.slice || {};
  var stats = data.stats || {};
  stateSummary.textContent =
    (slice.total_functions == null ? "—" : slice.total_functions) + " functions · " +
    (stats.n_edges == null ? "—" : stats.n_edges) + " edges";
  var mods = slice.modules || [];
  moduleTree.innerHTML = mods.map(function (m) {
    var sel = selectedModule && selectedModule.id === m.id;
    return '<li>' +
      '<button type="button" class="module-row' + (sel ? " selected" : "") + '"' +
      ' data-id="' + esc(m.id) + '" data-name="' + esc(m.name) + '"' +
      ' aria-pressed="' + (sel ? "true" : "false") + '"' +
      ' aria-expanded="false">' +
        '<span class="chev" aria-hidden="true">▸</span>' +
        '<span class="module-name">' + esc(m.name) + "</span>" +
        '<span class="fn-count mono">' + esc(m.n_functions) + " fn</span>" +
      "</button>" +
      '<div class="module-detail" hidden>' +
        '<span class="mono">' + esc(m.id) + "</span>" +
        "<span> · " + esc(m.n_functions) + " functions at " + esc(fmtEpoch(data.t)) + "</span>" +
      "</div>" +
    "</li>";
  }).join("");

  Array.prototype.forEach.call(moduleTree.querySelectorAll(".module-row"), function (btn) {
    btn.addEventListener("click", function () {
      var expanded = btn.getAttribute("aria-expanded") === "true";
      btn.setAttribute("aria-expanded", String(!expanded));
      var detail = btn.parentElement.querySelector(".module-detail");
      if (detail) { detail.hidden = expanded; }
      btn.querySelector(".chev").textContent = expanded ? "▸" : "▾";
      // clicking a module also selects it as the question target
      selectModule(btn.getAttribute("data-id"), btn.getAttribute("data-name"));
    });
  });
}

function selectModule(id, name) {
  selectedModule = { id: id, name: name };
  Array.prototype.forEach.call(moduleTree.querySelectorAll(".module-row"), function (btn) {
    var sel = btn.getAttribute("data-id") === id;
    btn.classList.toggle("selected", sel);
    btn.setAttribute("aria-pressed", String(sel));
  });
  askInput.value = "Is it safe to change " + name + " today?";
}

/* ---------- ask ---------- */
async function runQuery(question) {
  var my = ++inflight;
  var c = timeline[currentIndex];
  show(askLoading);
  askButton.disabled = true;
  try {
    var res = await fetch(API + "/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question, t: c && c.timestamp ? c.timestamp.epoch : null })
    });
    if (!res.ok) { throw new Error("query " + res.status); }
    var data = await res.json();
    if (my !== inflight) { return; }
    renderVerdict(data, question);
  } finally {
    if (my === inflight) { hide(askLoading); askButton.disabled = false; }
  }
}

function renderVerdict(data, question) {
  verdictBadge.textContent = data.verdict || "—";
  verdictBadge.className = "verdict-badge " + verdictClass(data.verdict);
  verdictSummary.textContent = data.summary || "";
  proofChain.innerHTML = (data.proof_chain || []).map(function (step, i) {
    return "<li>" +
      '<div class="step-title">Step ' + (i + 1) + ": " + esc(step.title) + "</div>" +
      '<div class="evidence">' + esc(step.evidence) + "</div>" +
      '<div class="source mono">' + esc(step.source) + "</div>" +
    "</li>";
  }).join("");
  var qs = (data.meta && data.meta.queries_run) || [];
  queriesRun.textContent = qs.length ? qs.join("  ·  ") : "—";
  var meta = data.meta || {};
  verdictMeta.textContent =
    (meta.repo ? meta.repo + "  ·  " : "") +
    (meta.target_id ? meta.target_id + "  ·  " : "") +
    (meta.t != null ? fmtEpoch(meta.t) : "");
  show(verdictCard);
  // money-shot transition: visible update each time the verdict re-renders
  verdictCard.classList.remove("flash");
  void verdictCard.offsetWidth; // restart the animation
  verdictCard.classList.add("flash");
}

askForm.addEventListener("submit", function (ev) {
  ev.preventDefault();
  var q = askInput.value.trim();
  if (!q || currentIndex < 0) { return; }
  runQuery(q).catch(function () { apiDown(); });
});

/* ---------- scrubber: re-fetch state AND re-run the question ---------- */
var scrubTimer = null;
scrubber.addEventListener("input", function () {
  currentIndex = Number(scrubber.value);
  renderScrubberLabel();
  clearTimeout(scrubTimer);
  scrubTimer = setTimeout(function () {
    Promise.all([fetchState(currentIndex)]).then(function () {
      // the jaw-drop: verdict follows the scrubbed time
      var q = askInput.value.trim();
      if (q) { return runQuery(q); }
    }).catch(function () { apiDown(); });
  }, 250);
});

/* ---------- boot ---------- */
(async function boot() {
  try {
    var statsRes = await fetch(API + "/api/stats");
    if (!statsRes.ok) { throw new Error("stats " + statsRes.status); }
    renderStats(await statsRes.json());
    await fetchTimeline();
    await fetchState(currentIndex);
  } catch (e) {
    apiDown();
  }
})();
