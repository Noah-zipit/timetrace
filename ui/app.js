/* timetrace UI shell — functional foundation only.
   MOCK DATA: everything rendered on this page comes from the MOCK_*
   constants below. Do not call the real API stubs (they return 501).
   Event week replaces these constants with real fetch() calls. */

"use strict";

// TODO(event-week): replace with fetch('/api/timeline')
const MOCK_TIMELINE = [
  {
    commit: "9f3a1c2",
    author: "noah-zipit",
    timestamp: "2026-10-01T09:12:00Z",
    nodes: [
      { id: "module:app/auth.py", kind: "module", name: "app/auth.py" },
      { id: "class:app/auth.py:Authenticator", kind: "class", name: "Authenticator" },
      { id: "func:app/auth.py:Authenticator.login", kind: "function", name: "Authenticator.login" },
      { id: "func:app/auth.py:hash_password", kind: "function", name: "hash_password" }
    ],
    edges: [
      { from: "module:app/auth.py", to: "class:app/auth.py:Authenticator", kind: "defines" },
      { from: "class:app/auth.py:Authenticator", to: "func:app/auth.py:Authenticator.login", kind: "defines" },
      { from: "func:app/auth.py:Authenticator.login", to: "func:app/auth.py:hash_password", kind: "calls", callee: "hash_password" }
    ]
  },
  {
    commit: "b7e40d9",
    author: "noah-zipit",
    timestamp: "2026-10-04T14:47:00Z",
    nodes: [
      { id: "module:app/auth.py", kind: "module", name: "app/auth.py" },
      { id: "module:app/rate_limit.py", kind: "module", name: "app/rate_limit.py" },
      { id: "class:app/auth.py:Authenticator", kind: "class", name: "Authenticator" },
      { id: "func:app/auth.py:Authenticator.login", kind: "function", name: "Authenticator.login" },
      { id: "func:app/auth.py:hash_password", kind: "function", name: "hash_password" },
      { id: "func:app/rate_limit.py:check", kind: "function", name: "check" }
    ],
    edges: [
      { from: "module:app/auth.py", to: "class:app/auth.py:Authenticator", kind: "defines" },
      { from: "class:app/auth.py:Authenticator", to: "func:app/auth.py:Authenticator.login", kind: "defines" },
      { from: "func:app/auth.py:Authenticator.login", to: "func:app/auth.py:hash_password", kind: "calls", callee: "hash_password" },
      { from: "func:app/auth.py:Authenticator.login", to: "func:app/rate_limit.py:check", kind: "calls", callee: "check" },
      { from: "module:app/auth.py", to: "module:app/rate_limit.py", kind: "imports", names: ["check"] }
    ]
  },
  {
    commit: "e58b213",
    author: "noah-zipit",
    timestamp: "2026-10-08T21:03:00Z",
    nodes: [
      { id: "module:app/auth.py", kind: "module", name: "app/auth.py" },
      { id: "module:app/rate_limit.py", kind: "module", name: "app/rate_limit.py" },
      { id: "class:app/auth.py:Authenticator", kind: "class", name: "Authenticator" },
      { id: "func:app/auth.py:Authenticator.login", kind: "function", name: "Authenticator.login" },
      { id: "func:app/auth.py:Authenticator.verify_token", kind: "function", name: "Authenticator.verify_token" },
      { id: "func:app/rate_limit.py:check", kind: "function", name: "check" }
    ],
    edges: [
      { from: "module:app/auth.py", to: "class:app/auth.py:Authenticator", kind: "defines" },
      { from: "class:app/auth.py:Authenticator", to: "func:app/auth.py:Authenticator.login", kind: "defines" },
      { from: "class:app/auth.py:Authenticator", to: "func:app/auth.py:Authenticator.verify_token", kind: "defines" },
      { from: "func:app/auth.py:Authenticator.login", to: "func:app/rate_limit.py:check", kind: "calls", callee: "check" },
      { from: "module:app/auth.py", to: "module:app/rate_limit.py", kind: "imports", names: ["check"] }
    ]
  }
];

// TODO(event-week): replace with fetch('/api/query', {method:'POST', ...})
const MOCK_ANSWER = {
  verdict: "Safe to change — with one condition.",
  summary: "Changing app/auth.py today is low risk. The only live dependents are Authenticator.login and rate_limit.check; hash_password was removed in e58b213 and no callers reference it.",
  proof_chain: [
    { title: "login gained a rate_limit.check call", source: "commit b7e40d9" },
    { title: "hash_password deleted — zero live callers", source: "commit e58b213" },
    { title: "verify_token added, no external dependents yet", source: "commit e58b213" }
  ]
};

