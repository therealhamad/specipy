/* One event stream, two renderings. The toggle only changes which section is
   visible — both views are built from the same events, so they can never
   disagree about what happened. */

const STAGES = ["detected", "mapping", "fixing", "verifying", "reviewing"];

const STATUS_TEXT = {
  detected: ["Change detected", "running", "A provider change was picked up. Working out what it breaks."],
  verified: ["Verified & ready for review", "ok", "The fix passed the contract test. A pull request is waiting for you."],
  needs_human: ["Needs a human", "warn", "The agent could not verify a fix. Nothing has been claimed as working."],
  error: ["Run failed", "bad", "The pipeline itself failed. Nothing was changed."],
};

const STAGE_TEXT = {
  mapping: ["Finding the affected code", "running", "Reading the codebase to see what depends on the changed field."],
  fixing: ["Writing the fix", "running", "Editing the affected code."],
  verifying: ["Verifying the fix", "running", "Running the contract test the agent is not allowed to modify."],
  reviewing: ["Finishing up", "running", "Wrapping up and preparing the branch."],
};

const el = (id) => document.getElementById(id);
const state = { runId: null, source: null, snapshot: null, seen: new Set() };

/* ------------------------------ mode toggle ----------------------------- */

function setMode(mode) {
  const simple = mode === "simple";
  el("mode-simple").classList.toggle("active", simple);
  el("mode-technical").classList.toggle("active", !simple);
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-technical").setAttribute("aria-selected", String(!simple));
  if (state.runId) {
    el("simple").hidden = !simple;
    el("technical").hidden = simple;
  }
  localStorage.setItem("drift-mode", mode);
}

el("mode-simple").onclick = () => setMode("simple");
el("mode-technical").onclick = () => setMode("technical");

/* -------------------------------- helpers ------------------------------- */

