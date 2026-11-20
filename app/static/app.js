"use strict";
// Plain-JS frontend for the transcriber API (see README "API").
// All job data is rendered with textContent only.

(function () {
  const ACTIVE = ["queued", "downloading", "extracting", "transcribing", "cancelling"];
  const STATUS_LABEL = {
    queued: "Waiting",
    downloading: "Downloading",
    extracting: "Preparing audio",
    transcribing: "Transcribing",
    cancelling: "Stopping…",
    done: "Done",
    error: "Failed",
    cancelled: "Cancelled",
  };
  const FAST_POLL_MS = 2000;
  const SLOW_POLL_MS = 15000;

  const $ = (id) => document.getElementById(id);
  const urlForm = $("url-form");
  const urlInput = $("url-input");
  const urlSubmit = $("url-submit");
  const drop = $("drop");
  const fileInput = $("file-input");
  const uploadStatus = $("upload-status");
  const uploadLabel = $("upload-label");
  const uploadProgress = $("upload-progress");
  const formMessage = $("form-message");
  const jobsList = $("jobs");
  const jobsEmpty = $("jobs-empty");
  const card = $("transcript-card");
  const meta = $("transcript-meta");
  const textArea = $("transcript");
  const copyBtn = $("copy-btn");
  const dlTxt = $("dl-txt");
  const dlSrt = $("dl-srt");
  const stopBtn = $("stop-btn");
  const deleteBtn = $("delete-btn");
  const transcriptStatus = $("transcript-status");

  let jobs = [];
  let selectedId = null;
  let loadedTranscriptFor = null;
  let pollTimer = null;
  let uploading = false;

  function setMessage(el, text, kind) {
    el.textContent = text || "";
    el.className = "message" + (kind ? " " + kind : "");
  }

  async function apiError(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === "string") return body.detail;
    } catch (e) { /* not JSON */ }
    return "Request failed (" + res.status + ")";
  }

  function formatDuration(sec) {
    if (!sec && sec !== 0) return "";
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0");
    return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
  }

  function formatTime(epoch) {
    if (!epoch) return "";
    return new Date(epoch * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function statusText(job) {
    let text = STATUS_LABEL[job.status] || job.status;
    if (job.status === "queued" && job.queue_position > 0) {
      text += " (position " + job.queue_position + ")";
    } else if (job.status === "downloading" || job.status === "transcribing") {
      text += " " + Math.round(job.progress) + "%";
    } else if (job.status === "error" && job.error) {
      text += ": " + job.error;
    }
    return text;
  }

  // ---- jobs list ---------------------------------------------------------

  function renderJobs() {
    jobsList.replaceChildren();
    jobsEmpty.hidden = jobs.length > 0;
    for (const job of jobs) {
      const li = document.createElement("li");
      li.className = "job" + (job.id === selectedId ? " selected" : "");
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      li.setAttribute("aria-pressed", job.id === selectedId ? "true" : "false");

      const name = document.createElement("div");
      name.className = "job-name";
      name.textContent = job.name;
      li.appendChild(name);

      const line = document.createElement("div");
      line.className = "job-meta";
      const status = document.createElement("span");
      status.className = "job-status " + job.status;
      status.textContent = statusText(job);
      line.appendChild(status);
      const extra = [formatTime(job.created)];
      if (job.duration) extra.push(formatDuration(job.duration));
      if (job.language) extra.push(job.language);
      line.appendChild(document.createTextNode(" · " + extra.join(" · ")));
      li.appendChild(line);

      if (ACTIVE.includes(job.status)) {
        const bar = document.createElement("progress");
        bar.max = 100;
        // Indeterminate while waiting or converting.
        if (job.status === "downloading" || job.status === "transcribing") bar.value = job.progress;
        else bar.removeAttribute("value");
        li.appendChild(bar);
      }

      li.addEventListener("click", () => selectJob(job.id));
      li.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectJob(job.id); }
      });
      jobsList.appendChild(li);
    }
  }

  function schedulePoll(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshJobs, ms);
  }

  async function refreshJobs() {
    let delay = SLOW_POLL_MS;
    try {
      const res = await fetch("/api/jobs", { cache: "no-store" });
      if (!res.ok) throw new Error(await apiError(res));
      jobs = await res.json();
      renderJobs();
      if (jobs.some((j) => ACTIVE.includes(j.status))) delay = FAST_POLL_MS;
      if (selectedId) {
        const job = jobs.find((j) => j.id === selectedId);
        if (!job) clearSelection();
        else renderSelected(job);
      }
    } catch (e) {
      delay = FAST_POLL_MS * 2;
    }
    schedulePoll(delay);
  }

  // ---- transcript pane ---------------------------------------------------

  function clearSelection() {
    selectedId = null;
    loadedTranscriptFor = null;
    card.hidden = true;
    textArea.value = "";
    renderJobs();
  }

  function selectJob(id) {
    if (selectedId !== id) {
      selectedId = id;
      loadedTranscriptFor = null;
      textArea.value = "";
      setMessage(transcriptStatus, "");
    }
    renderJobs();
    const job = jobs.find((j) => j.id === id);
    if (job) renderSelected(job);
    card.hidden = false;
  }

  function setLink(a, href) {
    if (href) {
      a.href = href;
      a.removeAttribute("aria-disabled");
      a.tabIndex = 0;
    } else {
      a.removeAttribute("href");
      a.setAttribute("aria-disabled", "true");
      a.tabIndex = -1;
    }
  }

  async function renderSelected(job) {
    const done = job.status === "done";
    const parts = [job.name];
    if (job.duration) parts.push(formatDuration(job.duration));
    if (job.language) parts.push("language: " + job.language);
    meta.textContent = parts.join(" · ");
    copyBtn.disabled = !done;
    deleteBtn.disabled = job.status !== "queued" &&
      !(done || job.status === "error" || job.status === "cancelled");
    stopBtn.hidden = !ACTIVE.includes(job.status);
    stopBtn.disabled = job.status === "cancelling";
    stopBtn.textContent = job.status === "cancelling" ? "Stopping…" : "Stop";
    const base = "/api/jobs/" + encodeURIComponent(job.id);
    setLink(dlTxt, done ? base + "/transcript.txt" : null);
    setLink(dlSrt, done ? base + "/transcript.srt" : null);

    if (!done) {
      textArea.value = "";
      setMessage(transcriptStatus, statusText(job), job.status === "error" ? "error" : "");
      return;
    }
    if (loadedTranscriptFor === job.id) return;
    loadedTranscriptFor = job.id;
    setMessage(transcriptStatus, "Loading transcript…");
    try {
      const res = await fetch(base, { cache: "no-store" });
      if (!res.ok) throw new Error(await apiError(res));
      const full = await res.json();
      if (selectedId !== job.id) return;
      textArea.value = full.transcript || "";
      setMessage(transcriptStatus, full.transcript ? "" : "No speech was detected.");
    } catch (e) {
      loadedTranscriptFor = null;
      setMessage(transcriptStatus, e.message, "error");
    }
  }

  function fallbackCopy() {
    textArea.focus();
    textArea.select();
    textArea.setSelectionRange(0, textArea.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    return ok;
  }

  copyBtn.addEventListener("click", async () => {
    let ok = false;
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(textArea.value); ok = true; } catch (e) { ok = false; }
    }
    if (!ok) ok = fallbackCopy();
    if (ok) {
      copyBtn.textContent = "Copied";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
    } else {
      setMessage(transcriptStatus, "Copy failed. The text is selected; press Ctrl+C / ⌘C.", "error");
      textArea.select();
    }
  });

  deleteBtn.addEventListener("click", async () => {
    if (!selectedId) return;
    if (!window.confirm("Delete this job and its transcript?")) return;
    const id = selectedId;
    try {
      const res = await fetch("/api/jobs/" + encodeURIComponent(id), { method: "DELETE" });
      if (!res.ok && res.status !== 404) throw new Error(await apiError(res));
      jobs = jobs.filter((j) => j.id !== id);
      clearSelection();
    } catch (e) {
      setMessage(transcriptStatus, e.message, "error");
    }
    refreshJobs();
  });

  stopBtn.addEventListener("click", async () => {
    const job = jobs.find((j) => j.id === selectedId);
    if (!job || !ACTIVE.includes(job.status) || job.status === "cancelling") return;
    if (job.status !== "queued" && !window.confirm("Stop this transcription?")) return;
    stopBtn.disabled = true;
    try {
      const res = await fetch("/api/jobs/" + encodeURIComponent(job.id) + "/cancel", { method: "POST" });
      // 409: it finished meanwhile; the refresh shows the final state.
      if (!res.ok && res.status !== 409) throw new Error(await apiError(res));
    } catch (e) {
      setMessage(transcriptStatus, e.message, "error");
    }
    refreshJobs();
  });

  // ---- submitting --------------------------------------------------------

  urlForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;
    urlSubmit.disabled = true;
    setMessage(formMessage, "Checking link…");
    try {
      const res = await fetch("/api/jobs/url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      });
      if (!res.ok) throw new Error(await apiError(res));
      const body = await res.json();
      urlInput.value = "";
      setMessage(formMessage, "Added to the queue.", "ok");
      selectedId = body.id;
      loadedTranscriptFor = null;
      card.hidden = false;
      await refreshJobs();
    } catch (e) {
      setMessage(formMessage, e.message, "error");
    } finally {
      urlSubmit.disabled = false;
    }
  });

  function uploadFile(file) {
    if (!file || uploading) return;
    uploading = true;
    const form = new FormData();
    form.append("file", file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs/upload");
    uploadStatus.hidden = false;
    uploadLabel.textContent = "Uploading " + file.name;
    uploadProgress.value = 0;
    setMessage(formMessage, "");

    xhr.upload.addEventListener("progress", (ev) => {
      if (ev.lengthComputable) uploadProgress.value = (ev.loaded / ev.total) * 100;
    });
    xhr.addEventListener("load", () => {
      uploading = false;
      uploadStatus.hidden = true;
      fileInput.value = "";
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { body = null; }
      if (xhr.status === 202 && body && body.id) {
        setMessage(formMessage, "Uploaded and added to the queue.", "ok");
        selectedId = body.id;
        loadedTranscriptFor = null;
        card.hidden = false;
        refreshJobs();
      } else {
        const detail = body && typeof body.detail === "string" ? body.detail : "Upload failed (" + xhr.status + ")";
        setMessage(formMessage, detail, "error");
      }
    });
    const failed = () => {
      uploading = false;
      uploadStatus.hidden = true;
      fileInput.value = "";
      setMessage(formMessage, "Upload failed. Check the connection and try again.", "error");
    };
    xhr.addEventListener("error", failed);
    xhr.addEventListener("abort", failed);
    xhr.send(form);
  }

  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) uploadFile(fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((type) =>
    drop.addEventListener(type, (ev) => {
      ev.preventDefault();
      drop.classList.add("over");
    }));
  ["dragleave", "drop"].forEach((type) =>
    drop.addEventListener(type, (ev) => {
      ev.preventDefault();
      drop.classList.remove("over");
    }));
  drop.addEventListener("drop", (ev) => {
    const files = ev.dataTransfer && ev.dataTransfer.files;
    if (files && files[0]) uploadFile(files[0]);
  });
  // Dropping a file elsewhere on the page should not navigate away.
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("drop", (ev) => ev.preventDefault());

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshJobs();
  });

  refreshJobs();
})();
