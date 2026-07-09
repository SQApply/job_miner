import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import { getToken } from '../lib/keycloak';

const SUPPORTED_PROFILES = [
  'generic_listing', 'paginated_anchor', 'paginated_url_param', 'load_more_button',
  'infinite_scroll', 'hash_route_spa', 'detail_button_capture', 'modal_detail',
  'search_first', 'workday', 'jobdiva',
];

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

function compactJson(value) {
  try { return JSON.stringify(value || {}, null, 2); } catch { return '{}'; }
}

export default function AdminPortalManagement() {
  const [portals, setPortals] = useState([]);
  const [health, setHealth] = useState([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [busyPortalId, setBusyPortalId] = useState(null);
  const [editingPortalId, setEditingPortalId] = useState(null);
  const [overridePortalId, setOverridePortalId] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [form, setForm] = useState({
    display_name: '',
    listing_url: '',
    max_pages_per_run: 50,
    max_jobs_per_run: 500,
    request_rate_limit_per_minute: 30,
    crawl_timeout_seconds: 1800,
    scheduler_enabled: false,
    refresh_interval_minutes: 1440,
    deactivate_after_misses: 2,
    min_discovery_coverage_ratio: 0.25,
    max_consecutive_failures_before_pause: 5,
    detail_retry_attempts: 2,
  });
  const [editForm, setEditForm] = useState({});
  const [overrideForm, setOverrideForm] = useState({ profile_name: 'generic_listing', source_platform: 'custom_listing', crawl_strategy: 'generic_listing', profile_overrides: '{}', notes: '' });

  const activeTaskIds = useMemo(
    () => portals.filter((portal) => ['queued', 'running'].includes(String(portal.last_run_status))).map((portal) => portal.id),
    [portals],
  );

  async function loadPortals({ silent = false } = {}) {
    if (!silent) setLoading(true);
    try {
      const [portalResult, healthResult] = await Promise.all([
        api('/admin/job-portals?limit=100', {}, getToken()),
        api('/admin/job-portals/health/summary?limit=200', {}, getToken()).catch(() => ({ portals: [] })),
      ]);
      setPortals(portalResult.portals || []);
      setHealth(healthResult.portals || []);
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

  function portalPayload(source) {
    return {
      display_name: source.display_name?.trim(),
      listing_url: source.listing_url?.trim(),
      max_pages_per_run: numberValue(source.max_pages_per_run, 50),
      max_jobs_per_run: numberValue(source.max_jobs_per_run, 500),
      request_rate_limit_per_minute: numberValue(source.request_rate_limit_per_minute, 30),
      crawl_timeout_seconds: numberValue(source.crawl_timeout_seconds, 1800),
      scheduler_enabled: Boolean(source.scheduler_enabled),
      refresh_interval_minutes: Boolean(source.scheduler_enabled) ? numberValue(source.refresh_interval_minutes, 1440) : null,
      deactivate_after_misses: numberValue(source.deactivate_after_misses, 2),
      min_discovery_coverage_ratio: Number(source.min_discovery_coverage_ratio || 0.25),
      max_consecutive_failures_before_pause: numberValue(source.max_consecutive_failures_before_pause, 5),
      detail_retry_attempts: numberValue(source.detail_retry_attempts, 2),
    };
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
        body: JSON.stringify(portalPayload(form)),
      }, getToken());
      setNotice(`Portal probe queued. Task: ${result.task_id}`);
      setForm({ display_name: '', listing_url: '', max_pages_per_run: 50, max_jobs_per_run: 500, request_rate_limit_per_minute: 30, crawl_timeout_seconds: 1800, scheduler_enabled: false, refresh_interval_minutes: 1440, deactivate_after_misses: 2, min_discovery_coverage_ratio: 0.25, max_consecutive_failures_before_pause: 5, detail_retry_attempts: 2 });
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
      } else if (action === 'scheduler') {
        result = await api('/admin/job-portals/scheduler/run', { method: 'POST', body: JSON.stringify({ limit: 25 }) }, getToken());
        setNotice(`Portal scheduler run queued. Task: ${result.task_id}`);
      }
      await loadPortals({ silent: true });
    } catch (err) {
      setError(err.message || 'Portal action failed.');
    } finally {
      setBusyPortalId(null);
    }
  }

  function startEdit(portal) {
    setEditingPortalId(portal.id);
    setEditForm({
      display_name: portal.display_name || '',
      listing_url: portal.listing_url || '',
      max_pages_per_run: portal.max_pages_per_run || 50,
      max_jobs_per_run: portal.max_jobs_per_run || 500,
      request_rate_limit_per_minute: portal.request_rate_limit_per_minute || 30,
      crawl_timeout_seconds: portal.crawl_timeout_seconds || 1800,
      scheduler_enabled: Boolean(portal.scheduler_enabled),
      refresh_interval_minutes: portal.refresh_interval_minutes || 1440,
      deactivate_after_misses: portal.deactivate_after_misses || 2,
      min_discovery_coverage_ratio: portal.min_discovery_coverage_ratio || 0.25,
      max_consecutive_failures_before_pause: portal.max_consecutive_failures_before_pause || 5,
      detail_retry_attempts: portal.detail_retry_attempts || 2,
      configuration_version: portal.configuration_version,
    });
  }

  async function saveEdit(portal) {
    setBusyPortalId(portal.id);
    try {
      await api(`/admin/job-portals/${portal.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...portalPayload(editForm), configuration_version: editForm.configuration_version }),
      }, getToken());
      setNotice(`${portal.display_name} settings updated.`);
      setEditingPortalId(null);
      await loadPortals({ silent: true });
    } catch (err) {
      setError(err.message || 'Could not update portal.');
    } finally {
      setBusyPortalId(null);
    }
  }

  function startOverride(portal) {
    setOverridePortalId(portal.id);
    setOverrideForm({
      profile_name: portal.profile_name || 'generic_listing',
      source_platform: portal.source_platform || 'custom_listing',
      crawl_strategy: portal.crawl_strategy || portal.profile_name || 'generic_listing',
      profile_overrides: compactJson(portal.configuration_json?.profile_overrides || {}),
      notes: '',
      configuration_version: portal.configuration_version,
    });
  }

  async function saveOverride(portal) {
    setBusyPortalId(portal.id);
    try {
      let parsedOverrides = {};
      if (overrideForm.profile_overrides?.trim()) parsedOverrides = JSON.parse(overrideForm.profile_overrides);
      await api(`/admin/job-portals/${portal.id}/override`, {
        method: 'POST',
        body: JSON.stringify({
          profile_name: overrideForm.profile_name,
          source_platform: overrideForm.source_platform,
          crawl_strategy: overrideForm.crawl_strategy || overrideForm.profile_name,
          profile_overrides: parsedOverrides,
          notes: overrideForm.notes,
          configuration_version: overrideForm.configuration_version,
        }),
      }, getToken());
      setNotice(`${portal.display_name} crawler override saved. Run a test scrape before activation.`);
      setOverridePortalId(null);
      await loadPortals({ silent: true });
    } catch (err) {
      setError(err.message || 'Could not save override. Make sure profile overrides are valid JSON.');
    } finally {
      setBusyPortalId(null);
    }
  }

  const healthTotals = health.reduce((acc, portal) => {
    const counts = portal.job_counts || {};
    acc.activeJobs += Number(counts.active || 0);
    acc.inactiveJobs += Number(counts.inactive || 0);
    if (String(portal.last_health_status) === 'failing' || String(portal.last_health_status) === 'auto_paused') acc.failing += 1;
    if (portal.scheduler_enabled) acc.scheduled += 1;
    return acc;
  }, { activeJobs: 0, inactiveJobs: 0, failing: 0, scheduled: 0 });

  return <section className="portal-management">
    <section className="card">
      <div className="card-header">
        <div>
          <h2>Add Job Portal</h2>
          <p className="muted">Paste a public careers or jobs-listing URL. Job Miner validates the URL, probes it in the background, detects a compatible crawl profile, and requires a bounded test scrape before a full catalog crawl.</p>
        </div>
      </div>
      <form className="portal-form portal-form-expanded" onSubmit={submit}>
        <label>
          <span>Portal name</span>
          <input value={form.display_name} onChange={(event) => updateField('display_name', event.target.value)} placeholder="Example: Acme Careers" disabled={submitting} maxLength="160" />
        </label>
        <label className="portal-form-url">
          <span>Public job-listing URL</span>
          <input value={form.listing_url} onChange={(event) => updateField('listing_url', event.target.value)} placeholder="https://careers.example.com/jobs" disabled={submitting} type="url" />
        </label>
        <label><span>Max pages</span><input value={form.max_pages_per_run} onChange={(event) => updateField('max_pages_per_run', event.target.value)} disabled={submitting} type="number" min="1" max="250" /></label>
        <label><span>Max jobs</span><input value={form.max_jobs_per_run} onChange={(event) => updateField('max_jobs_per_run', event.target.value)} disabled={submitting} type="number" min="1" max="2000" /></label>
        <label><span>Requests/min</span><input value={form.request_rate_limit_per_minute} onChange={(event) => updateField('request_rate_limit_per_minute', event.target.value)} disabled={submitting} type="number" min="1" max="120" /></label>
        <label><span>Timeout sec</span><input value={form.crawl_timeout_seconds} onChange={(event) => updateField('crawl_timeout_seconds', event.target.value)} disabled={submitting} type="number" min="30" max="7200" /></label>
        <label><span>Auto refresh</span><select value={form.scheduler_enabled ? 'yes' : 'no'} onChange={(event) => updateField('scheduler_enabled', event.target.value === 'yes')} disabled={submitting}><option value="no">Disabled</option><option value="yes">Enabled</option></select></label>
        <label><span>Refresh mins</span><input value={form.refresh_interval_minutes} onChange={(event) => updateField('refresh_interval_minutes', event.target.value)} disabled={submitting || !form.scheduler_enabled} type="number" min="15" max="10080" /></label>
        <label><span>Deactivate after misses</span><input value={form.deactivate_after_misses} onChange={(event) => updateField('deactivate_after_misses', event.target.value)} disabled={submitting} type="number" min="1" max="10" /></label>
        <label><span>Coverage guard</span><input value={form.min_discovery_coverage_ratio} onChange={(event) => updateField('min_discovery_coverage_ratio', event.target.value)} disabled={submitting} type="number" min="0.05" max="1" step="0.05" /></label>
        <label><span>Auto-pause failures</span><input value={form.max_consecutive_failures_before_pause} onChange={(event) => updateField('max_consecutive_failures_before_pause', event.target.value)} disabled={submitting} type="number" min="1" max="20" /></label>
        <label><span>Detail retries</span><input value={form.detail_retry_attempts} onChange={(event) => updateField('detail_retry_attempts', event.target.value)} disabled={submitting} type="number" min="0" max="5" /></label>
        <div className="portal-form-submit"><button className="button" type="submit" disabled={submitting}>{submitting ? 'Queueing portal probe...' : 'Add and probe portal'}</button></div>
      </form>
      <p className="portal-security-note">Old jobs are never physically deleted by refreshes. After safe successive misses, they are marked inactive and candidate catalog/recommendations ignore them.</p>
      {error && <p className="error">{error}</p>}
      {notice && <p className="info-banner">{notice}</p>}
    </section>

    <section className="card">
      <div className="portal-list-heading">
        <div>
          <h2>Portal Health</h2>
          <p className="muted">Operational view for scheduled scraping, active jobs, failures, and deactivation safety.</p>
        </div>
        <button className="button secondary" onClick={() => portalAction({ id: 'scheduler', display_name: 'scheduler' }, 'scheduler')} disabled={Boolean(busyPortalId)}>Run scheduler once</button>
      </div>
      <div className="portal-health-grid">
        <div><span>Configured portals</span><strong>{health.length}</strong></div>
        <div><span>Scheduled portals</span><strong>{healthTotals.scheduled}</strong></div>
        <div><span>Failing portals</span><strong>{healthTotals.failing}</strong></div>
        <div><span>Active jobs</span><strong>{healthTotals.activeJobs}</strong></div>
        <div><span>Inactive jobs</span><strong>{healthTotals.inactiveJobs}</strong></div>
      </div>
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
          const counts = health.find((item) => item.id === portal.id)?.job_counts || {};
          const editing = editingPortalId === portal.id;
          const overriding = overridePortalId === portal.id;
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
              <div><span>Next scheduled run</span><strong>{portal.scheduler_enabled ? formatDate(portal.next_run_at) : 'Disabled'}</strong></div>
              <div><span>Failure streak</span><strong>{portal.failure_streak || 0}</strong></div>
              <div><span>Active jobs</span><strong>{counts.active ?? 'Not available'}</strong></div>
              <div><span>Inactive jobs</span><strong>{counts.inactive ?? 0}</strong></div>
              <div><span>Health</span><strong>{statusLabel(portal.last_health_status || 'unknown')}</strong></div>
            </div>
            {Array.isArray(detected.reasons) && detected.reasons.length > 0 && <p className="portal-detection-reason">{detected.reasons.join(' ')}</p>}
            {portal.latest_pipeline_metrics?.ingestion && <p className="portal-ingestion-summary">Last ingestion: {portal.latest_pipeline_metrics.ingestion.inserted || 0} new, {portal.latest_pipeline_metrics.ingestion.changed || 0} updated, {portal.latest_pipeline_metrics.ingestion.unchanged || 0} unchanged. Deactivated: {portal.latest_pipeline_metrics.lifecycle_reconcile?.deactivated || 0}.</p>}
            {portal.alert_status && <p className="portal-alert-note">Alert: {statusLabel(portal.alert_status)} at {formatDate(portal.last_alert_on)}</p>}

            {editing && <div className="portal-inline-editor">
              <label><span>Name</span><input value={editForm.display_name} onChange={(event) => setEditForm({ ...editForm, display_name: event.target.value })} /></label>
              <label className="portal-form-url"><span>URL</span><input value={editForm.listing_url} onChange={(event) => setEditForm({ ...editForm, listing_url: event.target.value })} /></label>
              <label><span>Auto refresh</span><select value={editForm.scheduler_enabled ? 'yes' : 'no'} onChange={(event) => setEditForm({ ...editForm, scheduler_enabled: event.target.value === 'yes' })}><option value="no">Disabled</option><option value="yes">Enabled</option></select></label>
              <label><span>Refresh mins</span><input type="number" min="15" max="10080" value={editForm.refresh_interval_minutes} onChange={(event) => setEditForm({ ...editForm, refresh_interval_minutes: event.target.value })} disabled={!editForm.scheduler_enabled} /></label>
              <label><span>Deactivate misses</span><input type="number" min="1" max="10" value={editForm.deactivate_after_misses} onChange={(event) => setEditForm({ ...editForm, deactivate_after_misses: event.target.value })} /></label>
              <label><span>Coverage guard</span><input type="number" min="0.05" max="1" step="0.05" value={editForm.min_discovery_coverage_ratio} onChange={(event) => setEditForm({ ...editForm, min_discovery_coverage_ratio: event.target.value })} /></label>
              <label><span>Failure auto-pause</span><input type="number" min="1" max="20" value={editForm.max_consecutive_failures_before_pause} onChange={(event) => setEditForm({ ...editForm, max_consecutive_failures_before_pause: event.target.value })} /></label>
              <label><span>Detail retries</span><input type="number" min="0" max="5" value={editForm.detail_retry_attempts} onChange={(event) => setEditForm({ ...editForm, detail_retry_attempts: event.target.value })} /></label>
              <div className="portal-actions"><button className="button" onClick={() => saveEdit(portal)} disabled={busy}>Save settings</button><button className="button secondary" onClick={() => setEditingPortalId(null)} disabled={busy}>Cancel</button></div>
            </div>}

            {overriding && <div className="portal-inline-editor">
              <label><span>Profile</span><select value={overrideForm.profile_name} onChange={(event) => setOverrideForm({ ...overrideForm, profile_name: event.target.value, crawl_strategy: event.target.value })}>{SUPPORTED_PROFILES.map((profile) => <option key={profile} value={profile}>{profile}</option>)}</select></label>
              <label><span>Source platform</span><input value={overrideForm.source_platform} onChange={(event) => setOverrideForm({ ...overrideForm, source_platform: event.target.value })} /></label>
              <label><span>Crawl strategy</span><input value={overrideForm.crawl_strategy} onChange={(event) => setOverrideForm({ ...overrideForm, crawl_strategy: event.target.value })} /></label>
              <label className="portal-textarea-label"><span>Profile overrides JSON</span><textarea rows="6" value={overrideForm.profile_overrides} onChange={(event) => setOverrideForm({ ...overrideForm, profile_overrides: event.target.value })} /></label>
              <label className="portal-textarea-label"><span>Notes</span><textarea rows="3" value={overrideForm.notes} onChange={(event) => setOverrideForm({ ...overrideForm, notes: event.target.value })} /></label>
              <div className="portal-actions"><button className="button" onClick={() => saveOverride(portal)} disabled={busy}>Save override</button><button className="button secondary" onClick={() => setOverridePortalId(null)} disabled={busy}>Cancel</button></div>
            </div>}

            <div className="portal-actions">
              {!isActive && <button className="button secondary" onClick={() => portalAction(portal, 'probe')} disabled={busy || portal.status === 'blocked'}>Probe again</button>}
              {canTest && <button className="button secondary" onClick={() => portalAction(portal, 'test')} disabled={busy}>Run test scrape</button>}
              {canActivate && <button className="button success" onClick={() => portalAction(portal, 'activate')} disabled={busy}>Activate and initial scrape</button>}
              {isActive && <button className="button" onClick={() => portalAction(portal, 'run')} disabled={busy}>Run catalog refresh</button>}
              {isActive && <button className="button danger" onClick={() => portalAction(portal, 'pause')} disabled={busy}>Pause</button>}
              <button className="button secondary" onClick={() => startEdit(portal)} disabled={busy}>Edit production controls</button>
              <button className="button secondary" onClick={() => startOverride(portal)} disabled={busy}>Override profile</button>
            </div>
          </article>;
        })}
      </div>}
    </section>
  </section>;
}