const clock = (ts) =>
  new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString([], {
    hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

function metaList(node, rows) {
  node.innerHTML = "";
  for (const [key, value] of rows) {
    if (value === null || value === undefined || value === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    if (typeof value === "object" && value.href) {
      const a = document.createElement("a");
      a.href = value.href;
      a.textContent = value.text;
      a.target = "_blank";
      a.rel = "noreferrer";
      dd.appendChild(a);
    } else {
      dd.textContent = String(value);
    }
    node.append(dt, dd);
  }
}

function renderDiffText(node, text) {
  node.innerHTML = "";
  if (!text) {
    node.textContent = "Waiting for the agent…";
    return;
  }
  for (const line of text.split("\n")) {
    const span = document.createElement("span");
    if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) {
      span.className = "meta-line";
    } else if (line.startsWith("@@")) {
      span.className = "hunk";
    } else if (line.startsWith("+")) {
      span.className = "add";
    } else if (line.startsWith("-")) {
      span.className = "del";
    }
    span.textContent = line + "\n";
    node.appendChild(span);
  }
}

/* -------------------------------- rendering ----------------------------- */

function renderStatus(snap) {
  const terminal = STATUS_TEXT[snap.status];
  const staged = STAGE_TEXT[snap.stage];
  const [headline, tone, detail] =
    snap.status === "detected" && staged ? staged : terminal || STATUS_TEXT.detected;

  el("status-headline").textContent = headline;
  el("status-detail").textContent = snap.error && snap.status !== "verified" ? snap.error : detail;

  const chip = el("status-chip");
  chip.className = "chip " + tone;
  chip.textContent = { running: "in progress", ok: "verified", warn: "needs human", bad: "failed" }[tone];

  const panel = document.querySelector(".status-panel");
  panel.className = "panel status-panel " + (tone === "running" ? "" : tone);

  el("sim-badge").hidden = !snap.simulated;
}

function renderTimeline(snap) {
  const index = STAGES.indexOf(snap.stage);
  const stalled = snap.status === "needs_human" || snap.status === "error";
  document.querySelectorAll("#timeline li").forEach((li) => {
    const at = STAGES.indexOf(li.dataset.stage);
    li.classList.remove("active", "done", "blocked");
    if (at < index || (snap.status === "verified" && at <= index)) li.classList.add("done");
    else if (at === index) li.classList.add(stalled ? "blocked" : "active");
  });

  // The verify step is where a failure actually lands; say so plainly.
  const verify = document.querySelector('#timeline li[data-stage="verifying"] i');
  if (snap.attempts) {
    verify.textContent =
      snap.status === "verified"
        ? `Passed on ${snap.attempts === 1 ? "the first run" : `attempt ${snap.attempts}`}`
        : `${snap.attempts} attempt${snap.attempts === 1 ? "" : "s"}, not passing`;
  }
  const review = document.querySelector('#timeline li[data-stage="reviewing"] i');
  if (snap.status === "verified") review.textContent = `Branch ${snap.branch}`;
  else if (stalled) review.textContent = "Opened for review, marked needs-human";
}

function renderSimple(snap) {
  if (snap.root_cause || snap.fix) {
    el("plain-panel").hidden = false;
    el("plain-cause").textContent = snap.root_cause || "";
    el("plain-fix").textContent = snap.fix || "";
  }
  if (snap.spoken_summary) {
    el("briefing-panel").hidden = false;
    el("briefing-text").textContent = snap.spoken_summary;
    const audio = el("briefing-audio");
    if (snap.audio_url && audio.getAttribute("src") !== snap.audio_url) {
      audio.src = snap.audio_url;
    }
    audio.hidden = !snap.audio_url;
    el("briefing-source").textContent = snap.audio_url
      ? snap.audio_source === "cached"
        ? "Playing the pre-generated briefing."
        : ""
      : "Audio unavailable — the text above is the briefing.";
  }
}

function renderTechnical(snap) {
  metaList(el("tech-meta"), [
    ["run", snap.id],
    ["branch", snap.branch],
    ["commit", snap.commit ? snap.commit.slice(0, 10) : ""],
    ["provider", snap.provider_version],
    ["session", snap.session_id],
    ["console", snap.console_url ? { href: snap.console_url, text: "open in Claude Console" } : ""],
    ["mode", snap.simulated ? "simulated" : "live"],
  ]);

  metaList(el("tech-verify"), [
    ["status", snap.status],
    ["stage", snap.stage],
    ["test attempts", snap.attempts || 0],
    ["files changed", (snap.files_changed || []).join(", ")],
  ]);

  if (snap.test_output_tail) {
    el("tech-test-output").hidden = false;
    el("tech-test-output").textContent = snap.test_output_tail;
  }

  const drift = snap.drift || {};
  el("diff-tool").textContent = drift.tool ? `via ${drift.tool}` : "";

  const list = el("breaking-list");
  list.innerHTML = "";
  const changes = [
    ...(drift.breaking || []).map((c) => [c, false]),
    ...(drift.additive || []).map((c) => [c, true]),
  ];
  for (const [change, additive] of changes) {
    const card = document.createElement("div");
    card.className = "change" + (additive ? " additive" : "");
    const op = document.createElement("div");
    op.className = "op";
    op.textContent = `${change.operation || ""}  ${change.location || ""}`.trim();
    const what = document.createElement("p");
    what.className = "what";
    what.textContent = change.detail || change.kind;
    const doc = document.createElement("p");
    // Three-state: documented, explicitly undocumented, or never inspected.
    const described = (change.documentation || {}).described;
    doc.className = "doc" + (described === false ? " undocumented" : "");
    if (described === true) {
      doc.textContent = `Vendor description: ${change.documentation.description}`;
    } else if (described === false) {
      doc.textContent =
        "No vendor description — the agent had to infer this from types and usage.";
    } else {
      doc.textContent = "Documentation not inspected for this record.";
    }
    card.append(op, what, doc);

    if ((change.corroborated_by || []).length) {
      const agree = document.createElement("p");
      agree.className = "doc";
      agree.textContent = `Confirmed independently by oasdiff (${change.corroborated_by.join(", ")}).`;
      card.appendChild(agree);
    }
    list.appendChild(card);
  }

  el("raw-diff").textContent = JSON.stringify(drift, null, 2);
  renderDiffText(el("code-diff"), snap.code_diff);
}

function render(snap) {
  state.snapshot = snap;
  el("empty").hidden = true;
  const simple = el("mode-simple").classList.contains("active");
  el("simple").hidden = !simple;
  el("technical").hidden = simple;
  renderStatus(snap);
  renderTimeline(snap);
  renderSimple(snap);
  renderTechnical(snap);
}

/* ------------------------------- agent log ------------------------------ */

const LOG_BODY = {
  "drift.detected": (e) => e.summary,
  "session.created": (e) => e.session_id,
  "agent.message": (e) => (e.text || "").slice(0, 400),
  "agent.tool_use": (e) => `${e.tool} ${e.summary || ""}`.trim(),
  "agent.tool_result": (e) => (e.ok ? "ok" : "error"),
  "agent.thinking": () => "thinking",
  stage: (e) => `${e.stage} — ${e.message || ""}`,
  status: (e) => `${e.status} — ${e.message || ""}`,
  log: (e) => e.message,
  "session.error": (e) => e.message,
  "session.warning": (e) => e.message,
  "session.status": (e) => e.session_status,
  "voice.ready": (e) => `${e.audio_source} ${e.detail || ""}`.trim(),
  "voice.skipped": (e) => e.reason,
};

function appendLog(event) {
  const build = LOG_BODY[event.type];
  if (!build) return;
  const row = document.createElement("div");
  row.className = "log-row" + (event.type === "session.error" ? " err" : "");
  const time = document.createElement("time");
  time.textContent = clock(event.ts);
  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = event.type;
  const body = document.createElement("span");
  body.className = "body";
  body.textContent = build(event) || "";
  row.append(time, kind, body);
  const log = el("agent-log");
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

/* -------------------------------- streaming ----------------------------- */

function attach(runId) {
  if (state.source) state.source.close();
  state.runId = runId;
  state.seen.clear();
  el("agent-log").innerHTML = "";
  history.replaceState(null, "", `/?run=${runId}`);

  const source = new EventSource(`/events/${runId}`);
  state.source = source;

  const onEvent = (raw) => {
    let event;
    try {
      event = JSON.parse(raw.data);
    } catch {
      return;
    }
    if (event.type === "snapshot") {
      render(event);
      return;
    }
    if (event.type === "done") {
      source.close();
      fetch(`/runs/${runId}`).then((r) => r.json()).then(render).catch(() => {});
      return;
    }
    if (event.seq && state.seen.has(event.seq)) return;
    if (event.seq) state.seen.add(event.seq);
    appendLog(event);
    if (state.snapshot) {
      // Keep the headline moving between snapshots.
      if (event.stage) state.snapshot.stage = event.stage;
      if (event.status) state.snapshot.status = event.status;
      renderStatus(state.snapshot);
      renderTimeline(state.snapshot);
    }
  };

  for (const type of Object.keys(LOG_BODY).concat(["snapshot", "done"])) {
    source.addEventListener(type, onEvent);
  }
  source.onmessage = onEvent;
  source.onerror = () => {
    // Browser reconnects on its own; history replay makes that lossless.
  };
}

/* --------------------------------- boot -------------------------------- */

el("start-demo").onclick = async () => {
  const button = el("start-demo");
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    const response = await fetch("/demo/drift", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "failed to start");
    attach(body.run_id);
  } catch (error) {
    el("empty-hint").textContent = String(error.message || error);
    button.disabled = false;
    button.textContent = "Run from sample diff";
  }
};

async function boot() {
  setMode(localStorage.getItem("drift-mode") || "simple");

  const wanted = new URLSearchParams(location.search).get("run");
  const [runs, health] = await Promise.all([
    fetch("/runs").then((r) => r.json()).catch(() => ({ runs: [] })),
    fetch("/health").then((r) => r.json()).catch(() => null),
  ]);

  if (health && health.live_blockers && health.live_blockers.length && !health.demo_mode) {
    el("empty-hint").textContent =
      "Live runs need: " + health.live_blockers.join(", ") + ". Set DEMO_MODE=1 for a simulated run.";
  }

  const target = wanted || (runs.runs[0] && runs.runs[0].id);
  if (target) attach(target);
}

boot();
