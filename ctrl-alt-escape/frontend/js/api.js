// api.js — Fetch wrapper with session token, error handling, and connection indicator
// API_BASE_URL is injected by /config.js (served by Netlify or FastAPI).
// Falls back to '' (same-origin) for local dev where FastAPI serves the frontend.
const API_BASE = (typeof window !== 'undefined' && window.API_BASE_URL) ? window.API_BASE_URL : '';


let _session = localStorage.getItem('cae_session') || null;
let _adminToken = sessionStorage.getItem('cae_admin_token') || null;
let _connectionStatus = 'connected'; // connected | reconnecting | offline

function setSession(token) {
  _session = token;
  if (token) localStorage.setItem('cae_session', token);
  else localStorage.removeItem('cae_session');
}

function getSession() { return _session; }

function setAdminToken(token) {
  _adminToken = token;
  if (token) sessionStorage.setItem('cae_admin_token', token);
  else sessionStorage.removeItem('cae_admin_token');
}

function getAdminToken() { return _adminToken; }

function setConnectionStatus(status) {
  if (_connectionStatus === status) return;
  _connectionStatus = status;
  document.dispatchEvent(new CustomEvent('connectionChanged', { detail: { status } }));
}

async function apiFetch(path, options = {}, retries = 2) {
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (_session) headers['X-Session'] = _session;
  if (_adminToken) headers['X-Admin-Token'] = _adminToken;

  const url = API_BASE + path;

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      if (attempt > 0) {
        setConnectionStatus('reconnecting');
        await new Promise(r => setTimeout(r, 1000 * attempt));
      }
      const resp = await fetch(url, { ...options, headers });
      setConnectionStatus('connected');

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: 'SERVER_ERROR', message: resp.statusText }));
        return { ok: false, status: resp.status, error: err.detail || err };
      }
      const data = await resp.json();
      return { ok: true, data };
    } catch (e) {
      if (attempt === retries) {
        setConnectionStatus('offline');
        return { ok: false, error: { error: 'NETWORK_ERROR', message: 'Connection error. Please check your network.' } };
      }
    }
  }
}

function apiGet(path) { return apiFetch(path, { method: 'GET' }); }
function apiPost(path, body) { return apiFetch(path, { method: 'POST', body: JSON.stringify(body || {}) }); }

window.API = { get: apiGet, post: apiPost, setSession, getSession, setAdminToken, getAdminToken };
