const urlInput = document.getElementById("video-url");
const clearButton = document.getElementById("clear-btn");
const prepareButton = document.getElementById("prepare-btn");
const statusText = document.getElementById("status");
const formatButtons = [...document.querySelectorAll(".format-option")];

// عناصر شريط التقدم
const progressBox = document.getElementById("progress");
const progressBar = document.getElementById("progress-bar");
const progressFill = document.getElementById("progress-fill");
const progressLabel = document.getElementById("progress-label");
const progressPercent = document.getElementById("progress-percent");
const progressSpeed = document.getElementById("progress-speed");
const progressEta = document.getElementById("progress-eta");

// عنوان الـ Backend. دلوقتي على جهازك إنت، وبعدين هنغيره لما نستضيفه.
const API_BASE = "http://127.0.0.1:5000";
const POLL_INTERVAL_MS = 800;
const MAX_POLL_FAILURES = 5;

let selectedFormat = "mp4";

formatButtons.forEach((button) => {
  button.addEventListener("click", () => {
    selectedFormat = button.dataset.format;
    formatButtons.forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    statusText.textContent = `Selected format: ${selectedFormat.toUpperCase()}`;
  });
});

clearButton.addEventListener("click", () => {
  urlInput.value = "";
  statusText.textContent = "";
  urlInput.focus();
});

// ---------- دوال مساعدة لشريط التقدم ----------

function formatTime(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = s % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

function formatSpeed(bytesPerSecond) {
  const mb = bytesPerSecond / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB/s` : `${Math.round(bytesPerSecond / 1024)} KB/s`;
}

// mode: "idle" | "busy" (شريط بيتحرك من غير نسبة) | "progress" | "finished" | "failed"
function setProgress({ mode, label, percent = 0, speed = "", eta = "" }) {
  progressBox.hidden = mode === "idle";
  progressBox.classList.toggle("busy", mode === "busy");
  progressBox.classList.toggle("finished", mode === "finished");
  progressBox.classList.toggle("failed", mode === "failed");

  progressLabel.textContent = label || "";
  progressPercent.textContent = mode === "progress" || mode === "finished" ? `${Math.floor(percent)}%` : "";
  progressFill.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  progressBar.setAttribute("aria-valuenow", String(Math.floor(percent)));
  progressSpeed.textContent = speed;
  progressEta.textContent = eta;
}

function finishJob() {
  prepareButton.disabled = false;
}

function triggerFileDownload(jobId, filename) {
  const link = document.createElement("a");
  link.href = `${API_BASE}/api/file/${jobId}`;
  link.download = filename || "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

// ---------- متابعة الشغلانة ----------

async function pollJob(jobId) {
  let failures = 0;

  while (true) {
    let job;
    try {
      const response = await fetch(`${API_BASE}/api/progress/${jobId}`);
      if (response.status === 404) {
        throw new Error("expired");
      }
      if (!response.ok) {
        throw new Error(`status ${response.status}`);
      }
      job = await response.json();
      failures = 0;
    } catch (error) {
      failures += 1;
      if (error.message === "expired" || failures >= MAX_POLL_FAILURES) {
        setProgress({ mode: "failed", label: "Connection lost", percent: 100 });
        statusText.textContent = "Lost connection to the server. Make sure the backend is running, then try again.";
        finishJob();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      continue;
    }

    if (job.status === "starting") {
      setProgress({ mode: "busy", label: "Checking the video..." });
    } else if (job.status === "downloading") {
      setProgress({
        mode: "progress",
        label: "Downloading...",
        percent: job.percent || 0,
        speed: job.speed ? formatSpeed(job.speed) : "",
        eta: job.eta != null ? `${formatTime(job.eta)} left` : "",
      });
    } else if (job.status === "processing") {
      setProgress({ mode: "busy", label: selectedFormat === "mp3" ? "Converting to MP3..." : "Finalizing the video..." });
    } else if (job.status === "done") {
      setProgress({ mode: "finished", label: "Done!", percent: 100 });
      statusText.textContent = `Your file is downloading: ${job.filename}`;
      triggerFileDownload(jobId, job.filename);
      finishJob();
      return;
    } else if (job.status === "error") {
      setProgress({ mode: "failed", label: "Failed", percent: 100 });
      statusText.textContent = job.error || "Something went wrong. Please try again.";
      finishJob();
      return;
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

// ---------- زر Prepare download ----------

prepareButton.addEventListener("click", async () => {
  const raw = urlInput.value.trim();

  if (!raw) {
    statusText.textContent = "Please paste a link first.";
    urlInput.focus();
    return;
  }

  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    statusText.textContent = "That link doesn't look valid. Please check it and try again.";
    return;
  }

  if (!["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].includes(parsed.hostname)) {
    statusText.textContent = "Please enter a YouTube link.";
    return;
  }

  prepareButton.disabled = true;
  statusText.textContent = "";
  setProgress({ mode: "busy", label: "Starting..." });

  try {
    const response = await fetch(`${API_BASE}/api/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: raw, format: selectedFormat }),
    });

    if (!response.ok) {
      let message = `Server error (${response.status}).`;
      try {
        const data = await response.json();
        if (data.error) message = data.error;
      } catch {}
      setProgress({ mode: "idle" });
      statusText.textContent = message;
      finishJob();
      return;
    }

    const { job_id: jobId } = await response.json();
    await pollJob(jobId);
  } catch (error) {
    setProgress({ mode: "idle" });
    statusText.textContent = "Could not reach the server. Make sure the backend is running, then try again.";
    console.error(error);
    finishJob();
  }
});
