#!/usr/bin/env python3
"""
Claude Code Permission Approval Web Server

Watches /tmp/claude-approvals/ for permission requests from the hook script,
serves a web UI for the user to approve/deny, and writes responses back.

Usage: python3 approval-server.py
Then open http://localhost:19836
"""

import json
import glob
import os
import signal
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

QUEUE_DIR = "/tmp/claude-approvals"
PORT = 19836

# Session-level auto-allow rules: { (session_id, tool_name): True }
session_auto_allow = {}


def check_auto_allow():
    """Scan pending requests and auto-approve those matching session auto-allow rules."""
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
        resp_path = path.replace(".request.json", ".response.json")
        if os.path.exists(resp_path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        sid = str(data.get("session_id", ""))
        tname = data.get("tool_name", "")
        if (sid, tname) in session_auto_allow:
            try:
                with open(resp_path, "w") as f:
                    json.dump({"decision": "allow"}, f)
                print(f"[~] Auto-allowed {tname} for session {sid}")
            except IOError:
                pass


def auto_allow_loop():
    """Background thread: periodically check for auto-allowable requests."""
    while True:
        if session_auto_allow:
            check_auto_allow()
        time.sleep(0.5)

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Approvals</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 24px;
  }
  h1 {
    font-size: 20px;
    color: #a78bfa;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .status {
    font-size: 12px;
    color: #666;
    margin-left: auto;
    font-weight: normal;
  }
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
  .card {
    background: #16213e;
    border: 1px solid #2a2a4a;
    border-left: 4px solid #a78bfa;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 16px;
    animation: slideIn 0.3s ease;
  }
  .card.cat-bash     { border-left-color: #ef4444; }
  .card.cat-write    { border-left-color: #f97316; }
  .card.cat-plan     { border-left-color: #a78bfa; }
  .card.cat-question { border-left-color: #22d3ee; }
  .card.cat-read     { border-left-color: #4ade80; }
  .card.cat-prompt   { border-left-color: #10b981; }
  .card.cat-other    { border-left-color: #666; }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .card-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
  }
  .tool-badge {
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
  }
  .badge-bash     { background: #ef444422; color: #ef4444; }
  .badge-write    { background: #f9731622; color: #f97316; }
  .badge-plan     { background: #a78bfa22; color: #a78bfa; }
  .badge-question { background: #22d3ee22; color: #22d3ee; }
  .badge-read     { background: #4ade8022; color: #4ade80; }
  .badge-prompt   { background: #10b98122; color: #10b981; }
  .badge-other    { background: #66666622; color: #999; }
  .project-tag {
    background: #facc1522;
    color: #facc15;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
  }
  .session-id {
    color: #666;
    font-size: 11px;
  }
  .project-path {
    font-size: 11px;
    color: #555;
    margin-bottom: 8px;
    font-weight: 600;
  }
  .timestamp {
    font-size: 11px;
    color: #555;
    margin-left: auto;
  }
  .detail {
    background: #0f0f23;
    border-radius: 8px;
    padding: 14px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
    margin-bottom: 14px;
    position: relative;
    overflow: hidden;
  }
  .detail.collapsed { max-height: 380px; }
  .detail.collapsed::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 40px;
    background: linear-gradient(transparent, #0f0f23);
    pointer-events: none;
  }
  .detail-toggle {
    background: none;
    border: none;
    color: #a78bfa;
    font-size: 12px;
    cursor: pointer;
    padding: 4px 0;
    margin-bottom: 10px;
    font-weight: 600;
  }
  .detail-toggle:hover { color: #c4b5fd; }
  .detail-toggle:active { transform: none; }
  /* Markdown-rendered content styles */
  .detail .md-h1, .detail .md-h2, .detail .md-h3 {
    font-weight: 700;
    margin: 4px 0 2px;
  }
  .detail .md-h1 { color: #a78bfa; font-size: 15px; }
  .detail .md-h2 { color: #c4b5fd; font-size: 14px; }
  .detail .md-h3 { color: #ddd6fe; font-size: 13px; }
  .detail code {
    background: #1e1e3a;
    color: #facc15;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 12px;
  }
  .detail .md-bullet { color: #a78bfa; }
  /* Question rendering */
  .question-block { margin-bottom: 10px; }
  .question-text { color: #22d3ee; font-weight: 700; }
  .question-header-tag {
    display: inline-block;
    background: #22d3ee22;
    color: #22d3ee;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 11px;
    margin-left: 6px;
  }
  .question-multi-tag {
    display: inline-block;
    background: #f9731622;
    color: #f97316;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 11px;
    margin-left: 4px;
  }
  .question-option {
    padding: 2px 0 2px 16px;
    color: #ccc;
  }
  .question-option .opt-label { color: #e0e0e0; font-weight: 600; }
  .question-option .opt-desc { color: #999; }
  .allow-info {
    font-size: 12px;
    color: #888;
    margin-bottom: 14px;
  }
  .allow-info code {
    color: #facc15;
    background: #facc1511;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .buttons {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
  }
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
  .btn-allow {
    background: #3b82f6;
    color: white;
  }
  .btn-allow:hover { background: #2563eb; }
  .btn-allow-lg {
    background: #3b82f6;
    color: white;
    padding: 10px 28px;
    font-size: 14px;
  }
  .btn-allow-lg:hover { background: #2563eb; }
  .btn-always {
    background: #16a34a;
    color: white;
  }
  .btn-always:hover { background: #15803d; }
  .btn-session {
    background: #0d9488;
    color: white;
  }
  .btn-session:hover { background: #0f766e; }
  .btn-deny {
    background: #333;
    color: #ccc;
  }
  .btn-deny:hover { background: #ef4444; color: white; }
  .btn-deny-sm {
    background: #333;
    color: #ccc;
    padding: 6px 14px;
    font-size: 12px;
  }
  .btn-deny-sm:hover { background: #ef4444; color: white; }
  .btn-feedback {
    background: #a78bfa22;
    color: #c4b5fd;
    padding: 8px 20px;
  }
  .btn-feedback:hover { background: #a78bfa44; }
  /* Plan-specific styles */
  .plan-intro {
    color: #ccc;
    font-size: 14px;
    margin-bottom: 8px;
  }
  .plan-detail {
    border-left: 3px solid #a78bfa33;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .plan-prompts-section {
    margin-bottom: 12px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .plan-prompts-label {
    font-size: 12px;
    color: #888;
  }
  .plan-prompt-tag {
    background: #a78bfa18;
    color: #c4b5fd;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-family: monospace;
  }
  .plan-prompt {
    color: #ccc;
    font-size: 14px;
    margin-bottom: 12px;
  }
  .plan-feedback-area {
    margin-bottom: 4px;
  }
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
  .plan-feedback-input:focus {
    outline: none;
    border-color: #a78bfa;
  }
  .plan-feedback-input::placeholder { color: #555; }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  /* Interactive question card styles */
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
    width: 20px;
    height: 20px;
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
    transition: all 0.15s;
  }
  .q-custom-toggle:hover { border-color: #22d3ee55; color: #aaa; }
  .q-custom-toggle:active { transform: none; }
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
  .q-custom-input::placeholder { color: #555; }
  .btn-answer {
    background: #22d3ee;
    color: #0f0f23;
    font-weight: 700;
  }
  .btn-answer:hover { background: #06b6d4; }
  .btn-answer:disabled { background: #22d3ee55; color: #0f0f23aa; }
  /* Prompt input card styles */
  .prompt-text {
    color: #10b981;
    font-size: 14px;
    margin-bottom: 12px;
    font-weight: 600;
  }
  .prompt-input {
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
    min-height: 80px;
    margin-bottom: 14px;
  }
  .prompt-input:focus {
    outline: none;
    border-color: #10b981;
  }
  .prompt-input::placeholder { color: #555; }
  .quick-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .btn-quick {
    background: #1e1e3a;
    border: 1px solid #2a2a4a;
    color: #ccc;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-quick:hover { background: #2a2a4a; color: #fff; }
  .btn-quick:active { transform: scale(0.97); }
  .path-select-area {
    margin-bottom: 12px;
  }
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
    color: #e0e0e0;
  }
  .path-option:hover { border-color: #f59e0b55; background: #16213e; }
  .path-option:active { transform: scale(0.99); }
  .path-option .path-label { color: #f59e0b; font-weight: 600; }
  .path-option .path-pattern { color: #888; font-size: 11px; margin-top: 2px; }
  .btn-allow-path {
    background: #78350f;
    color: #fbbf24;
  }
  .btn-allow-path:hover { background: #92400e; }
  .btn-submit-prompt {
    background: #10b981;
    color: white;
    font-weight: 700;
  }
  .btn-submit-prompt:hover { background: #059669; }
  .btn-dismiss {
    background: #333;
    color: #ccc;
  }
  .btn-dismiss:hover { background: #555; }
  .q-section { margin-bottom: 14px; }
  .q-section-question {
    color: #22d3ee;
    font-weight: 700;
    font-size: 15px;
    margin-bottom: 10px;
  }
  .q-section-tags { margin-bottom: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
  @media (max-width: 600px) {
    body { padding: 12px; }
    .card { padding: 14px; }
    .card-header { flex-wrap: wrap; gap: 6px; }
    .detail { font-size: 12px; }
    .detail.collapsed { max-height: 260px; }
    .buttons { flex-wrap: wrap; gap: 8px; }
    .buttons button { flex: 1 1 calc(50% - 8px); min-width: 80px; box-sizing: border-box; }
  }
</style>
</head>
<body>
<h1>
  Claude Code Approvals
  <span class="status" id="status">Connected</span>
</h1>
<div id="requests"></div>

<script>
let knownIds = new Set();
let respondedIds = new Set();
let lastPending = [];

function toolCategory(name) {
  if (name === 'ExitPlanMode') return 'plan';
  if (name === 'AskUserQuestion') return 'question';
  if (name === 'Bash' || name === 'mcp__acp__Bash') return 'bash';
  if (/^(Write|Edit|NotebookEdit)(|mcp__.*)$/.test(name) ||
      name.startsWith('mcp__') && /Write|Edit/.test(name)) return 'write';
  if (name === 'Read' || (name.startsWith('mcp__') && /Read/.test(name))) return 'read';
  return 'other';
}

function isBenign(cat) {
  return cat === 'plan' || cat === 'question' || cat === 'read';
}

// Store request data for path-select lookups
const reqDataMap = {};


async function fetchPending() {
  try {
    const res = await fetch('/api/pending');
    const data = await res.json();
    document.getElementById('status').textContent =
      'Last checked: ' + new Date().toLocaleTimeString();
    const filtered = data.requests.filter(r => !respondedIds.has(r.id));
    renderRequests(filtered);
  } catch (e) {
    console.error('[DEBUG] fetchPending error:', e);
    document.getElementById('status').textContent = 'Connection error';
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderMarkdown(text) {
  let s = esc(text);
  // Headers
  s = s.replace(/^### (.+)$/gm, '<span class="md-h3">$1</span>');
  s = s.replace(/^## (.+)$/gm, '<span class="md-h2">$1</span>');
  s = s.replace(/^# (.+)$/gm, '<span class="md-h1">$1</span>');
  // Inline code
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Bullets
  s = s.replace(/^(\s*)[*-] (.+)$/gm, '$1<span class="md-bullet">&#8226;</span> $2');
  return s;
}

function renderQuestion(req) {
  const questions = req.tool_input && req.tool_input.questions;
  if (!questions || !Array.isArray(questions) || questions.length === 0) {
    return esc(req.detail || '(no question data)');
  }
  let html = '';
  questions.forEach((q, i) => {
    html += '<div class="question-block">';
    html += '<div><span class="question-text">' + esc(q.question) + '</span>';
    if (q.header) html += '<span class="question-header-tag">' + esc(q.header) + '</span>';
    if (q.multiSelect) html += '<span class="question-multi-tag">multi-select</span>';
    html += '</div>';
    if (q.options && Array.isArray(q.options)) {
      q.options.forEach(opt => {
        html += '<div class="question-option">';
        html += '<span class="opt-label">' + esc(opt.label) + '</span>';
        if (opt.description) html += ' &mdash; <span class="opt-desc">' + esc(opt.description) + '</span>';
        html += '</div>';
      });
    }
    html += '</div>';
  });
  return html;
}

function renderDetail(req, cat) {
  if (cat === 'plan') {
    const planText = (req.tool_input && req.tool_input.plan) || req.detail || '';
    return renderMarkdown(planText);
  }
  if (cat === 'question') return renderQuestion(req);
  return esc(req.detail || '');
}

function setupCollapsible(card) {
  const detail = card.querySelector('.detail');
  if (!detail) return;
  // Wait for render, then check overflow
  requestAnimationFrame(() => {
    const threshold = window.innerWidth <= 600 ? 80 : 120;
    if (detail.scrollHeight > threshold + 20) {
      detail.classList.add('collapsed');
      const toggle = document.createElement('button');
      toggle.className = 'detail-toggle';
      toggle.textContent = 'Show more...';
      toggle.addEventListener('click', () => {
        if (detail.classList.contains('collapsed')) {
          detail.classList.remove('collapsed');
          detail.style.maxHeight = 'none';
          toggle.textContent = 'Show less';
        } else {
          detail.classList.add('collapsed');
          detail.style.maxHeight = '';
          toggle.textContent = 'Show more...';
        }
      });
      detail.parentNode.insertBefore(toggle, detail.nextSibling);
    }
  });
}

function renderPromptCard(req, time) {
  let responseHtml = '';
  if (req.last_response) {
    responseHtml = `
      <div class="detail collapsed" id="prompt-response-${req.id}">${renderMarkdown(req.last_response)}</div>`;
  }
  return `
    <div class="card-header">
      <span class="tool-badge badge-prompt">Claude is ready</span>
      <span class="project-tag">${esc(req.project_dir || '').split('/').pop()}</span>
      <span class="session-id">Session ${req.session_id || '?'}</span>
      <span class="timestamp">${time}</span>
    </div>
    <div class="project-path">${esc(req.project_dir || '')}</div>
    <div class="prompt-text">Claude has finished and is waiting for your next instruction.</div>
    ${responseHtml}
    <div class="quick-actions">
      <button class="btn-quick" onclick="quickPrompt('${req.id}','/clear')">/clear</button>
      <button class="btn-quick" onclick="quickPrompt('${req.id}','Implement the next TODO item from PRD.md')">Next TODO</button>
      <button class="btn-quick" onclick="quickPrompt('${req.id}','Commit the current changes and push to GitHub')">Push to GitHub</button>
    </div>
    <textarea class="prompt-input" id="prompt-input-${req.id}" placeholder="Type your next instruction for Claude..." rows="3"></textarea>
    <div class="buttons">
      <button class="btn-dismiss" onclick="dismissPrompt('${req.id}',this)">Dismiss</button>
      <button class="btn-submit-prompt" onclick="submitPrompt('${req.id}')">Submit</button>
    </div>`;
}

async function quickPrompt(id, prompt) {
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/submit-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, prompt})
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    card.querySelectorAll('button, textarea').forEach(el => el.disabled = false);
  }
}

async function submitPrompt(id) {
  const input = document.getElementById('prompt-input-' + id);
  const prompt = input ? input.value.trim() : '';
  if (!prompt) { if (input) input.focus(); return; }
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/submit-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, prompt})
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    card.querySelectorAll('button, textarea').forEach(el => el.disabled = false);
  }
}

async function dismissPrompt(id, btn) {
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/dismiss-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    card.querySelectorAll('button, textarea').forEach(el => el.disabled = false);
  }
}

function renderRequests(requests) {
  lastPending = requests;
  const container = document.getElementById('requests');
  if (requests.length === 0) {
    container.innerHTML = '<div class="empty"><span class="dot"></span>Waiting for permission requests...</div>';
    knownIds.clear();
    return;
  }
  // Remove cards for requests that no longer exist
  const currentIds = new Set(requests.map(r => r.id));
  knownIds.forEach(id => {
    if (!currentIds.has(id)) {
      const el = document.getElementById('card-' + id);
      if (el) el.remove();
    }
  });
  // Add new cards
  requests.forEach(req => {
    if (!knownIds.has(req.id)) {
      const isPrompt = req.type === 'prompt-waiting';
      const cat = isPrompt ? 'prompt' : toolCategory(req.tool_name);
      const benign = isBenign(cat);
      reqDataMap[req.id] = req;
      const card = document.createElement('div');
      card.className = 'card cat-' + cat;
      card.id = 'card-' + req.id;
      const time = new Date(req.timestamp * 1000).toLocaleTimeString();

      const detailHtml = isPrompt ? '' : renderDetail(req, cat);
      const denyClass = benign ? 'btn-deny-sm' : 'btn-deny';
      const allowClass = benign ? 'btn-allow-lg' : 'btn-allow';

      if (isPrompt) {
        card.innerHTML = renderPromptCard(req, time);
      } else if (cat === 'plan') {
        // Plan-specific layout matching Claude Code CLI
        card.innerHTML = `
          <div class="card-header">
            <span class="tool-badge badge-${cat}">Ready to code?</span>
            <span class="project-tag">${esc(req.project_dir || '').split('/').pop()}</span>
            <span class="session-id">Session ${req.session_id || '?'}</span>
            <span class="timestamp">${time}</span>
          </div>
          <div class="plan-intro">Here is Claude's plan:</div>
          <div class="detail plan-detail">${detailHtml}</div>
          <div class="plan-prompt">Claude has written up a plan and is ready to execute. Would you like to proceed?</div>
          <div class="plan-feedback-area" id="feedback-area-${req.id}" style="display:none">
            <textarea class="plan-feedback-input" id="feedback-input-${req.id}" placeholder="Tell Claude what to change..." rows="3"></textarea>
            <div class="buttons" style="margin-top:8px">
              <button class="btn-deny-sm" onclick="toggleFeedback('${req.id}')">Cancel</button>
              <button class="btn-allow" onclick="submitFeedback('${req.id}')">Send Feedback</button>
            </div>
          </div>
          <div class="buttons" id="plan-buttons-${req.id}">
            <button class="btn-deny-sm" onclick="respond('${req.id}','deny',this)">Deny</button>
            <button class="btn-feedback" onclick="toggleFeedback('${req.id}')">Feedback</button>
            <button class="btn-allow-lg" onclick="respond('${req.id}','allow',this)">Approve</button>
          </div>`;
      } else if (cat === 'question') {
        card.innerHTML = renderQuestionCard(req, time);
      } else {
        card.innerHTML = `
          <div class="card-header">
            <span class="tool-badge badge-${cat}">${esc(req.tool_name)}</span>
            <span class="project-tag">${esc(req.project_dir || '').split('/').pop()}</span>
            <span class="session-id">Session ${req.session_id || '?'}</span>
            <span class="timestamp">${time}</span>
          </div>
          <div class="project-path">${esc(req.project_dir || '')}</div>
          <div class="detail">${detailHtml}</div>
          ${req.detail_sub ? '<div class="detail" style="margin-top:8px;color:#aaa;font-size:12px">' + esc(req.detail_sub) + '</div>' : ''}
          ${renderAllowInfo(req)}
          ${['Read','Edit','Write'].includes(req.tool_name) ? '<div class="allow-info">"Allow this session" will auto-approve all <code>' + esc(req.tool_name) + '</code> calls in session ' + esc(String(req.session_id)) + '</div>' : ''}
          ${['Edit','Write'].includes(req.tool_name) ? '<div class="path-select-area" id="path-area-' + req.id + '" style="display:none"></div>' : ''}
          ${hasSplitPatterns(req) ? '<div class="path-select-area" id="split-area-' + req.id + '" style="display:none"></div>' : ''}
          <div class="buttons">
            <button class="${denyClass}" onclick="respond('${req.id}','deny',this)">Deny</button>
            <button class="btn-always" onclick="${hasSplitPatterns(req) ? 'respondAlwaysAllPatterns(\\'' + req.id + '\\',this)' : 'respond(\\'' + req.id + '\\',\\'always\\',this)'}">Always Allow${hasSplitPatterns(req) ? ' All' : ''}</button>
            ${hasSplitPatterns(req) ? '<button class="btn-always" style="background:#0d9488" onclick="toggleSplitPatterns(\\'' + req.id + '\\')">Allow Command...</button>' : ''}
            ${['Edit','Write'].includes(req.tool_name) ? '<button class="btn-allow-path" onclick="togglePathSelect(\\'' + req.id + '\\')">Allow Path</button>' : ''}
            ${['Read','Edit','Write'].includes(req.tool_name) ? '<button class="btn-session" onclick="respondSessionAllow(\\'' + req.id + '\\',\\'' + req.session_id + '\\',\\'' + req.tool_name + '\\',this)">Allow this session</button>' : ''}
            <button class="${allowClass}" onclick="respond('${req.id}','allow',this)">Allow</button>
          </div>`;
      }
      container.prepend(card);
      setupCollapsible(card);
    }
  });
  knownIds = currentIds;
}

function hasSplitPatterns(req) {
  return req.allow_patterns && Array.isArray(req.allow_patterns) && req.allow_patterns.length > 1;
}

function renderAllowInfo(req) {
  if (hasSplitPatterns(req)) {
    return '<div class="allow-info">"Always Allow All" will apply to: ' +
      req.allow_patterns.map(p => '<code>' + esc(p) + '</code>').join(', ') + '</div>';
  }
  return '<div class="allow-info">"Always Allow" will apply to: <code>' + esc(req.allow_pattern) + '</code></div>';
}

function toggleSplitPatterns(reqId) {
  const area = document.getElementById('split-area-' + reqId);
  if (area.style.display !== 'none') { area.style.display = 'none'; return; }
  const card = document.getElementById('card-' + reqId);
  const req = lastPending.find(r => r.id === reqId);
  if (!req || !req.allow_patterns) return;
  let html = '';
  req.allow_patterns.forEach(pat => {
    html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pat).replace(/'/g, "\\\\'") + '\\')">'
      + '<div class="path-label">Allow: <code>' + esc(pat) + '</code></div>'
      + '</div>';
  });
  area.innerHTML = html;
  area.style.display = 'block';
}

async function respondAlwaysAllPatterns(reqId, btn) {
  const card = document.getElementById('card-' + reqId);
  const buttons = card.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  btn.textContent = '...';
  const req = lastPending.find(r => r.id === reqId);
  const patterns = (req && req.allow_patterns) || [];
  try {
    // Send all patterns; server will add each to settings
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_patterns: patterns})
    });
    respondedIds.add(reqId);
    knownIds.delete(reqId);
    card.remove();
  } catch (e) {
    buttons.forEach(b => b.disabled = false);
    btn.textContent = 'Error';
  }
}

async function respond(id, decision, btn, message) {
  const card = document.getElementById('card-' + id);
  const buttons = card.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  btn.textContent = '...';

  try {
    const body = {id, decision};
    if (message) body.message = message;
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    buttons.forEach(b => b.disabled = false);
    btn.textContent = 'Error';
  }
}

async function respondSessionAllow(id, sessionId, toolName, btn) {
  const card = document.getElementById('card-' + id);
  const buttons = card.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  btn.textContent = '...';

  try {
    await fetch('/api/session-allow', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, session_id: sessionId, tool_name: toolName})
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    buttons.forEach(b => b.disabled = false);
    btn.textContent = 'Error';
  }
}

function togglePathSelect(reqId) {
  const area = document.getElementById('path-area-' + reqId);
  if (!area) return;
  if (area.style.display !== 'none') {
    area.style.display = 'none';
    return;
  }
  const req = reqDataMap[reqId];
  if (!req) return;
  const filePath = (req.tool_input && req.tool_input.file_path) || '';
  const projectDir = req.project_dir || '';
  const toolName = req.tool_name || 'Write';
  if (!filePath) return;

  // Build directory segments between project_dir and file
  const rel = projectDir && filePath.startsWith(projectDir)
    ? filePath.slice(projectDir.length).replace(/^\\//, '')
    : filePath;
  const parts = rel.split('/').filter(Boolean);
  const projectName = projectDir.split('/').filter(Boolean).pop() || '';

  let html = '';
  let cumPath = projectDir;
  for (let i = 0; i < parts.length - 1; i++) {
    cumPath += '/' + parts[i];
    const displayPath = projectName + '/' + parts.slice(0, i + 1).join('/') + '/*';
    const pattern = toolName + '(' + cumPath + '/*)';
    html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pattern).replace(/'/g, "\\\\'") + '\\')">'
      + '<div class="path-label">' + esc(displayPath) + '</div>'
      + '<div class="path-pattern">' + esc(pattern) + '</div>'
      + '</div>';
  }
  area.innerHTML = html;
  area.style.display = 'block';
}

async function submitPathAllow(reqId, pattern) {
  const card = document.getElementById('card-' + reqId);
  card.querySelectorAll('button, .path-option').forEach(el => {
    if (el.tagName === 'BUTTON') el.disabled = true;
    else el.style.pointerEvents = 'none';
  });
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_pattern: pattern})
    });
    respondedIds.add(reqId);
    knownIds.delete(reqId);
    card.remove();
  } catch (e) {
    card.querySelectorAll('button').forEach(b => b.disabled = false);
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
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, decision: 'deny', message: feedback})
    });
    respondedIds.add(id);
    knownIds.delete(id);
    card.remove();
  } catch (e) {
    card.querySelectorAll('button, textarea').forEach(el => el.disabled = false);
  }
}

// --- Interactive question card ---
// Track selected options per request: { reqId: { qIdx: Set<optIdx> } }
const questionSelections = {};
// Track whether a request has any multiSelect question
const questionMultiSelect = {};

function renderQuestionCard(req, time) {
  const cat = 'question';
  const questions = req.tool_input && req.tool_input.questions;
  if (!questions || !Array.isArray(questions) || questions.length === 0) {
    // Fallback to a simple detail card
    return `
      <div class="card-header">
        <span class="tool-badge badge-${cat}">AskUserQuestion</span>
        <span class="project-tag">${esc(req.project_dir || '').split('/').pop()}</span>
        <span class="session-id">Session ${req.session_id || '?'}</span>
        <span class="timestamp">${time}</span>
      </div>
      <div class="detail">${esc(req.detail || '(no question data)')}</div>
      <div class="buttons">
        <button class="btn-deny-sm" onclick="respond('${req.id}','deny',this)">Deny</button>
        <button class="btn-allow-lg" onclick="respond('${req.id}','allow',this)">Allow</button>
      </div>`;
  }

  // Init selection state
  questionSelections[req.id] = {};
  questionMultiSelect[req.id] = questions.some(q => q.multiSelect);
  questions.forEach((q, qi) => { questionSelections[req.id][qi] = new Set(); });

  let html = `
    <div class="card-header">
      <span class="tool-badge badge-${cat}">AskUserQuestion</span>
      <span class="project-tag">${esc(req.project_dir || '').split('/').pop()}</span>
      <span class="session-id">Session ${req.session_id || '?'}</span>
      <span class="timestamp">${time}</span>
    </div>`;

  questions.forEach((q, qi) => {
    html += `<div class="q-section" data-qidx="${qi}">`;
    html += `<div class="q-section-tags">`;
    if (q.header) html += `<span class="question-header-tag">${esc(q.header)}</span>`;
    if (q.multiSelect) html += `<span class="question-multi-tag">multi-select</span>`;
    html += `</div>`;
    html += `<div class="q-section-question">${esc(q.question)}</div>`;

    if (q.options && Array.isArray(q.options)) {
      q.options.forEach((opt, oi) => {
        html += `<div class="q-option" id="qopt-${req.id}-${qi}-${oi}" onclick="toggleQuestionOption('${req.id}',${qi},${oi},${!!q.multiSelect})">
          <div class="q-check"></div>
          <div>
            <div class="q-label">${esc(opt.label)}</div>
            ${opt.description ? '<div class="q-desc">' + esc(opt.description) + '</div>' : ''}
          </div>
        </div>`;
      });
    }
    html += `</div>`;
  });

  // Custom answer area
  html += `
    <div class="q-custom-area">
      <button class="q-custom-toggle" id="q-custom-btn-${req.id}" onclick="toggleQuestionCustom('${req.id}')">Type a custom answer...</button>
      <textarea class="q-custom-input" id="q-custom-input-${req.id}" style="display:none" placeholder="Type your answer here..." rows="2"></textarea>
    </div>`;

  // Buttons
  html += `
    <div class="buttons" style="margin-top:14px">
      <button class="btn-deny-sm" onclick="respond('${req.id}','deny',this)">Deny</button>
      <button class="btn-answer" id="q-submit-${req.id}" onclick="submitQuestionAnswer('${req.id}')">Send Answer</button>
    </div>`;

  return html;
}

function toggleQuestionOption(reqId, qIdx, optIdx, multi) {
  const sel = questionSelections[reqId][qIdx];
  if (sel.has(optIdx)) {
    sel.delete(optIdx);
  } else {
    if (!multi) sel.clear();
    sel.add(optIdx);
  }
  // Update visual state for all options in this question
  const section = document.querySelector(`#card-${reqId} .q-section[data-qidx="${qIdx}"]`);
  if (!section) return;
  section.querySelectorAll('.q-option').forEach((el, i) => {
    el.classList.toggle('selected', sel.has(i));
  });
  // For single-select, clear and hide custom input when selecting an option
  if (!multi && sel.size > 0) {
    const customInput = document.getElementById('q-custom-input-' + reqId);
    const customBtn = document.getElementById('q-custom-btn-' + reqId);
    if (customInput) { customInput.value = ''; customInput.style.display = 'none'; }
    if (customBtn) customBtn.style.display = 'block';
  }
}

function clearQuestionSelections(reqId) {
  const sel = questionSelections[reqId];
  if (!sel) return;
  for (const qIdx in sel) {
    sel[qIdx].clear();
    const section = document.querySelector(`#card-${reqId} .q-section[data-qidx="${qIdx}"]`);
    if (section) section.querySelectorAll('.q-option').forEach(el => el.classList.remove('selected'));
  }
}

function toggleQuestionCustom(reqId) {
  const btn = document.getElementById('q-custom-btn-' + reqId);
  const input = document.getElementById('q-custom-input-' + reqId);
  const multi = questionMultiSelect[reqId];
  if (input.style.display === 'none') {
    input.style.display = 'block';
    btn.style.display = 'none';
    if (!multi) clearQuestionSelections(reqId);
    input.focus();
  } else {
    input.style.display = 'none';
    input.value = '';
    btn.style.display = 'block';
  }
}

function submitQuestionAnswer(reqId) {
  const req = questionSelections[reqId];
  const customInput = document.getElementById('q-custom-input-' + reqId);
  const customText = customInput ? customInput.value.trim() : '';

  // Collect selected option labels from the DOM
  const selected = [];
  for (const qIdx in req) {
    req[qIdx].forEach(optIdx => {
      const optEl = document.getElementById('qopt-' + reqId + '-' + qIdx + '-' + optIdx);
      if (optEl) {
        const label = optEl.querySelector('.q-label');
        if (label) selected.push(label.textContent);
      }
    });
  }

  // For multi-select: combine selected options and custom text
  // For single-select: custom text takes priority (they are mutually exclusive)
  const multi = questionMultiSelect[reqId];
  if (multi && customText) {
    selected.push(customText);
  } else if (!multi && customText) {
    const btn = document.getElementById('q-submit-' + reqId);
    respond(reqId, 'deny', btn, 'User answered: ' + customText);
    return;
  }

  if (selected.length === 0) {
    // Nothing selected, flash the button
    const btn = document.getElementById('q-submit-' + reqId);
    btn.style.background = '#ef4444';
    btn.textContent = 'Select an option';
    setTimeout(() => { btn.style.background = ''; btn.textContent = 'Send Answer'; }, 1500);
    return;
  }

  const msg = 'User answered: ' + selected.join(', ');
  const btn = document.getElementById('q-submit-' + reqId);
  respond(reqId, 'deny', btn, msg);
}

fetchPending();
setInterval(fetchPending, 500);
</script>
</body>
</html>"""


def _is_pid_alive(pid):
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class ApprovalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == "/api/pending":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # Run auto-allow check so the UI also reflects latest state
            check_auto_allow()
            requests = []
            for path in sorted(glob.glob(os.path.join(QUEUE_DIR, "*.request.json"))):
                try:
                    with open(path) as f:
                        data = json.load(f)
                    # Skip if response already exists
                    resp_path = path.replace(".request.json", ".response.json")
                    if os.path.exists(resp_path):
                        continue
                    # Skip and cleanup if hook process is dead
                    pid = data.get("pid")
                    if pid and not _is_pid_alive(pid):
                        os.remove(path)
                        continue
                    requests.append(data)
                except (json.JSONDecodeError, IOError):
                    continue
            # Also scan for prompt-waiting markers
            for path in sorted(glob.glob(os.path.join(QUEUE_DIR, "*.prompt-waiting.json"))):
                try:
                    with open(path) as f:
                        data = json.load(f)
                    resp_path = path.replace(".prompt-waiting.json", ".prompt-response.json")
                    if os.path.exists(resp_path):
                        continue
                    pid = data.get("pid")
                    if pid and not _is_pid_alive(pid):
                        os.remove(path)
                        continue
                    requests.append(data)
                except (json.JSONDecodeError, IOError):
                    continue
            self.wfile.write(json.dumps({"requests": requests}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/session-allow":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = str(body.get("session_id", ""))
            tool_name = body.get("tool_name", "")
            request_id = body.get("id", "")
            if sid and tool_name:
                session_auto_allow[(sid, tool_name)] = True
                print(f"[+] Session auto-allow: {tool_name} for session {sid}")
            # Also approve the current request
            if request_id:
                resp_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")
                req_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
                if os.path.exists(req_file):
                    with open(resp_file, "w") as f:
                        json.dump({"decision": "allow"}, f)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/respond":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            request_id = body.get("id", "")
            decision = body.get("decision", "deny")
            message = body.get("message", "")

            # Validate request exists
            request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
            if not os.path.exists(request_file):
                self.send_error(404, "Request not found")
                return

            # If "always", write to settings.local.json
            if decision == "always":
                try:
                    with open(request_file) as f:
                        req_data = json.load(f)
                    settings_file = req_data.get("settings_file", "")
                    # Support multiple patterns (compound Bash commands)
                    allow_patterns = body.get("allow_patterns") or []
                    if not allow_patterns:
                        # Single pattern: client override or from request data
                        allow_pattern = body.get("allow_pattern") or req_data.get("allow_pattern", "")
                        if allow_pattern:
                            allow_patterns = [allow_pattern]
                    if settings_file:
                        for pattern in allow_patterns:
                            self._add_to_settings(settings_file, pattern)
                except (json.JSONDecodeError, IOError):
                    pass

            # Write response file
            response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")
            resp_data = {"decision": decision}
            if message:
                resp_data["message"] = message
            with open(response_file, "w") as f:
                json.dump(resp_data, f)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/submit-prompt":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            request_id = body.get("id", "")
            prompt = body.get("prompt", "")
            waiting_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-waiting.json")
            if not os.path.exists(waiting_file):
                self.send_error(404, "Prompt request not found")
                return
            response_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-response.json")
            with open(response_file, "w") as f:
                json.dump({"action": "submit", "prompt": prompt}, f)
            print(f"[>] Prompt submitted for {request_id}: {prompt[:80]}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/dismiss-prompt":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            request_id = body.get("id", "")
            waiting_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-waiting.json")
            if not os.path.exists(waiting_file):
                self.send_error(404, "Prompt request not found")
                return
            response_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-response.json")
            with open(response_file, "w") as f:
                json.dump({"action": "dismiss"}, f)
            print(f"[x] Prompt dismissed for {request_id}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        else:
            self.send_error(404)

    def _add_to_settings(self, settings_file, pattern):
        """Add an allow pattern to settings.local.json"""
        try:
            if os.path.exists(settings_file):
                with open(settings_file) as f:
                    settings = json.load(f)
            else:
                settings = {"permissions": {"allow": []}}

            if "permissions" not in settings:
                settings["permissions"] = {"allow": []}
            if "allow" not in settings["permissions"]:
                settings["permissions"]["allow"] = []

            if pattern not in settings["permissions"]["allow"]:
                settings["permissions"]["allow"].append(pattern)
                with open(settings_file, "w") as f:
                    json.dump(settings, f, indent=2)
                    f.write("\n")
                print(f"[+] Added to allowlist: {pattern}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[!] Failed to update settings: {e}")


def main():
    os.makedirs(QUEUE_DIR, exist_ok=True)
    # Start background thread for auto-allow checks (independent of web UI)
    t = threading.Thread(target=auto_allow_loop, daemon=True)
    t.start()
    server = HTTPServer(("0.0.0.0", PORT), ApprovalHandler)
    print(f"Claude Code Approval Server running on http://localhost:{PORT}")
    print(f"Watching: {QUEUE_DIR}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
