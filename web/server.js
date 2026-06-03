// stackweft web tier — dependency-free Node http server (no express).
// Three real layers: this Node backend ⇄ the Python AI orchestrator (stackweft.cli)
// ⇄ a browser chat UI. The backend spawns the orchestrator and exposes its
// SQLite-backed state as JSON for live polling.
const http = require("http");
const { spawn, execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const PORT = process.env.STACKWEFT_WEB_PORT || 7878;
const HOME = path.resolve(__dirname, "..");   // repo root (contains the stackweft package)
const REPO = process.env.STACKWEFT_DEMO_REPO || path.join(HOME, "experiments/conduit");
const SCAFFOLD = process.env.STACKWEFT_DEMO_BASE || "58634fd";
const PYENV = { ...process.env, PYTHONPATH: HOME, PYTHONUNBUFFERED: "1" };

function py(args, cb) {
  execFile("python3", ["-m", "stackweft.cli", ...args],
    { cwd: HOME, env: PYENV, maxBuffer: 8 << 20 },
    (err, stdout, stderr) => cb(err, stdout, stderr));
}

function send(res, code, type, body) {
  res.writeHead(code, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(body);
}

function startRun(requirement, cb) {
  // Reset the target repo to the clean scaffold so each delivery starts fresh.
  execFile("bash", ["-c",
    `cd ${REPO} && git checkout ${SCAFFOLD} -q && git reset --hard ${SCAFFOLD} -q && ` +
    `git clean -fdq frontend backend && git checkout backend/test backend/vitest.config.js frontend/vitest.config.js 2>/dev/null; true`],
    () => {
      // Target the demo repo explicitly (cwd is StackWeft's own repo, which the engine
      // refuses to deliver into). --ask: pause after clarify if ambiguous (PM loop).
      const p = spawn("python3", ["-m", "stackweft.cli", "run", requirement, "--repo", REPO, "--ask"],
        { cwd: HOME, env: PYENV, detached: true, stdio: ["ignore", "pipe", "pipe"] });
      let buf = "", done = false;
      p.stdout.on("data", (d) => {
        buf += d.toString();
        const m = buf.match(/run_id=([0-9a-f]+)/);
        if (m && !done) { done = true; cb(null, m[1]); }
      });
      p.on("error", (e) => { if (!done) { done = true; cb(e); } });
      setTimeout(() => { if (!done) { done = true; cb(new Error("no run_id within 20s")); } }, 20000);
      p.unref();
    });
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  if (req.method === "GET" && u.pathname === "/") {
    return send(res, 200, "text/html; charset=utf-8",
      fs.readFileSync(path.join(__dirname, "index.html")));
  }
  if (req.method === "GET" && u.pathname.startsWith("/assets/")) {
    const f = path.join(__dirname, "assets", path.basename(u.pathname));
    if (fs.existsSync(f)) {
      const ext = path.extname(f).toLowerCase();
      const mime = { ".svg": "image/svg+xml", ".png": "image/png",
        ".jpg": "image/jpeg", ".ico": "image/x-icon", ".md": "text/markdown",
        ".js": "application/javascript; charset=utf-8" }[ext] || "application/octet-stream";
      return send(res, 200, mime, fs.readFileSync(f));
    }
    return send(res, 404, "text/plain", "asset not found");
  }
  if (req.method === "POST" && u.pathname === "/api/run") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let requirement = "", attachments = [];
      try { const j = JSON.parse(body); requirement = j.requirement || ""; attachments = j.attachments || []; } catch {}
      if (!requirement && !attachments.length) return send(res, 400, "application/json", '{"error":"empty"}');
      // Multimodal bundle: text-extractable attachments fold into the requirement
      // context; media (image/audio/video) are listed in a manifest (true extraction
      // = future Evidence-Graph work; we don't fake understanding).
      const textParts = [], media = [];
      for (const a of attachments) {
        if (a.text) textParts.push(`[附件 ${a.name}]\n${String(a.text).slice(0, 4000)}`);
        else media.push(`${a.name} (${a.type || "?"}, ${a.size || 0}B)`);
      }
      let full = requirement;
      if (textParts.length) full += "\n\n=== 附件内容（已纳入需求理解） ===\n" + textParts.join("\n\n");
      if (media.length) full += "\n\n=== 多模态附件（manifest，待抽取） ===\n- " + media.join("\n- ");
      startRun(full, (err, rid) =>
        err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
            : send(res, 200, "application/json", JSON.stringify({ run_id: rid })));
    });
    return;
  }
  if (req.method === "POST" && u.pathname === "/api/clarify") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let id = "", answer = "";
      try { const j = JSON.parse(body); id = j.run_id || ""; answer = j.answer || ""; } catch {}
      if (!id || !answer) return send(res, 400, "application/json", '{"error":"need run_id+answer"}');
      // resume the paused run with the PM's answer (runs to completion in the background)
      const p = spawn("python3", ["-m", "stackweft.cli", "clarify-answer", id, answer],
        { cwd: HOME, env: PYENV, detached: true, stdio: "ignore" });
      p.unref();
      send(res, 200, "application/json", JSON.stringify({ ok: true, run_id: id }));
    });
    return;
  }
  if (req.method === "POST" && u.pathname === "/api/control") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let id = "", action = "", text = "";
      try { const j = JSON.parse(body); id = j.run_id || ""; action = j.action || ""; text = j.text || ""; } catch {}
      const ACTIONS = ["pause", "abort", "append", "resume", "approve", "deny", "mode"];
      if (!ACTIONS.includes(action) || (action !== "mode" && !id))
        return send(res, 400, "application/json", '{"error":"need run_id + valid action"}');
      // approve/deny resolve a pending gate; mode sets the global approval mode (no run id);
      // pause/abort/append set a flag (instant); resume re-runs the pipeline (long → detached).
      const args = ["-m", "stackweft.cli", "control", id || "_", action]; if (text) args.push(text);
      const p = spawn("python3", args, { cwd: HOME, env: PYENV, detached: true, stdio: "ignore" });
      p.unref();
      send(res, 200, "application/json", JSON.stringify({ ok: true, action }));
    });
    return;
  }
  if (req.method === "GET" && u.pathname === "/api/onebot") {  // OneBot capability claim
    return py(["onebot", "--caps"], (err, out) =>
      send(res, err ? 500 : 200, "application/json", err ? JSON.stringify({ error: String(err) }) : (out || "{}")));
  }
  if (req.method === "POST" && u.pathname === "/api/onebot") {  // OneBot v11 message event in → reply
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      py(["onebot", "--event", body || "{}"], (err, out) =>
        send(res, err ? 500 : 200, "application/json", err ? JSON.stringify({ error: String(err) }) : (out || "{}")));
    });
    return;
  }
  if (req.method === "GET" && u.pathname === "/api/skill-changes") {
    return py(["skill-changes"], (err, out) =>
      err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
          : send(res, 200, "application/json", out || "[]"));
  }
  if (req.method === "GET" && u.pathname === "/api/skill-log") {
    return py(["skill-changelog"], (err, out) =>
      err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
          : send(res, 200, "application/json", out || "[]"));
  }
  if (req.method === "POST" && u.pathname === "/api/skill") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let action = "", change_id = "", brief = "", name = "", version = 0;
      try { const j = JSON.parse(body); action = j.action || ""; change_id = j.change_id || ""; brief = j.brief || ""; name = j.name || ""; version = j.version || 0; } catch {}
      // approve/veto are instant; request drafts via LLM (slow) → detached
      if (action === "approve" || action === "veto") {
        return py(["skill-" + action, change_id], (err, out) =>
          err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
              : send(res, 200, "application/json", out || "{}"));
      }
      if (action === "rollback" && name && version) {
        return py(["skill-rollback", name, String(version)], (err, out) =>
          err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
              : send(res, 200, "application/json", out || "{}"));
      }
      if (action === "request" && brief) {
        const p = spawn("python3", ["-m", "stackweft.cli", "skill-request", brief],
          { cwd: HOME, env: PYENV, detached: true, stdio: "ignore" });
        p.unref();
        return send(res, 200, "application/json", JSON.stringify({ ok: true, queued: true }));
      }
      return send(res, 400, "application/json", '{"error":"need action approve|veto|request"}');
    });
    return;
  }
  if (req.method === "POST" && u.pathname === "/api/clarify-ask") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let id = "", question = "";
      try { const j = JSON.parse(body); id = j.run_id || ""; question = j.question || ""; } catch {}
      if (!id || !question) return send(res, 400, "application/json", '{"error":"need run_id+question"}');
      py(["clarify-ask", id, question], (err, out) =>  // {"answer":...}; does NOT submit the point
        err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
            : send(res, 200, "application/json", out || "{}"));
    });
    return;
  }
  if (req.method === "GET" && u.pathname === "/api/debug") {
    return py(["json", "--debug", u.searchParams.get("id") || ""], (err, out) =>
      err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
          : send(res, 200, "application/json", out || "{}"));
  }
  if (req.method === "GET" && u.pathname === "/api/status") {
    return py(["json", u.searchParams.get("id") || ""], (err, out) =>
      err ? send(res, 500, "application/json", JSON.stringify({ error: String(err) }))
          : send(res, 200, "application/json", out || "{}"));
  }
  if (req.method === "GET" && u.pathname === "/learn") {
    const tmp = `/tmp/stackweft_learn_${Date.now()}.html`;
    return py(["learn", "--out", tmp], (err) =>
      err ? send(res, 500, "text/html", "<p>learn error</p>")
          : send(res, 200, "text/html; charset=utf-8", fs.readFileSync(tmp)));
  }
  if (req.method === "GET" && u.pathname === "/api/report") {
    const tmp = `/tmp/stackweft_report_${Date.now()}.html`;
    return py(["viz", u.searchParams.get("id") || "", "--out", tmp], (err) =>
      err ? send(res, 500, "text/html", "<p>report error</p>")
          : send(res, 200, "text/html; charset=utf-8", fs.readFileSync(tmp)));
  }
  send(res, 404, "text/plain", "not found");
});

server.listen(PORT, () => console.log(`stackweft web on http://localhost:${PORT}`));
