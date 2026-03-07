# ── HTML Dashboard ──
# Frontend HTML/CSS/JS for the Claude Code WebUI dashboard.
# Extracted from server.py for separation of concerns.


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Sessions</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
    zoom: 1.2;
  }
  .header {
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
    border-bottom: 1px solid #2a2a4a;
  }
  .header h1 {
    font-size: 18px;
    color: #a78bfa;
  }
  .header .status {
    font-size: 12px;
    color: #888;
    margin-left: auto;
  }
  .header .back-btn {
    background: #2a2a4a;
    color: #a78bfa;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    display: none;
  }
  .header .back-btn:hover { background: #3a3a5a; }
  .container { padding: 16px 24px; }

  /* ── Dashboard view ── */
  .dashboard { }
  .empty {
    text-align: center;
    color: #555;
    margin-top: 80px;
    font-size: 16px;
  }
  .empty .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    background: #4ade80;
    border-radius: 50%;
    margin-right: 8px;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 1; }
  }
  .session-card {
    background: #16213e;
    border: 1px solid #2a2a4a;
    border-left: 4px solid hsl(var(--sh,220),60%,50%);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 12px;
    cursor: pointer;
    transition: all 0.2s;
    animation: slideIn 0.3s ease;
  }
  .session-card:hover { border-color: #a78bfa55; background: #1a2744; border-left-color: hsl(var(--sh,220),60%,50%); }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .sc-top {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 2px;
  }
  .sc-sid-row {
    margin-bottom: 6px;
  }
  .state-badge {
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .badge-idle { background: hsl(var(--sh,220),60%,50%,0.13); color: hsl(var(--sh,220),60%,60%); }
  .badge-busy { background: hsl(var(--sh,220),60%,50%,0.13); color: hsl(var(--sh,220),60%,60%); }
  .badge-permission_prompt { background: #ef444422; color: #ef4444; }
  .badge-elicitation { background: #22d3ee22; color: #22d3ee; }
  .badge-plan_review { background: #a78bfa22; color: #a78bfa; }
  .sc-project {
    font-weight: 700;
    font-size: 14px;
    color: #e0e0e0;
  }
  .sc-sid {
    font-size: 12px;
    color: #888;
  }
  .sc-user-prompt {
    font-size: 12px;
    color: #888;
    line-height: 1.4;
    margin-bottom: 4px;
  }
  .sc-user-prompt::before {
    content: '> ';
    color: #888;
  }
  .sc-summary {
    font-size: 12px;
    color: #e0e0e0;
    line-height: 1.5;
    white-space: pre-wrap;
  }
  .sc-time {
    font-size: 12px;
    color: #888;
    margin-left: auto;
  }
  .sc-actions {
    margin-top: 10px;
    display: flex;
    gap: 8px;
  }
  .attention-count {
    background: #ef4444;
    color: white;
    border-radius: 50%;
    width: 22px;
    height: 22px;
    font-size: 12px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-left: 8px;
  }

  /* ── Session detail view ── */
  .session-detail { display: none; }
  .transcript-view {
    max-height: calc(100vh - 280px);
    overflow-y: auto;
    margin-bottom: 16px;
    padding-right: 8px;
  }
  .msg {
    margin-bottom: 12px;
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .msg-user {
    background: #1e3a5f;
    border-left: 3px solid #3b82f6;
  }
  .msg-assistant {
    background: #1a2744;
    border-left: 3px solid #a78bfa;
  }
  .msg-tool {
    background: #0f0f23;
    border-left: 3px solid #f97316;
    font-size: 12px;
  }
  .msg-label {
    font-size: 11px;
    font-weight: 700;
    margin-bottom: 4px;
    text-transform: uppercase;
  }
  .msg-user .msg-label { color: #3b82f6; }
  .msg-assistant .msg-label { color: #a78bfa; }
  .msg-tool .msg-label { color: #f97316; }
  .msg-content {
    overflow: hidden;
    position: relative;
  }
  .msg-content.collapsed { max-height: 200px; }
  .msg-content.collapsed::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 40px;
    background: linear-gradient(transparent, #1a2744);
    pointer-events: none;
  }
  .msg-user .msg-content.collapsed::after {
    background: linear-gradient(transparent, #1e3a5f);
  }
  .msg-tool .msg-content.collapsed::after {
    background: linear-gradient(transparent, #0f0f23);
  }
  .msg-toggle {
    background: none;
    border: none;
    color: #a78bfa;
    font-size: 12px;
    cursor: pointer;
    padding: 2px 0;
    font-weight: 600;
  }

  /* ── Permission card in detail view ── */
  .perm-card {
    background: #16213e;
    border: 1px solid #ef444444;
    border-left: 4px solid #ef4444;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 16px;
  }
  .perm-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .perm-tool {
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
  }
  .perm-tool-bash { background: #ef444422; color: #ef4444; }
  .perm-tool-write { background: #f9731622; color: #f97316; }
  .perm-tool-plan { background: #a78bfa22; color: #a78bfa; }
  .perm-tool-question { background: #22d3ee22; color: #22d3ee; }
  .perm-tool-web { background: #3b82f622; color: #3b82f6; }
  .perm-tool-other { background: #66666622; color: #999; }
  .perm-detail {
    background: #0f0f23;
    border-radius: 8px;
    padding: 12px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
    margin-bottom: 12px;
    max-height: 300px;
    overflow: auto;
  }
  .perm-sub {
    color: #aaa;
    font-size: 12px;
    margin-bottom: 12px;
  }
  .allow-info {
    font-size: 12px;
    color: #888;
    margin-bottom: 12px;
  }
  .allow-info code {
    color: #facc15;
    background: #facc1511;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .path-select-area { margin-bottom: 12px; }
  .path-option {
    background: #0f0f23;
    border: 2px solid #2a2a4a;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 6px;
    cursor: pointer;
    transition: all 0.15s;
    font-size: 13px;
    font-family: monospace;
  }
  .path-option:hover { border-color: #f59e0b55; background: #16213e; }
  .path-option .path-label { color: #f59e0b; font-weight: 600; }
  .path-option .path-pattern { color: #888; font-size: 11px; margin-top: 2px; }

  /* ── Question card styles ── */
  .q-option {
    background: #0f0f23;
    border: 2px solid #2a2a4a;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .q-option:hover { border-color: #22d3ee55; background: #16213e; }
  .q-option.selected { border-color: #22d3ee; background: #22d3ee0d; }
  .q-option .q-check {
    width: 20px; height: 20px;
    border: 2px solid #444;
    border-radius: 4px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    margin-top: 1px;
    transition: all 0.15s;
  }
  .q-option.selected .q-check {
    border-color: #22d3ee;
    background: #22d3ee;
    color: #0f0f23;
  }
  .q-option .q-label { color: #e0e0e0; font-weight: 600; font-size: 14px; }
  .q-option .q-desc { color: #888; font-size: 12px; margin-top: 2px; }
  .q-custom-area { margin-top: 10px; }
  .q-custom-toggle {
    background: none;
    border: 1px dashed #444;
    border-radius: 8px;
    color: #888;
    padding: 10px 14px;
    width: 100%;
    text-align: left;
    font-size: 13px;
    cursor: pointer;
  }
  .q-custom-toggle:hover { border-color: #22d3ee55; color: #aaa; }
  .q-custom-input {
    width: 100%;
    background: #0f0f23;
    border: 2px solid #22d3ee55;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 60px;
  }
  .q-custom-input:focus { outline: none; border-color: #22d3ee; }

  /* Plan feedback */
  .plan-feedback-input {
    width: 100%;
    background: #0f0f23;
    border: 1px solid #2a2a4a;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 60px;
  }
  .plan-feedback-input:focus { outline: none; border-color: #a78bfa; }

  /* ── Prompt input area ── */
  .prompt-area {
    border-top: 1px solid #2a2a4a;
    padding: 16px 24px;
    background: #16213e;
  }
  .image-upload-area {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .btn-upload-image {
    background: #1e293b;
    border: 1px dashed #444;
    color: #a78bfa;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    white-space: nowrap;
  }
  .btn-upload-image:hover { border-color: #a78bfa; }
  .image-preview-area {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .image-thumb {
    position: relative;
    width: 60px;
    height: 60px;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid #333;
  }
  .image-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .image-thumb .remove-btn {
    position: absolute;
    top: -4px; right: -4px;
    width: 18px; height: 18px;
    background: #ef4444;
    color: white;
    border: none;
    border-radius: 50%;
    font-size: 11px;
    cursor: pointer;
    padding: 0;
  }
  .prompt-row {
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }
  .prompt-input {
    flex: 1;
    background: #0f0f23;
    border: 1px solid #2a2a4a;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 44px;
    max-height: 200px;
  }
  .prompt-input:focus { outline: none; border-color: #a78bfa; }
  .prompt-input::placeholder { color: #555; }
  .quick-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .btn-quick {
    background: #1e1e3a;
    border: 1px solid #2a2a4a;
    color: #ccc;
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-quick:hover { background: #2a2a4a; color: #fff; }

  /* ── Buttons ── */
  button {
    padding: 8px 20px;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  button:active { transform: scale(0.97); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-allow { background: #3b82f6; color: white; }
  .btn-allow:hover { background: #2563eb; }
  .btn-allow-lg { background: #3b82f6; color: white; padding: 10px 28px; font-size: 14px; }
  .btn-allow-lg:hover { background: #2563eb; }
  .btn-always { background: #16a34a; color: white; }
  .btn-always:hover { background: #15803d; }
  .btn-session { background: #0d9488; color: white; }
  .btn-session:hover { background: #0f766e; }
  .btn-deny { background: #333; color: #ccc; }
  .btn-deny:hover { background: #ef4444; color: white; }
  .btn-deny-sm { background: #333; color: #ccc; padding: 6px 14px; font-size: 12px; }
  .btn-deny-sm:hover { background: #ef4444; color: white; }
  .btn-feedback { background: #a78bfa22; color: #c4b5fd; }
  .btn-feedback:hover { background: #a78bfa44; }
  .btn-answer { background: #22d3ee; color: #0f0f23; font-weight: 700; }
  .btn-answer:hover { background: #06b6d4; }
  .btn-send { background: #a78bfa; color: white; padding: 10px 24px; white-space: nowrap; }
  .btn-send:hover { background: #8b5cf6; }
  .btn-allow-path { background: #78350f; color: #fbbf24; }
  .btn-allow-path:hover { background: #92400e; }
  .buttons {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 10px;
  }

  /* Markdown rendering */
  .md-h1, .md-h2, .md-h3 { font-weight: 700; margin: 4px 0 2px; }
  .md-h1 { color: #a78bfa; font-size: 15px; }
  .md-h2 { color: #c4b5fd; font-size: 14px; }
  .md-h3 { color: #ddd6fe; font-size: 13px; }
  .md-table { border-collapse: collapse; margin: 6px 0; width: auto; font-size: 12px; white-space: normal; }
  .md-table th, .md-table td { border: 1px solid #444; padding: 4px 8px; text-align: left; }
  .md-table th { background: #2a2a3a; font-weight: 600; color: #c4b5fd; }
  code {
    background: #1e1e3a;
    color: #facc15;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 12px;
  }

  @media (max-width: 600px) {
    .container { padding: 12px; }
    .prompt-area { padding: 12px; }
    .session-card { padding: 12px 14px; }
    .perm-card { padding: 12px 14px; }
    .buttons { flex-wrap: wrap; gap: 8px; }
    .buttons button { flex: 1 1 calc(50% - 8px); min-width: 80px; }
  }
</style>
</head>
<body>
<div class="header">
  <button class="back-btn" id="backBtn" onclick="showDashboard()">Back</button>
  <h1 id="pageTitle">Claude Sessions</h1>
  <span class="status" id="status">Connected</span>
</div>

<!-- Dashboard view -->
<div class="container dashboard" id="dashboardView">
  <div id="sessionList"></div>
</div>

<!-- Session detail view -->
<div class="session-detail" id="detailView">
  <div class="container">
    <div id="permCards"></div>
    <div class="transcript-view" id="transcriptView"></div>
  </div>
  <div class="prompt-area" id="promptArea">
    <div class="quick-actions">
      <button class="btn-quick" onclick="quickPrompt('/compact')">/compact</button>
      <button class="btn-quick" onclick="quickPrompt('/clear')">/clear</button>
    </div>
    <div class="image-upload-area">
      <input type="file" id="imageFile" accept="image/*" style="display:none" onchange="handleImageFile(this)">
      <button class="btn-upload-image" onclick="document.getElementById('imageFile').click()">+ Image</button>
      <div class="image-preview-area" id="imagePreview"></div>
    </div>
    <div class="prompt-row">
      <textarea class="prompt-input" id="promptInput" placeholder="Send a prompt to this session..." rows="1"></textarea>
      <button class="btn-send" onclick="sendPrompt()">Send</button>
    </div>
  </div>
</div>

<script>
// ── State ──
let currentView = 'dashboard';
let currentSessionId = null;
let respondedIds = new Set();
let imagePaths = [];
let pollTimer = null;
let lastDashboardHash = '';
let lastPermCardId = '';
let lastTranscriptHash = '';

// Question state
const questionSelections = {};
const questionMultiSelect = {};

function sessionHue(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) & 0xffffffff;
  return ((h % 360) + 360) % 360;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMarkdown(text) {
  let s = esc(text.trim());
  // Parse tables before other transformations
  s = s.replace(/(^\\|.+\\|\\s*\\n\\|[-| :]+\\|\\s*\\n(\\|.+\\|\\s*\\n?)+)/gm, function(table) {
    const rows = table.trim().split('\\n').filter(r => r.trim());
    if (rows.length < 2) return table;
    const parseRow = r => r.replace(/^\\|/, '').replace(/\\|$/, '').split('|').map(c => c.trim());
    const headers = parseRow(rows[0]);
    // rows[1] is the separator line, skip it
    let h = '<table class="md-table"><thead><tr>' + headers.map(c => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
    for (let i = 2; i < rows.length; i++) {
      const cells = parseRow(rows[i]);
      h += '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>';
    }
    h += '</tbody></table>';
    return h;
  });
  s = s.replace(/^### (.+)$/gm, '<span class="md-h3">$1</span>');
  s = s.replace(/^## (.+)$/gm, '<span class="md-h2">$1</span>');
  s = s.replace(/^# (.+)$/gm, '<span class="md-h1">$1</span>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  s = s.replace(/^(\\s*)[*-] (.+)$/gm, '$1&#8226; $2');
  return s;
}

function toolCat(name) {
  if (name === 'ExitPlanMode') return 'plan';
  if (name === 'AskUserQuestion') return 'question';
  if (name === 'Bash' || name === 'mcp__acp__Bash') return 'bash';
  if (/Write|Edit/.test(name)) return 'write';
  if (name === 'WebFetch' || name === 'WebSearch') return 'web';
  return 'other';
}

function stateLabel(s) {
  const m = {
    idle: 'Idle',
    busy: 'Working',
    permission_prompt: 'Needs Approval',
    elicitation: 'Question',
    plan_review: 'Plan Review'
  };
  return m[s] || s;
}

// ── Dashboard ──

async function fetchSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    document.getElementById('status').textContent = 'Last checked: ' + new Date().toLocaleTimeString();
    renderDashboard(data.sessions || []);
  } catch (e) {
    document.getElementById('status').textContent = 'Connection error';
  }
}

function renderDashboard(sessions) {
  const el = document.getElementById('sessionList');
  if (sessions.length === 0) {
    if (lastDashboardHash !== 'empty') {
      el.innerHTML = '<div class="empty"><span class="dot"></span>No active sessions</div>';
      lastDashboardHash = 'empty';
    }
    document.title = 'Claude Sessions';
    return;
  }
  const needAttention = sessions.filter(s =>
    s.state === 'permission_prompt' || s.state === 'elicitation' || s.state === 'plan_review' || s.state === 'idle'
  ).length;
  document.title = needAttention > 0 ? '(' + needAttention + ') Claude Sessions' : 'Claude Sessions';

  // Skip re-render if nothing changed
  const hash = sessions.map(s => s.session_id + ':' + (s.state||'') + ':' + (s.last_summary||'') + ':' + (s.last_user_prompt||'') + ':' + (s.last_activity||'') + ':' + (s.pending_request ? s.pending_request.id : '')).join('|');
  if (hash === lastDashboardHash) return;
  lastDashboardHash = hash;

  let html = '';
  sessions.forEach(s => {
    const project = (s.cwd || '').split('/').pop() || '?';
    const state = s.state || 'busy';
    const summary = esc(s.last_summary || '');
    const userPrompt = esc(s.last_user_prompt || '');
    const time = s.last_activity ? new Date(s.last_activity * 1000).toLocaleTimeString() : '';
    const hue = sessionHue(s.session_id);
    html += '<div class="session-card state-' + state + '" style="--sh:' + hue + '" onclick="openSession(\\'' + esc(s.session_id) + '\\')">';
    html += '<div class="sc-top">';
    html += '<span class="state-badge badge-' + state + '">' + stateLabel(state) + '</span>';
    html += '<span class="sc-project">' + esc(project) + '</span>';
    if (time) html += '<span class="sc-time">' + time + '</span>';
    html += '</div>';
    html += '<div class="sc-sid-row"><span class="sc-sid">Session ' + esc(s.session_id) + '</span></div>';
    if (userPrompt) html += '<div class="sc-user-prompt">' + userPrompt + '</div>';
    if (summary) html += '<div class="sc-summary">' + renderMarkdown(s.last_summary || '') + '</div>';

    // Inline permission approve/deny on dashboard
    if (state === 'permission_prompt' && s.pending_request) {
      const pr = s.pending_request;
      html += '<div class="sc-actions" onclick="event.stopPropagation()">';
      html += '<span style="color:#ef4444;font-size:12px;font-weight:700">' + esc(pr.tool_name) + '</span>';
      html += ' <span style="color:#888;font-size:12px">' + esc((pr.detail || '').substring(0, 80)) + '</span>';
      html += ' <button class="btn-allow" style="padding:5px 14px;font-size:12px" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>';
      html += ' <button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
      html += '</div>';
    }
    html += '</div>';
  });
  el.innerHTML = html;
}

// ── Session Detail ──

function openSession(sid) {
  currentSessionId = sid;
  currentView = 'detail';
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('detailView').style.display = 'block';
  document.getElementById('backBtn').style.display = 'block';
  document.getElementById('pageTitle').textContent = 'Session ' + sid;
  document.getElementById('transcriptView').innerHTML = '';
  document.getElementById('permCards').innerHTML = '';
  lastPermCardId = '';
  lastTranscriptHash = '';
  fetchSessionDetail();
  startDetailPolling();
}

function showDashboard() {
  currentSessionId = null;
  currentView = 'dashboard';
  document.getElementById('dashboardView').style.display = 'block';
  document.getElementById('detailView').style.display = 'none';
  document.getElementById('backBtn').style.display = 'none';
  document.getElementById('pageTitle').textContent = 'Claude Sessions';
  stopDetailPolling();
  lastDashboardHash = '';
  fetchSessions();
}

let detailPollTimer = null;
function startDetailPolling() {
  stopDetailPolling();
  detailPollTimer = setInterval(fetchSessionDetail, 1000);
}
function stopDetailPolling() {
  if (detailPollTimer) { clearInterval(detailPollTimer); detailPollTimer = null; }
}

async function fetchSessionDetail() {
  if (!currentSessionId) return;
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    const session = (data.sessions || []).find(s => s.session_id === currentSessionId);
    if (!session) {
      showDashboard();
      return;
    }

    // Render permission card if applicable
    renderPermCards(session);

    // Fetch transcript
    const tRes = await fetch('/api/session/' + currentSessionId + '/transcript?limit=50');
    const tData = await tRes.json();
    renderTranscript(tData.entries || []);
  } catch (e) {
    console.error('fetchSessionDetail error:', e);
  }
}

function renderPermCards(session) {
  const el = document.getElementById('permCards');
  if (!session.pending_request) {
    if (lastPermCardId) { el.innerHTML = ''; lastPermCardId = ''; }
    return;
  }
  const pr = session.pending_request;
  if (respondedIds.has(pr.id)) {
    if (lastPermCardId) { el.innerHTML = ''; lastPermCardId = ''; }
    return;
  }
  // Skip re-render if same permission request is already shown
  if (pr.id === lastPermCardId) return;
  lastPermCardId = pr.id;

  const cat = toolCat(pr.tool_name);
  const isBenign = ['plan','question','web'].includes(cat) || pr.tool_name === 'Read';
  const denyClass = isBenign ? 'btn-deny-sm' : 'btn-deny';
  const allowClass = isBenign ? 'btn-allow-lg' : 'btn-allow';

  let html = '<div class="perm-card" id="perm-' + esc(pr.id) + '">';
  html += '<div class="perm-header">';
  html += '<span class="perm-tool perm-tool-' + cat + '">' + esc(pr.tool_name) + '</span>';
  html += '</div>';

  if (cat === 'plan') {
    // Plan review card
    const planText = (pr.tool_input && pr.tool_input.plan) || pr.detail || '';
    html += '<div class="perm-detail">' + renderMarkdown(planText) + '</div>';
    if (pr.detail_sub) html += '<div class="perm-sub">' + esc(pr.detail_sub) + '</div>';
    html += '<div id="feedback-area-' + esc(pr.id) + '" style="display:none">';
    html += '<textarea class="plan-feedback-input" id="feedback-input-' + esc(pr.id) + '" placeholder="Tell Claude what to change..." rows="3"></textarea>';
    html += '<div class="buttons" style="margin-top:8px">';
    html += '<button class="btn-deny-sm" onclick="toggleFeedback(\\'' + esc(pr.id) + '\\')">Cancel</button>';
    html += '<button class="btn-allow" onclick="submitFeedback(\\'' + esc(pr.id) + '\\')">Send Feedback</button>';
    html += '</div></div>';
    html += '<div class="buttons" id="plan-buttons-' + esc(pr.id) + '">';
    html += '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
    html += '<button class="btn-feedback" onclick="toggleFeedback(\\'' + esc(pr.id) + '\\')">Feedback</button>';
    html += '<button class="btn-allow-lg" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Approve</button>';
    html += '</div>';

  } else if (cat === 'question') {
    // Question card
    html += renderQuestionCard(pr);

  } else {
    // Standard permission card
    if (pr.detail) html += '<div class="perm-detail">' + esc(pr.detail) + '</div>';
    if (pr.detail_sub) html += '<div class="perm-sub">' + esc(pr.detail_sub) + '</div>';

    // Allow info
    const hasMulti = pr.allow_patterns && pr.allow_patterns.length > 1;
    if (hasMulti) {
      html += '<div class="allow-info">"Always Allow All" will apply to: ' +
        pr.allow_patterns.map(p => '<code>' + esc(p) + '</code>').join(', ') + '</div>';
    } else {
      html += '<div class="allow-info">"Always Allow" will apply to: <code>' + esc(pr.allow_pattern || '') + '</code></div>';
    }

    // Session-allow info for Read/Edit/Write
    if (['Read','Edit','Write'].includes(pr.tool_name)) {
      html += '<div class="allow-info">"Allow this session" will auto-approve all <code>' + esc(pr.tool_name) + '</code> calls in session ' + esc(String(pr.session_id)) + '</div>';
    }

    // Path select area for Edit/Write
    if (['Edit','Write'].includes(pr.tool_name)) {
      html += '<div class="path-select-area" id="path-area-' + esc(pr.id) + '" style="display:none"></div>';
    }

    // Split patterns area
    if (hasMulti) {
      html += '<div class="path-select-area" id="split-area-' + esc(pr.id) + '" style="display:none"></div>';
    }

    html += '<div class="buttons">';
    html += '<button class="' + denyClass + '" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
    if (hasMulti) {
      html += '<button class="btn-always" onclick="respondAlwaysAll(\\'' + esc(pr.id) + '\\',this)">Always Allow All</button>';
      html += '<button class="btn-always" style="background:#0d9488" onclick="toggleSplitPatterns(\\'' + esc(pr.id) + '\\')">Allow Command...</button>';
    } else {
      html += '<button class="btn-always" onclick="respond(\\'' + esc(pr.id) + '\\',\\'always\\',this)">Always Allow</button>';
    }
    if (['Edit','Write'].includes(pr.tool_name)) {
      html += '<button class="btn-allow-path" onclick="togglePathSelect(\\'' + esc(pr.id) + '\\')">Allow Path</button>';
    }
    if (['Read','Edit','Write'].includes(pr.tool_name)) {
      html += '<button class="btn-session" onclick="respondSessionAllow(\\'' + esc(pr.id) + '\\',\\'' + esc(String(pr.session_id)) + '\\',\\'' + esc(pr.tool_name) + '\\',this)">Allow this session</button>';
    }
    html += '<button class="' + allowClass + '" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>';
    html += '</div>';
  }
  html += '</div>';
  el.innerHTML = html;
}

function renderQuestionCard(pr) {
  const questions = pr.tool_input && pr.tool_input.questions;
  if (!questions || !Array.isArray(questions) || questions.length === 0) {
    return '<div class="perm-detail">' + esc(pr.detail || '(no question data)') + '</div>' +
      '<div class="buttons">' +
      '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>' +
      '<button class="btn-allow-lg" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>' +
      '</div>';
  }
  questionSelections[pr.id] = {};
  questionMultiSelect[pr.id] = questions.some(q => q.multiSelect);
  questions.forEach((q, qi) => { questionSelections[pr.id][qi] = new Set(); });

  let h = '';
  questions.forEach((q, qi) => {
    h += '<div data-qidx="' + qi + '" style="margin-bottom:14px">';
    h += '<div style="color:#22d3ee;font-weight:700;font-size:15px;margin-bottom:10px">' + esc(q.question) + '</div>';
    if (q.options && Array.isArray(q.options)) {
      q.options.forEach((opt, oi) => {
        h += '<div class="q-option" id="qopt-' + esc(pr.id) + '-' + qi + '-' + oi + '" onclick="toggleQOpt(\\'' + esc(pr.id) + '\\',' + qi + ',' + oi + ',' + !!q.multiSelect + ')">';
        h += '<div class="q-check"></div><div>';
        h += '<div class="q-label">' + esc(opt.label) + '</div>';
        if (opt.description) h += '<div class="q-desc">' + esc(opt.description) + '</div>';
        h += '</div></div>';
      });
    }
    h += '</div>';
  });
  h += '<div class="q-custom-area">';
  h += '<button class="q-custom-toggle" id="q-custom-btn-' + esc(pr.id) + '" onclick="toggleQCustom(\\'' + esc(pr.id) + '\\')">Type a custom answer...</button>';
  h += '<textarea class="q-custom-input" id="q-custom-input-' + esc(pr.id) + '" style="display:none" placeholder="Type your answer..." rows="2"></textarea>';
  h += '</div>';
  h += '<div class="buttons" style="margin-top:14px">';
  h += '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
  h += '<button class="btn-answer" id="q-submit-' + esc(pr.id) + '" onclick="submitQAnswer(\\'' + esc(pr.id) + '\\')">Send Answer</button>';
  h += '</div>';
  return h;
}

function toggleQOpt(reqId, qIdx, optIdx, multi) {
  const sel = questionSelections[reqId][qIdx];
  if (sel.has(optIdx)) sel.delete(optIdx);
  else { if (!multi) sel.clear(); sel.add(optIdx); }
  const card = document.getElementById('perm-' + reqId);
  if (!card) return;
  const section = card.querySelector('[data-qidx="' + qIdx + '"]');
  if (!section) return;
  section.querySelectorAll('.q-option').forEach((el, i) => {
    el.classList.toggle('selected', sel.has(i));
  });
  if (!multi && sel.size > 0) {
    const ci = document.getElementById('q-custom-input-' + reqId);
    const cb = document.getElementById('q-custom-btn-' + reqId);
    if (ci) { ci.value = ''; ci.style.display = 'none'; }
    if (cb) cb.style.display = 'block';
  }
}

function toggleQCustom(reqId) {
  const btn = document.getElementById('q-custom-btn-' + reqId);
  const input = document.getElementById('q-custom-input-' + reqId);
  const multi = questionMultiSelect[reqId];
  if (input.style.display === 'none') {
    input.style.display = 'block'; btn.style.display = 'none';
    if (!multi) {
      const sel = questionSelections[reqId];
      for (const qi in sel) {
        sel[qi].clear();
        const card = document.getElementById('perm-' + reqId);
        if (card) card.querySelectorAll('[data-qidx="' + qi + '"] .q-option').forEach(el => el.classList.remove('selected'));
      }
    }
    input.focus();
  } else {
    input.style.display = 'none'; input.value = ''; btn.style.display = 'block';
  }
}

function submitQAnswer(reqId) {
  const sel = questionSelections[reqId];
  const ci = document.getElementById('q-custom-input-' + reqId);
  const customText = ci ? ci.value.trim() : '';
  const selected = [];
  for (const qi in sel) {
    sel[qi].forEach(oi => {
      const el = document.getElementById('qopt-' + reqId + '-' + qi + '-' + oi);
      if (el) { const l = el.querySelector('.q-label'); if (l) selected.push(l.textContent); }
    });
  }
  const multi = questionMultiSelect[reqId];
  if (multi && customText) selected.push(customText);
  else if (!multi && customText) {
    const btn = document.getElementById('q-submit-' + reqId);
    respond(reqId, 'deny', btn, 'User answered: ' + customText);
    return;
  }
  if (selected.length === 0) {
    const btn = document.getElementById('q-submit-' + reqId);
    btn.style.background = '#ef4444'; btn.textContent = 'Select an option';
    setTimeout(() => { btn.style.background = ''; btn.textContent = 'Send Answer'; }, 1500);
    return;
  }
  const msg = 'User answered: ' + selected.join(', ');
  const btn = document.getElementById('q-submit-' + reqId);
  respond(reqId, 'deny', btn, msg);
}

function renderTranscript(entries) {
  const el = document.getElementById('transcriptView');
  // Skip re-render if transcript hasn't changed
  const tHash = entries.length + ':' + (entries.length > 0 ? JSON.stringify(entries[entries.length - 1]).length : 0);
  if (tHash === lastTranscriptHash) return;
  lastTranscriptHash = tHash;
  const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  let html = '';
  entries.forEach(e => {
    if (e.type === 'user') {
      const content = e.message && e.message.content;
      if (!content) return;
      let text = '';
      let hasToolResult = false;
      if (Array.isArray(content)) {
        content.forEach(c => {
          if (typeof c === 'string') text += c;
          else if (c.type === 'text') text += c.text || '';
          else if (c.type === 'tool_result') hasToolResult = true;
        });
      }
      if (hasToolResult) return; // Skip tool result messages
      if (!text.trim()) return;
      html += '<div class="msg msg-user"><div class="msg-label">You</div><div class="msg-content">' + esc(text) + '</div></div>';
    } else if (e.type === 'assistant') {
      const msg = e.message || {};
      const content = msg.content || [];
      let text = '';
      let tools = [];
      content.forEach(c => {
        if (typeof c === 'object') {
          if (c.type === 'text') text += c.text || '';
          else if (c.type === 'tool_use') tools.push(c);
        }
      });
      if (text.trim()) {
        html += '<div class="msg msg-assistant"><div class="msg-label">Claude</div><div class="msg-content">' + renderMarkdown(text) + '</div></div>';
      }
      tools.forEach(t => {
        let detail = '';
        if (t.name === 'Bash' || t.name === 'mcp__acp__Bash') detail = (t.input && t.input.command) || '';
        else if (t.name === 'Write' || t.name === 'Edit' || t.name === 'mcp__acp__Write' || t.name === 'mcp__acp__Edit') detail = (t.input && t.input.file_path) || '';
        else if (t.name === 'Read') detail = (t.input && t.input.file_path) || '';
        else detail = JSON.stringify(t.input || {}).substring(0, 200);
        html += '<div class="msg msg-tool"><div class="msg-label">' + esc(t.name) + '</div><div class="msg-content">' + esc(detail) + '</div></div>';
      });
    }
  });
  el.innerHTML = html || '<div style="color:#555;text-align:center;padding:40px">No transcript entries</div>';
  // Only auto-scroll if user was already at the bottom
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

// ── Actions ──

async function respond(id, decision, btn, message) {
  if (btn) {
    const card = btn.closest('.perm-card, .session-card, .sc-actions');
    if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
    btn.textContent = '...';
  }
  try {
    const body = {id, decision};
    if (message) body.message = message;
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    respondedIds.add(id);
  } catch (e) {
    if (btn) btn.textContent = 'Error';
  }
}

async function respondAlwaysAll(reqId, btn) {
  const card = btn.closest('.perm-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.textContent = '...';
  // Get patterns from the session data
  try {
    const res = await fetch('/api/pending');
    const data = await res.json();
    const req = (data.requests || []).find(r => r.id === reqId);
    const patterns = (req && req.allow_patterns) || [];
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_patterns: patterns})
    });
    respondedIds.add(reqId);
  } catch (e) {
    btn.textContent = 'Error';
  }
}

async function respondSessionAllow(id, sessionId, toolName, btn) {
  const card = btn.closest('.perm-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.textContent = '...';
  try {
    await fetch('/api/session-allow', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, session_id: sessionId, tool_name: toolName})
    });
    respondedIds.add(id);
  } catch (e) {
    btn.textContent = 'Error';
  }
}

function toggleFeedback(id) {
  const area = document.getElementById('feedback-area-' + id);
  const buttons = document.getElementById('plan-buttons-' + id);
  if (area.style.display === 'none') {
    area.style.display = 'block';
    buttons.style.display = 'none';
    document.getElementById('feedback-input-' + id).focus();
  } else {
    area.style.display = 'none';
    buttons.style.display = 'flex';
  }
}

async function submitFeedback(id) {
  const input = document.getElementById('feedback-input-' + id);
  const feedback = input.value.trim();
  if (!feedback) { input.focus(); return; }
  const card = document.getElementById('perm-' + id);
  if (card) card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, decision: 'deny', message: feedback})
    });
    respondedIds.add(id);
  } catch (e) {}
}

function togglePathSelect(reqId) {
  const area = document.getElementById('path-area-' + reqId);
  if (!area) return;
  if (area.style.display !== 'none') { area.style.display = 'none'; return; }
  // Fetch request data to build path options
  fetch('/api/pending').then(r => r.json()).then(data => {
    const req = (data.requests || []).find(r => r.id === reqId);
    if (!req) return;
    const filePath = (req.tool_input && req.tool_input.file_path) || '';
    const projectDir = req.project_dir || '';
    const toolName = req.tool_name || 'Write';
    if (!filePath) return;
    const rel = projectDir && filePath.startsWith(projectDir) ? filePath.slice(projectDir.length).replace(/^\\//, '') : filePath;
    const parts = rel.split('/').filter(Boolean);
    const projectName = projectDir.split('/').filter(Boolean).pop() || '';
    let html = '';
    const rootPattern = toolName + '(' + projectDir + '/*)';
    html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(rootPattern) + '\\')">';
    html += '<div class="path-label">' + esc(projectName + '/*') + '</div>';
    html += '<div class="path-pattern">' + esc(rootPattern) + '</div></div>';
    let cumPath = projectDir;
    for (let i = 0; i < parts.length - 1; i++) {
      cumPath += '/' + parts[i];
      const displayPath = projectName + '/' + parts.slice(0, i + 1).join('/') + '/*';
      const pattern = toolName + '(' + cumPath + '/*)';
      html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pattern) + '\\')">';
      html += '<div class="path-label">' + esc(displayPath) + '</div>';
      html += '<div class="path-pattern">' + esc(pattern) + '</div></div>';
    }
    area.innerHTML = html;
    area.style.display = 'block';
  });
}

function toggleSplitPatterns(reqId) {
  const area = document.getElementById('split-area-' + reqId);
  if (!area) return;
  if (area.style.display !== 'none') { area.style.display = 'none'; return; }
  fetch('/api/pending').then(r => r.json()).then(data => {
    const req = (data.requests || []).find(r => r.id === reqId);
    if (!req || !req.allow_patterns) return;
    let html = '';
    req.allow_patterns.forEach(pat => {
      html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pat) + '\\')">';
      html += '<div class="path-label">Allow: <code>' + esc(pat) + '</code></div></div>';
    });
    area.innerHTML = html;
    area.style.display = 'block';
  });
}

async function submitPathAllow(reqId, pattern) {
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_pattern: pattern})
    });
    respondedIds.add(reqId);
  } catch (e) {}
}

// ── Prompt ──

async function sendPrompt() {
  if (!currentSessionId) return;
  const input = document.getElementById('promptInput');
  let prompt = input.value.trim();
  const images = imagePaths.slice();
  if (!prompt && images.length === 0) { input.focus(); return; }
  if (images.length > 0) {
    const refs = images.map(p => 'Please look at this image: ' + p).join('\\n');
    prompt = refs + (prompt ? '\\n\\n' + prompt : '');
  }
  input.value = '';
  imagePaths = [];
  document.getElementById('imagePreview').innerHTML = '';
  try {
    await fetch('/api/send-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: currentSessionId, prompt})
    });
  } catch (e) {
    console.error('Failed to send prompt:', e);
  }
}

async function quickPrompt(prompt) {
  if (!currentSessionId) return;
  try {
    await fetch('/api/send-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: currentSessionId, prompt})
    });
  } catch (e) {}
}

// ── Image upload ──

async function handleImageFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('image', file);
  try {
    const resp = await fetch('/api/upload-image', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.path) {
      imagePaths.push(data.path);
      renderImagePreviews();
    }
  } catch (e) {}
  input.value = '';
}

function renderImagePreviews() {
  const area = document.getElementById('imagePreview');
  area.innerHTML = imagePaths.map((p, i) =>
    '<div class="image-thumb">' +
    '<img src="/api/image?path=' + encodeURIComponent(p) + '">' +
    '<button class="remove-btn" onclick="imagePaths.splice(' + i + ',1);renderImagePreviews()">x</button>' +
    '</div>'
  ).join('');
}

// Ctrl+Enter to send
document.getElementById('promptInput').addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    sendPrompt();
  }
});

// Paste images
document.getElementById('promptInput').addEventListener('paste', function(e) {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const file = item.getAsFile();
      const formData = new FormData();
      formData.append('image', file);
      fetch('/api/upload-image', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => { if (data.path) { imagePaths.push(data.path); renderImagePreviews(); } });
      return;
    }
  }
});

// ── Polling ──
fetchSessions();
pollTimer = setInterval(() => {
  if (currentView === 'dashboard') fetchSessions();
}, 2000);
</script>
</body>
</html>"""
