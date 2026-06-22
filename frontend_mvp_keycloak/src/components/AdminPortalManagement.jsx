import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import { getToken } from '../lib/keycloak';

function formatDate(value) {
  if (!value) return 'Not available';
  try { return new Date(value).toLocaleString(); } catch { return String(value); }
}

function numberValue(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function statusLabel(status) {
  return String(status || 'unknown').replaceAll('_', ' ');
}

function PortalStatus({ portal }) {
  const status = String(portal.status || 'unknown');
  const run = String(portal.last_run_status || 'never');
  return <div className="portal-statuses">
    <span className={`status-pill portal-status-${status}`}>{statusLabel(status)}</span>
    <span className="portal-run-status">Last run: {statusLabel(run)}</span>
  </div>;
}

export default function AdminPortalManagement() {
  const [portals, setPortals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [busyPortalId, setBusyPortalId] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [form, setForm] = useState({
    display_name: '',
    listing_url: '',
    max_pages_per_run: 50,
    max_jobs_per_run: 500,
    request_rate_limit_per_minute: 30,
    crawl_timeout_seconds: 1800,
  });

  const activeTaskIds = useMemo(
    () => portals.filter((portal) => ['queued', 'running'].includes(String(portal.last_run_status))).map((portal) => portal.id),
    [portals],
  );

  async function loadPortals({ silent = false } = {}) {
    if (!silent) setLoading(true);
    try {
      const result = await api('/admin/job-portals?limit=100', {}, getToken());
      setPortals(result.portals || []);
      setError(null);
    } catch (err) {
      setError(err.message || 'Could not load job portals.');
    } finally {
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => { loadPortals(); }, []);

  useEffect(() => {
    if (!activeTaskIds.length) return undefined;
    const timer = window.setInterval(() => loadPortals({ silent: true }), 10000);
    return () => window.clearInterval(timer);
  }, [activeTaskIds.join('|')]);

  function updateField(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
  }

  async function submit(event) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    if (!form.display_name.trim() || !form.listing_url.trim()) {
      setError('Portal name and job-listing URL are required. Paste a careers or jobs-listing page, not a company homepage.');
      return;
    }
    setSubmitting(true);
    try {
      const result = await api('/admin/job-portals', {
        method: 'POST',
        body: JSON.stringify({
          display_name: form.display_name.trim(),
          listing_url: form.listing_url.trim(),
          max_pages_per_run: numberValue(form.max_pages_per_run, 50),
          max_jobs_per_run: numberValue(form.max_jobs_per_run, 500),
          request_rate_limit_per_minute: numberValue(form.request_rate_limit_per_minute, 30),
          crawl_timeout_seconds: numberValue(form.crawl_timeout_seconds, 1800),
        }),
      }, getToken());
      setNotice(`Portal probe queued. Task: ${result.task_id}`);
      setForm({ display_name: '', listing_url: '', max_pages_per_run: 50, max_jobs_per_run: 500, request_rate_limit_per_minute: 30, crawl_timeout_seconds: 1800 });
      await loadPortals({ silent: true });
    } catch (err) {
      setError(err.message || 'Could not create job portal.');
    } finally {
      setSubmitting(false);
    }
  }

  async function portalAction(portal, action) {
    setBusyPortalId(portal.id);
    setError(null);
    setNotice(null);
    try {
      let result;
      if (action === 'pause') {
        result = await api(`/admin/job-portals/${portal.id}/pause`, { method: 'POST', body: JSON.stringify({}) }, getToken());
        setNotice(`${portal.display_name} is paused.`);
      } else if (action === 'probe') {
        result = await api(`/admin/job-portals/${portal.id}/probe`, { method: 'POST', body: JSON.stringify({}) }, getToken());
        setNotice(`Probe queued for ${portal.display_name}. Task: ${result.task_id}`);
      } else if (action === 'test') {
        result = await api(`/admin/job-portals/${portal.id}/test-scrape`, { method: 'POST', body: JSON.stringify({}) }, getToken());
        setNotice(`Bounded test scrape queued for ${portal.display_name}. Task: ${result.task_id}`);
      } else if (action === 'activate') {
        result = await api(`/admin/job-portals/${portal.id}/activate`, { method: 'POST', body: JSON.stringify({}) }, getToken());
        setNotice(`Portal activated and initial catalog scrape queued. Task: ${result.task_id}`);
      } else if (action === 'run') {
        result = await api(`/admin/job-portals/${portal.id}/run`, { method: 'POST', body: JSON.stringify({}) }, getToken());
        setNotice(`Catalog refresh queued for ${portal.display_name}. Task: ${result.task_id}`);
      }
      await loadPortals({ silent: true });
    } catch (err) {
      setError(err.message || 'Portal action failed.');
    } finally {
      setBusyPortalId(null);
    }
  }

  return <section className="portal-management">
    <section className="card">
      <div className="card-header">
        <div>
          <h2>Add Job Portal</h2>
          <p className="muted">Paste a public careers or jobs-listing URL. Job Miner validates the URL, probes it in the background, detects a compatible crawl profile, and requires a bounded test scrape before a full catalog crawl.</p>
        </div>
      </div>
      <form className="portal-form" onSubmit={submit}>
        <label>
          <span>Portal name</span>
          <input value={form.display_name} onChange={(event) => updateField('display_name', event.target.value)} placeholder="Example: Acme Careers" disabled={submitting} maxLength="160" />
        </label>
        <label className="portal-form-url">
          <span>Public job-listing URL</span>
          <input value={form.listing_url} onChange={(event) => updateField('listing_url', event.target.value)} placeholder="https://careers.example.com/jobs" disabled={submitting} type="url" />
        </label>
        <label>
          <span>Max pages per run</span>
          <input value={form.max_pages_per_run} onChange={(event) => updateField('max_pages_per_run', event.target.value)} disabled={submitting} type="number" min="1" max="250" />
        </label>
        <label>
          <span>Max jobs per run</span>
          <input value={form.max_jobs_per_run} onChange={(event) => updateField('max_jobs_per_run', event.target.value)} disabled={submitting} type="number" min="1" max="2000" />
        </label>
        <label>
          <span>Requests/minute</span>
          <input value={form.request_rate_limit_per_minute} onChange={(event) => updateField('request_rate_limit_per_minute', event.target.value)} disabled={submitting} type="number" min="1" max="120" />
        </label>
        <label>
          <span>Timeout seconds</span>
          <input value={form.crawl_timeout_seconds} onChange={(event) => updateField('crawl_timeout_seconds', event.target.value)} disabled={submitting} type="number" min="30" max="7200" />
        </label>
        <div className="portal-form-submit">
          <button className="button" type="submit" disabled={submitting}>{submitting ? 'Queueing portal probe...' : 'Add and probe portal'}</button>
        </div>
      </form>
      <p className="portal-security-note">For safety, local/private network URLs, URLs with embedded credentials, and non-standard ports are rejected. A successful detection is not enough to activate a source: a limited test scrape must extract at least one valid job.</p>
      {error && <p className="error">{error}</p>}
      {notice && <p className="info-banner">{notice}</p>}
    </section>

    <section className="card">
      <div className="portal-list-heading">
        <div>
          <h2>Configured Job Portals</h2>
          <p className="muted">Portal configuration is stored in PostgreSQL. Jobs are written to MongoDB only after a successful active scrape.</p>
        </div>
        <button className="button secondary" onClick={() => loadPortals()} disabled={loading}>Refresh</button>
      </div>
      {loading ? <div className="empty-state">Loading job portals...</div> : !portals.length ? <div className="empty-state">No job portals configured yet.</div> : <div className="portal-list">
        {portals.map((portal) => {
          const busy = busyPortalId === portal.id || ['queued', 'running'].includes(String(portal.last_run_status));
          const lastProbe = portal.metadata?.last_probe || {};
          const detected = lastProbe.detected || {};
          const canTest = ['ready_for_test', 'needs_review'].includes(String(portal.status)) && !detected.blocked;
          const canActivate = String(portal.status) === 'ready_for_activation';
          const isActive = String(portal.status) === 'active' && Boolean(portal.is_active);
          return <article className="portal-card" key={portal.id}>
            <div className="portal-card-head">
              <div>
                <h3>{portal.display_name}</h3>
                <a href={portal.canonical_listing_url || portal.listing_url} target="_blank" rel="noreferrer">{portal.canonical_listing_url || portal.listing_url}</a>
              </div>
              <PortalStatus portal={portal} />
            </div>
            <div className="portal-meta-grid">
              <div><span>Platform</span><strong>{portal.source_platform || 'Pending detection'}</strong></div>
              <div><span>Crawl profile</span><strong>{portal.profile_name || 'Pending detection'}</strong></div>
              <div><span>Detection confidence</span><strong>{portal.source_platform_confidence == null ? 'Not available' : `${Math.round(Number(portal.source_platform_confidence) * 100)}%`}</strong></div>
              <div><span>Discovered in probe</span><strong>{lastProbe.discovered_urls ?? 'Not available'}</strong></div>
              <div><span>Last successful run</span><strong>{formatDate(portal.last_successful_run_on)}</strong></div>
              <div><span>Failure streak</span><strong>{portal.failure_streak || 0}</strong></div>
            </div>
            {Array.isArray(detected.reasons) && detected.reasons.length > 0 && <p className="portal-detection-reason">{detected.reasons.join(' ')}</p>}
            {portal.latest_pipeline_metrics?.ingestion && <p className="portal-ingestion-summary">Last ingestion: {portal.latest_pipeline_metrics.ingestion.inserted || 0} new, {portal.latest_pipeline_metrics.ingestion.changed || 0} updated, {portal.latest_pipeline_metrics.ingestion.unchanged || 0} unchanged.</p>}
            <div className="portal-actions">
              {!isActive && <button className="button secondary" onClick={() => portalAction(portal, 'probe')} disabled={busy || portal.status === 'blocked'}>Probe again</button>}
              {canTest && <button className="button secondary" onClick={() => portalAction(portal, 'test')} disabled={busy}>Run test scrape</button>}
              {canActivate && <button className="button success" onClick={() => portalAction(portal, 'activate')} disabled={busy}>Activate and initial scrape</button>}
              {isActive && <button className="button" onClick={() => portalAction(portal, 'run')} disabled={busy}>Run catalog refresh</button>}
              {isActive && <button className="button danger" onClick={() => portalAction(portal, 'pause')} disabled={busy}>Pause</button>}
            </div>
          </article>;
        })}
      </div>}
    </section>
  </section>;
}
