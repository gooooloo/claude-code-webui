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
import subprocess
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import uuid
import cgi

QUEUE_DIR = "/tmp/claude-approvals"
IMAGE_DIR = "/tmp/claude-images"
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
  .card.cat-web      { border-left-color: #3b82f6; }
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
  .badge-web      { background: #3b82f622; color: #3b82f6; }
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
    margin-bottom: 18px;
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
  .image-thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .image-thumb .remove-btn {
    position: absolute;
    top: -4px;
    right: -4px;
    width: 18px;
    height: 18px;
    background: #ef4444;
    color: white;
    border: none;
    border-radius: 50%;
    font-size: 11px;
    line-height: 18px;
    text-align: center;
    cursor: pointer;
    padding: 0;
  }
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
  if (name === 'WebFetch' || name === 'WebSearch') return 'web';
  return 'other';
}

function isBenign(cat) {
  return cat === 'plan' || cat === 'question' || cat === 'read' || cat === 'web';
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
  let s = esc(text.trim());
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
      <div class="detail" id="prompt-response-${req.id}">${renderMarkdown(req.last_response)}</div>`;
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
      <button class="btn-quick" onclick="quickPrompt('${req.id}','Commit the current changes and push')">Wrap up this task</button>
      <button class="btn-quick" onclick="quickPrompt('${req.id}','/clear')">/clear</button>
      <button class="btn-quick" onclick="quickPrompt('${req.id}','Implement the next TODO item from PRD.md')">Next TODO</button>
    </div>
    <div class="image-upload-area">
      <input type="file" id="image-file-${req.id}" accept="image/*" style="display:none" onchange="handleImageFile('${req.id}',this)">
      <button class="btn-upload-image" onclick="document.getElementById('image-file-${req.id}').click()">+ Image</button>
      <div class="image-preview-area" id="image-preview-${req.id}"></div>
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

const imagePathsMap = {};

async function handleImageFile(reqId, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  await uploadImage(reqId, file);
  input.value = '';
}

async function uploadImage(reqId, file) {
  const formData = new FormData();
  formData.append('image', file);
  try {
    const resp = await fetch('/api/upload-image', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.path) {
      if (!imagePathsMap[reqId]) imagePathsMap[reqId] = [];
      imagePathsMap[reqId].push(data.path);
      renderImagePreviews(reqId);
    }
  } catch (e) {
    console.error('Image upload failed:', e);
  }
}

function renderImagePreviews(reqId) {
  const area = document.getElementById('image-preview-' + reqId);
  if (!area) return;
  const paths = imagePathsMap[reqId] || [];
  area.innerHTML = paths.map((p, i) =>
    '<div class="image-thumb">' +
    '<img src="/api/image?path=' + encodeURIComponent(p) + '">' +
    '<button class="remove-btn" onclick="removeImage(\\'' + reqId + '\\',' + i + ')">x</button>' +
    '</div>'
  ).join('');
}

function removeImage(reqId, index) {
  if (imagePathsMap[reqId]) {
    imagePathsMap[reqId].splice(index, 1);
    renderImagePreviews(reqId);
  }
}

async function submitPrompt(id) {
  const input = document.getElementById('prompt-input-' + id);
  let prompt = input ? input.value.trim() : '';
  const images = imagePathsMap[id] || [];
  if (!prompt && images.length === 0) { if (input) input.focus(); return; }
  // Prepend image file path references
  if (images.length > 0) {
    const imgRefs = images.map(p => 'Please look at this image: ' + p).join('\\n');
    prompt = imgRefs + (prompt ? '\\n\\n' + prompt : '');
  }
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/submit-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, prompt})
    });
    delete imagePathsMap[id];
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
        // Setup paste handler for image paste
        const ta = card.querySelector('.prompt-input');
        if (ta) {
          ta.addEventListener('paste', (e) => {
            const items = e.clipboardData && e.clipboardData.items;
            if (!items) return;
            for (const item of items) {
              if (item.type.startsWith('image/')) {
                e.preventDefault();
                uploadImage(req.id, item.getAsFile());
                return;
              }
            }
          });
        }
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
  // Always include project root as first option
  const rootPattern = toolName + '(' + projectDir + '/*)';
  html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(rootPattern).replace(/'/g, "\\\\'") + '\\')">'
    + '<div class="path-label">' + esc(projectName + '/*') + '</div>'
    + '<div class="path-pattern">' + esc(rootPattern) + '</div>'
    + '</div>';
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


def _is_tmux_pane_alive(pane_id):
    """Check if a tmux pane still exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", pane_id],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _tmux_send_prompt(pane_id, prompt):
    """Send a prompt to a tmux pane using load-buffer + paste-buffer + Enter."""
    try:
        # Load prompt into tmux buffer via stdin
        subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=prompt.encode(), capture_output=True, timeout=5
        )
        # Paste buffer into the target pane and delete the buffer
        subprocess.run(
            ["tmux", "paste-buffer", "-t", pane_id, "-d"],
            capture_output=True, timeout=5
        )
        # Send Enter to submit
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            capture_output=True, timeout=5
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
                    # Liveness check: tmux mode checks pane, non-tmux checks pid
                    if data.get("tmux_mode"):
                        tmux_pane = data.get("tmux_pane", "")
                        if tmux_pane and not _is_tmux_pane_alive(tmux_pane):
                            os.remove(path)
                            continue
                    else:
                        pid = data.get("pid")
                        if pid and not _is_pid_alive(pid):
                            os.remove(path)
                            continue
                    requests.append(data)
                except (json.JSONDecodeError, IOError):
                    continue
            self.wfile.write(json.dumps({"requests": requests}).encode())
        elif self.path.startswith("/api/image"):
            # Serve uploaded image for preview
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            img_path = params.get("path", [""])[0]
            if not img_path or not img_path.startswith(IMAGE_DIR) or not os.path.isfile(img_path):
                self.send_error(404)
                return
            ext = os.path.splitext(img_path)[1].lower()
            content_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
            ct = content_types.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            with open(img_path, "rb") as f:
                self.wfile.write(f.read())
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
            # Read waiting file to check tmux mode
            try:
                with open(waiting_file) as f:
                    waiting_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                waiting_data = {}
            if waiting_data.get("tmux_mode"):
                # Tmux mode: send prompt via tmux send-keys
                tmux_pane = waiting_data.get("tmux_pane", "")
                if tmux_pane and _tmux_send_prompt(tmux_pane, prompt):
                    # Clean up waiting file (UserPromptSubmit hook also cleans as backup)
                    try:
                        os.remove(waiting_file)
                    except OSError:
                        pass
                    print(f"[>] Prompt sent via tmux to {tmux_pane}: {prompt[:80]}")
                else:
                    self.send_error(500, "Failed to send prompt via tmux")
                    return
            else:
                # Non-tmux mode: write response file for hook to pick up
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
            # Read waiting file to check tmux mode
            try:
                with open(waiting_file) as f:
                    waiting_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                waiting_data = {}
            if waiting_data.get("tmux_mode"):
                # Tmux mode: just remove waiting file (Claude stays at > prompt)
                try:
                    os.remove(waiting_file)
                except OSError:
                    pass
            else:
                # Non-tmux mode: write dismiss response for hook to pick up
                response_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-response.json")
                with open(response_file, "w") as f:
                    json.dump({"action": "dismiss"}, f)
            print(f"[x] Prompt dismissed for {request_id}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/session-reset":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = str(body.get("session_id", ""))
            source = body.get("source", "unknown")
            if not sid:
                self.send_error(400, "Missing session_id")
                return
            # Clear session auto-allow rules for this session
            keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
            for k in keys_to_remove:
                del session_auto_allow[k]
            if keys_to_remove:
                print(f"[~] Cleared {len(keys_to_remove)} session auto-allow rule(s) for session {sid}")
            # Write deny responses for pending requests of this session (so polling hooks exit cleanly)
            for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
                resp_path = path.replace(".request.json", ".response.json")
                if os.path.exists(resp_path):
                    continue
                try:
                    with open(path) as f:
                        data = json.load(f)
                    if str(data.get("session_id", "")) == sid:
                        with open(resp_path, "w") as f:
                            json.dump({"decision": "deny", "message": "Session reset"}, f)
                        print(f"[~] Denied stale request {data.get('id', '?')} (session reset)")
                except (json.JSONDecodeError, IOError):
                    continue
            # Write dismiss responses for prompt-waiting files of this session
            for path in glob.glob(os.path.join(QUEUE_DIR, "*.prompt-waiting.json")):
                resp_path = path.replace(".prompt-waiting.json", ".prompt-response.json")
                if os.path.exists(resp_path):
                    continue
                try:
                    with open(path) as f:
                        data = json.load(f)
                    if str(data.get("session_id", "")) == sid:
                        with open(resp_path, "w") as f:
                            json.dump({"action": "dismiss"}, f)
                        print(f"[~] Dismissed stale prompt {data.get('id', '?')} (session reset)")
                except (json.JSONDecodeError, IOError):
                    continue
            print(f"[*] Session reset: session={sid} source={source}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/session-end":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            sid = str(body.get("session_id", ""))
            if not sid:
                self.send_error(400, "Missing session_id")
                return
            # Clear session auto-allow rules
            keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
            for k in keys_to_remove:
                del session_auto_allow[k]
            if keys_to_remove:
                print(f"[~] Cleared {len(keys_to_remove)} session auto-allow rule(s) for session {sid}")
            # Delete all request/response files for this session (no hooks are polling anymore)
            deleted = 0
            for pattern, resp_suffix in [("*.request.json", ".response.json"), ("*.prompt-waiting.json", ".prompt-response.json")]:
                for path in glob.glob(os.path.join(QUEUE_DIR, pattern)):
                    try:
                        with open(path) as f:
                            data = json.load(f)
                        if str(data.get("session_id", "")) == sid:
                            resp_path = path.replace(pattern.lstrip("*"), resp_suffix)
                            rm_count = 0
                            if os.path.exists(path):
                                os.remove(path)
                                rm_count += 1
                            if os.path.exists(resp_path):
                                os.remove(resp_path)
                                rm_count += 1
                            deleted += rm_count
                    except (json.JSONDecodeError, IOError):
                        continue
            print(f"[*] Session end: session={sid}, deleted {deleted} file(s)")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        elif self.path == "/api/upload-image":
            os.makedirs(IMAGE_DIR, exist_ok=True)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type}
                )
                file_item = form["image"]
                if file_item.filename:
                    ext = os.path.splitext(file_item.filename)[1].lower() or ".png"
                    filename = str(uuid.uuid4()) + ext
                    filepath = os.path.join(IMAGE_DIR, filename)
                    with open(filepath, "wb") as f:
                        f.write(file_item.file.read())
                    print(f"[img] Saved image: {filepath}")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "path": filepath}).encode())
                else:
                    self.send_error(400, "No file uploaded")
            else:
                self.send_error(400, "Expected multipart/form-data")
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