// TODO(event-week): replace with fetch('/api/query', {method:'POST', ...})
const MOCK_CHAIN = [
  {
    title: "Locate the target module in the graph",
    evidence: "app/auth.py exists in all 3 recorded snapshots; 6 nodes and 5 edges reference it at the latest state.",
    source: "snapshot e58b213 · module:app/auth.py"
  },
  {
    title: "Walk dependents backwards in time",
    evidence: "Only Authenticator.login and rate_limit.check call into auth.py. login picked up the check call on 2026-10-04; nothing else touches the module since.",
    source: "edge walk: calls → func:app/auth.py:Authenticator.login, func:app/rate_limit.py:check"
  },
  {
    title: "Check for recently deleted callers",
    evidence: "hash_password was deleted in e58b213. A stale import would break, but no snapshot since then imports it.",
    source: "commit e58b213 · removed: func:app/auth.py:hash_password"
  },
  {
    title: "Render verdict",
    evidence: "Two live dependents, both touched within the last 5 days and covered by recent commits. Change is safe provided rate_limit.check keeps its signature.",
    source: "aggregation over snapshots 9f3a1c2 → e58b213"
  }
];

/* ---------- helpers ---------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

/* ---------- tab bar ---------- */

var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
tabs.forEach(function (tab) {
  tab.addEventListener("click", function () {
    tabs.forEach(function (t) {
      var active = t === tab;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", String(active));
      document.getElementById(t.dataset.view).hidden = !active;
      document.getElementById(t.dataset.view).classList.toggle("active", active);
    });
  });
});

/* ---------- timeline view ---------- */

var scrubber = document.getElementById("scrubber");
var stateLabel = document.getElementById("state-label");
var stateMeta = document.getElementById("state-meta");
var nodeList = document.getElementById("node-list");
var edgeList = document.getElementById("edge-list");

function formatDate(iso) {
  var d = new Date(iso);
  return d.toISOString().slice(0, 10);
}

function renderState(idx) {
  var snap = MOCK_TIMELINE[idx];
  if (!snap) { return; }

  stateLabel.textContent = "state at " + formatDate(snap.timestamp);
  stateMeta.textContent = snap.commit + " · " + snap.author + " · " + snap.timestamp;

  nodeList.innerHTML = snap.nodes.map(function (n) {
    return '<li><span class="kind">' + esc(n.kind) + '</span>' + esc(n.id) + "</li>";
  }).join("");

  edgeList.innerHTML = snap.edges.map(function (e) {
    return '<li><span class="edge-kind">' + esc(e.kind) + "</span>" +
      esc(e.from) + " &rarr; " + esc(e.to) + "</li>";
  }).join("");
}

scrubber.max = String(MOCK_TIMELINE.length - 1);
scrubber.addEventListener("input", function () {
  renderState(Number(scrubber.value));
});
renderState(0);

/* ---------- ask view ---------- */

var askForm = document.getElementById("ask-form");
var askInput = document.getElementById("ask-input");
var answerCard = document.getElementById("answer-card");

askForm.addEventListener("submit", function (ev) {
  ev.preventDefault();
  var q = askInput.value.trim();
  if (!q) { return; }

  answerCard.hidden = false;
  answerCard.innerHTML =
    '<span class="mock-tag">Mock answer</span>' +
    '<div class="verdict">' + esc(MOCK_ANSWER.verdict) + "</div>" +
    '<p class="summary">' + esc(MOCK_ANSWER.summary) + "</p>" +
    '<div class="proof-mini"><strong>Proof chain (preview)</strong><ul>' +
    MOCK_ANSWER.proof_chain.map(function (p) {
      return "<li>" + esc(p.title) + " — <em>" + esc(p.source) + "</em></li>";
    }).join("") +
    "</ul></div>";
});

/* ---------- proof view ---------- */

var chainList = document.getElementById("chain-list");

chainList.innerHTML = MOCK_CHAIN.map(function (step, i) {
  return "<li>" +
    '<div class="step-title">Step ' + (i + 1) + ": " + esc(step.title) + "</div>" +
    '<div class="evidence">' + esc(step.evidence) + "</div>" +
    '<div class="source">' + esc(step.source) + "</div>" +
    "</li>";
}).join("");
