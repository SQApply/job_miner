import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api } from './lib/api';
import { getToken, initKeycloak, logout } from './lib/keycloak';
import './styles.css';

function Button({ children, onClick, disabled, variant = 'primary', type = 'button', href }) {
  if (href) {
    return <a className={`button ${variant}`} href={href} target="_blank" rel="noreferrer" onClick={onClick}>{children}</a>;
  }
  return <button type={type} className={`button ${variant}`} onClick={onClick} disabled={disabled}>{children}</button>;
}

function Card({ title, subtitle, children, className = '' }) {
  return <section className={`card ${className}`}>
    {(title || subtitle) && <div className="card-header">
      <div>
        {title && <h2>{title}</h2>}
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
    </div>}
    {children}
  </section>;
}

function EmptyState({ message }) {
  return <div className="empty-state">{message}</div>;
}

function Field({ label, value }) {
  const display = value === undefined || value === null || value === '' ? 'Not available' : String(value);
  return <div className="info-item">
    <div className="info-label">{label}</div>
    <div className="info-value">{display}</div>
  </div>;
}

function Chips({ items = [], max = 14 }) {
  const clean = (items || []).filter(Boolean).map(String).filter(Boolean).slice(0, max);
  if (!clean.length) return <span className="muted">Not available</span>;
  return <div className="chips">{clean.map((item, index) => <span key={`${item}-${index}`} className="chip">{item}</span>)}</div>;
}

function valueFrom(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return null;
}

function firstNonEmptyArray(...values) {
  for (const value of values) {
    if (Array.isArray(value) && value.length > 0) return value;
  }
  return [];
}

function getJobTitle(item) {
  const raw = valueFrom(item?.title, item?.job?.title, item?.match?.title, item?.match?.job?.title, 'Untitled job');
  return cleanJobTitle(String(raw), getCompany(item));
}

function cleanJobTitle(title, company) {
  if (!title) return 'Untitled job';
  const parts = title.split(' | ').map((part) => part.trim()).filter(Boolean);
  if (parts.length > 1) {
    const last = parts[parts.length - 1].toLowerCase();
    const companyText = String(company || '').toLowerCase();
    if (!company || companyText.includes(last) || last.includes(companyText) || last.includes('judge group')) {
      return parts.slice(0, -1).join(' | ');
    }
  }
  return title;
}

function getCompany(item) {
  return valueFrom(item?.company, item?.job?.company, item?.match?.company, item?.match?.job?.company, 'Company not available');
}

function getLocation(item) {
  return valueFrom(item?.location_text, item?.job?.location_text, item?.match?.location_text, item?.match?.job?.location_text, 'Location not available');
}

function getJobId(item) {
  return valueFrom(item?.job_id, item?.job?.job_id, item?.match?.job_id, item?.match?.job?.job_id);
}

function getApplyUrl(item) {
  return valueFrom(
    item?.apply_url,
    item?.job_url,
    item?.job?.apply_url,
    item?.job?.job_url,
    item?.match?.apply_url,
    item?.match?.job_url,
    item?.match?.job?.apply_url,
    item?.match?.job?.job_url
  );
}

function getMatchRunId(item) {
  return valueFrom(item?.match_run_id, item?.match?.match_run_id);
}

function getSourceCollection(item) {
  return valueFrom(item?.source_collection, item?.match?.source_collection);
}

function isFallbackRecommendation(item) {
  const evidence = item?.evidence || item?.match?.evidence || {};
  const decision = String(valueFrom(item?.llm_decision, item?.match?.llm_decision, '') || '').toLowerCase();
  const reason = String(valueFrom(item?.llm_reason, item?.reason, item?.match?.llm_reason, item?.match?.reason, '') || '').toLowerCase();
  return evidence.reranker_status === 'fallback'
    || decision.includes('fallback')
    || reason.includes('llm did not return')
    || reason.includes('fallback score');
}

function getReason(item) {
  if (isFallbackRecommendation(item)) return '';
  return valueFrom(item?.llm_reason, item?.reason, item?.match?.llm_reason, item?.match?.reason, item?.evidence?.reason, '');
}

function getMatchedSkills(item) {
  const evidence = item?.evidence || item?.match?.evidence || {};
  return evidence.llm_matched_skills || evidence.matched_skills || [];
}

function getMissingSkills(item) {
  const evidence = item?.evidence || item?.match?.evidence || {};
  return evidence.llm_missing_skills || [];
}

function formatDate(value) {
  if (!value) return 'Not available';
  try { return new Date(value).toLocaleString(); } catch { return String(value); }
}

function CandidateProfileCard({ profile, onRefresh }) {
  const tower = profile?.candidate_tower || {};
  const resume = profile?.resume_profile || {};
  const contact = resume?.contact || {};

  const fullName = valueFrom(tower.full_name, contact.full_name, resume.full_name, 'Candidate');
  const title = valueFrom(tower.current_title, resume.current_title, resume.headline);
  const company = valueFrom(tower.current_company, resume.current_company);
  const location = valueFrom(tower.location, contact.location, resume.location);
  const email = valueFrom(tower.email, contact.email);
  const phone = valueFrom(tower.phone, contact.phone);
  const experience = valueFrom(tower.total_experience_years, resume.total_experience_years);
  const skills = firstNonEmptyArray(tower.skills, tower.primary_skills, resume.primary_skills);
  const domains = firstNonEmptyArray(tower.domains, resume.domains);
  const profileStatus = valueFrom(tower.status, tower.profile_state, resume.status, resume.profile_state, 'ready');

  return <Card title="Candidate Profile" subtitle="Profile summary used for recommendations.">
    <div className="row top-actions"><Button onClick={onRefresh}>Refresh profile</Button></div>
    <div className="profile-hero">
      <div>
        <h3>{fullName}</h3>
        <p>{title || 'Title not available'}{company ? ` · ${company}` : ''}</p>
      </div>
      <span className="status-pill">{profileStatus}</span>
    </div>
    <div className="profile-grid">
      <Field label="Email" value={email} />
      <Field label="Phone" value={phone} />
      <Field label="Location" value={location} />
      <Field label="Experience" value={experience ? `${experience} years` : null} />
    </div>
    <div className="detail-section"><h4>Skills</h4><Chips items={skills} /></div>
    <div className="detail-section"><h4>Domains</h4><Chips items={domains} /></div>
  </Card>;
}

function RecommendationCard({ item, onAction, onFeedback }) {
  const applyUrl = getApplyUrl(item);
  const reason = getReason(item);
  const matchedSkills = getMatchedSkills(item);
  return <article className="job-card">
    <div className="job-title">{getJobTitle(item)}</div>
    <div className="job-company-location">{getLocation(item)}</div>
    {reason && <p className="reason">{reason}</p>}
    {!!matchedSkills.length && <div className="mini-section"><span>Matched skills</span><Chips items={matchedSkills} max={8} /></div>}
    <div className="action-bar">
      <Button onClick={() => onAction(item, 'save')}>Save</Button>
      <Button onClick={() => onAction(item, 'apply-click')} disabled={!applyUrl}>Apply</Button>
      <Button variant="secondary" onClick={() => onAction(item, 'not-interested')}>Not Interested</Button>
      <Button variant="success" onClick={() => onFeedback(item, 'good_match')}>Good Match</Button>
      <Button variant="danger" onClick={() => onFeedback(item, 'bad_match')}>Bad Match</Button>
    </div>
  </article>;
}

function SavedJobCard({ item }) {
  const applyUrl = getApplyUrl(item);
  return <div className="saved-row">
    <div className="saved-main">
      <h4>{getJobTitle(item)}</h4>
      <p>{getCompany(item)} · {getLocation(item)}</p>
      <span className="status-pill">{item.status || item.application_status || 'saved'}</span>
    </div>
    {applyUrl && <Button href={applyUrl}>Open job</Button>}
  </div>;
}

function ApplicationCard({ item }) {
  const applyUrl = getApplyUrl(item);
  return <div className="saved-row">
    <div className="saved-main">
      <h4>{getJobTitle(item)}</h4>
      <p>{getCompany(item)} · {getLocation(item)}</p>
      <span className="status-pill">{item.application_status || 'application'}</span>
      <p className="muted">Last updated: {formatDate(item.modified_on || item.last_status_on || item.created_on)}</p>
    </div>
    {applyUrl && <Button href={applyUrl}>Open job</Button>}
  </div>;
}

function SavedJobsCard({ saved, onRefresh }) {
  return <Card title="Saved Jobs" subtitle="Jobs you saved or marked for later.">
    <div className="row top-actions"><Button onClick={onRefresh}>Refresh</Button></div>
    {!saved.length ? <EmptyState message="No saved jobs yet. Click Save on a recommendation to see it here." /> : <div className="compact-list">
      {saved.map((item) => <SavedJobCard key={item.id || `${item.job_id}-${item.status}`} item={item} />)}
    </div>}
  </Card>;
}

function ApplicationsCard({ apps, onRefresh }) {
  return <Card title="Applications" subtitle="Jobs you selected or clicked apply for.">
    <div className="row top-actions"><Button onClick={onRefresh}>Refresh</Button></div>
    {!apps.length ? <EmptyState message="No applications yet. Click Apply or Select on a recommendation to see it here." /> : <div className="compact-list">
      {apps.map((item) => <ApplicationCard key={item.id || `${item.job_id}-${item.application_status}`} item={item} />)}
    </div>}
  </Card>;
}


function ResumeUploadPage({ me, onUploaded }) {
  const [file, setFile] = useState(null);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);

  async function submitResume(event) {
    event.preventDefault();
    setError(null);
    if (!file) {
      setError('Resume upload is mandatory. Select a PDF, DOCX, or image resume file.');
      return;
    }
    const form = new FormData();
    form.append('resume', file);
    setUploading(true);
    try {
      const result = await api('/me/resume', { method: 'POST', body: form }, getToken());
      await onUploaded(result);
    } catch (e) {
      setError(e.message || 'Resume upload failed');
    } finally {
      setUploading(false);
    }
  }

  return <div className="onboarding-shell">
    <Card title="Upload your resume" subtitle="Resume upload is required before recommendations can be generated.">
      <div className="onboarding-hero">
        <div>
          <h3>Welcome{me?.user?.full_name ? `, ${me.user.full_name}` : ''}</h3>
          <p>Your account is created. Upload your resume now and Job Miner will extract your profile automatically.</p>
        </div>
        <span className="status-pill warning">Resume required</span>
      </div>

      <form onSubmit={submitResume} className="upload-form">
        <label className="upload-dropzone">
          <input
            type="file"
            accept=".pdf,.docx,.png,.jpg,.jpeg,.webp,.tif,.tiff"
            onChange={(event) => setFile(event.target.files?.[0] || null)}
            disabled={uploading}
          />
          <div>
            <strong>{file ? file.name : 'Choose resume file'}</strong>
            <p>Supported formats: PDF, DOCX, PNG, JPG, WEBP, TIFF. Maximum size is controlled by JOB_MINER_RESUME_UPLOAD_MAX_MB.</p>
          </div>
        </label>

        {error && <p className="error">{error}</p>}

        <div className="row top-actions">
          <Button type="submit" disabled={uploading || !file}>{uploading ? 'Processing resume...' : 'Upload and generate profile'}</Button>
          <Button variant="secondary" onClick={() => setFile(null)} disabled={uploading || !file}>Clear</Button>
        </div>
      </form>

      <div className="process-note">
        <h4>What happens after upload?</h4>
        <ol>
          <li>Job Miner extracts readable resume text.</li>
          <li>The parser generates your candidate profile, skills, experience, education, and domains.</li>
          <li>You are redirected to the candidate portal with your profile filled.</li>
        </ol>
      </div>
    </Card>
  </div>;
}
function ResumeProcessingPage({ me, refreshMe }) {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [error, setError] = useState(null);

  async function refreshStatus() {
    try {
      setError(null);
      await refreshMe();
    } catch (e) {
      setError(e.message || 'Could not refresh processing status');
    }
  }

  useEffect(() => {
    refreshStatus();

    const timer = window.setInterval(() => {
      setElapsedSeconds((value) => value + 1);
    }, 1000);

    const poller = window.setInterval(refreshStatus, 15000);

    return () => {
      window.clearInterval(timer);
      window.clearInterval(poller);
    };
  }, []);

  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = String(elapsedSeconds % 60).padStart(2, '0');
  const showComeBackLater = elapsedSeconds >= 300;

  return <div className="onboarding-shell">
    <Card title="Processing your resume" subtitle="Your upload is already in progress. Please do not upload the same resume again.">
      <div className="onboarding-hero processing-hero">
        <div>
          <h3>Resume processing is in progress</h3>
          <p>Job Miner is extracting your profile, skills, experience, education, and domains.</p>
          <p className="muted">Elapsed time: <b>{minutes}:{seconds}</b></p>
        </div>
        <span className="status-pill warning">Processing</span>
      </div>

      <div className="progress-panel">
        <div className="progress-spinner" aria-hidden="true" />
        <div>
          <h4>What is happening now?</h4>
          <p>Profile creation usually takes 1–3 minutes. Job recommendations are generated after your profile is ready.</p>
          {showComeBackLater && <p className="muted">This is taking longer than usual. You can keep this page open or come back later. Your current upload will continue in the background.</p>}
        </div>
      </div>

      <div className="row top-actions">
        <Button onClick={refreshStatus}>Refresh status</Button>
      </div>

      {error && <p className="error">{error}</p>}
    </Card>
  </div>;
}



function CandidatePortal({ me, refreshMe }) {
  const [profile, setProfile] = useState(null);
  const [recs, setRecs] = useState([]);
  const [source, setSource] = useState('llm');
  const [saved, setSaved] = useState([]);
  const [apps, setApps] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function loadProfile() {
    setError(null);
    try { setProfile(await api('/me/profile', {}, getToken())); } catch (e) { setError(e.message); }
  }


  async function loadRecs(nextSource = source) {
    setError(null);
    setLoading(true);
    try {
      const res = await api(`/me/recommendations?source=${nextSource}&limit=50`, {}, getToken());
      const items = res.recommendations || [];
      setRecs(nextSource === 'llm' ? items.filter((item) => !isFallbackRecommendation(item)) : items);
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }

  async function loadSavedAndApps() {
    try { setSaved((await api('/me/saved-jobs', {}, getToken())).saved_jobs || []); } catch {}
    try { setApps((await api('/me/applications', {}, getToken())).applications || []); } catch {}
  }

  async function jobAction(item, action) {
    const jobId = getJobId(item);
    const applyUrl = getApplyUrl(item);
    if (!jobId) return;
    const body = { match_run_id: getMatchRunId(item), source_collection: getSourceCollection(item), apply_url: applyUrl };
    await api(`/me/jobs/${jobId}/${action}`, { method: 'POST', body: JSON.stringify(body) }, getToken());
    if (action === 'apply-click' && applyUrl) window.open(applyUrl, '_blank');
    await loadSavedAndApps();
  }

  async function sendFeedback(item, label) {
    const jobId = getJobId(item);
    if (!jobId) return;
    await api(`/me/recommendations/${jobId}/feedback`, { method: 'POST', body: JSON.stringify({ job_id: jobId, label, match_run_id: getMatchRunId(item) }) }, getToken());
    alert('Feedback saved');
  }

  useEffect(() => {
    if (!me.candidate_link) return;
    loadProfile();
    if (me.profile_state === 'ready') loadRecs(source);
    loadSavedAndApps();
  }, [me?.candidate_link?.candidate_id, me?.profile_state]);

  if (me.profile_state === 'conflict') {
    return <Card title="Profile resolution needed" subtitle="We found more than one candidate profile for your verified email.">
      <p>No candidate data is shown until an administrator resolves the duplicate profile safely.</p>
      <Button onClick={refreshMe}>Retry</Button>
      {error && <p className="error">{error}</p>}
    </Card>;
  }

  if (me.profile_state === 'blocked' || !me.candidate_link) {
    return <Card title="Profile setup is blocked" subtitle="Your login is valid, but your candidate profile cannot be linked yet.">
      <p>Make sure your email is verified in Keycloak or Google, then reload the session. If this remains blocked, contact an administrator.</p>
      <Button onClick={refreshMe}>Retry</Button>
      {error && <p className="error">{error}</p>}
    </Card>;
  }

  const isIncomplete = me.profile_state === 'incomplete';

  return <div className="candidate-layout">
    <div className="left-column">
      <CandidateProfileCard profile={profile} onRefresh={loadProfile} />
      <SavedJobsCard saved={saved} onRefresh={loadSavedAndApps} />
      <ApplicationsCard apps={apps} onRefresh={loadSavedAndApps} />
    </div>
    <div className="right-column">
      <Card title="Recommended Jobs" subtitle={isIncomplete ? "Complete your profile and upload a resume to generate recommendations." : "Review jobs and act directly from this list."}>
        {isIncomplete ? <EmptyState message="Your candidate profile was created from login only. Upload or process your resume, add mobile number, work status, skills, and preferred locations to activate recommendations." /> : <>
          <div className="row top-actions">
            <Button onClick={() => { setSource('llm'); loadRecs('llm'); }}>Recommended</Button>
            <Button variant="secondary" onClick={() => { setSource('baseline'); loadRecs('baseline'); }}>Baseline</Button>
            <Button variant="secondary" onClick={() => loadRecs(source)}>Refresh</Button>
          </div>
          <p className="muted">Showing: <b>{source === 'llm' ? 'Recommended jobs' : 'Baseline jobs'}</b></p>
        {loading ? <EmptyState message="Loading recommendations..." /> : !recs.length ? <EmptyState message="No LLM-scored recommendations found. Use Baseline or ask admin to rerun LLM reranking with a smaller chunk size." /> : <div className="job-list">
          {recs.map((r) => <RecommendationCard key={`${r.match_run_id}-${r.job_id}`} item={r} onAction={jobAction} onFeedback={sendFeedback} />)}
        </div>}
        </>}
      </Card>
      {error && <p className="error">{error}</p>}
    </div>
  </div>;
}

function FieldPlain({ label, value }) {
  return <div className="info-item"><div className="info-label">{label}</div><div className="info-value">{value || 'Not available'}</div></div>;
}

function AdminStats({ stats }) {
  const counts = stats?.warehouse_counts || {};
  const rows = Object.entries(counts);
  if (!rows.length) return <EmptyState message="No stats loaded yet." />;
  return <div className="stats-grid">{rows.map(([key, value]) => <div className="stat" key={key}><span>{key}</span><strong>{value}</strong></div>)}</div>;
}

function RunsTable({ runs = [] }) {
  if (!runs.length) return <EmptyState message="No pipeline runs yet." />;
  return <div className="table-wrap"><table><thead><tr><th>Pipeline</th><th>Status</th><th>Started</th><th>Completed</th></tr></thead><tbody>{runs.map((run) => <tr key={run.id}><td>{run.pipeline_name}</td><td><span className="status-pill">{run.status}</span></td><td>{formatDate(run.started_on || run.created_on)}</td><td>{formatDate(run.completed_on)}</td></tr>)}</tbody></table></div>;
}

function TaskStatus({ task }) {
  if (!task) return <EmptyState message="No task selected." />;
  return <div className="profile-grid">
    <FieldPlain label="Task" value={task.task_name} />
    <FieldPlain label="Status" value={task.status} />
    <FieldPlain label="Queue" value={task.queue_name} />
    <FieldPlain label="Task UUID" value={task.task_uuid} />
    <FieldPlain label="Started" value={formatDate(task.started_on)} />
    <FieldPlain label="Completed" value={formatDate(task.completed_on)} />
    <FieldPlain label="Error" value={task.error_message} />
  </div>;
}

function AdminPortal() {
  const [stats, setStats] = useState(null);
  const [runs, setRuns] = useState([]);
  const [task, setTask] = useState(null);
  const [taskId, setTaskId] = useState('');
  const [error, setError] = useState(null);

  async function loadStats() { try { setStats(await api('/admin/stats', {}, getToken())); } catch (e) { setError(e.message); } }
  async function loadRuns() { try { setRuns((await api('/admin/pipeline/runs', {}, getToken())).runs || []); } catch (e) { setError(e.message); } }
  async function runPipeline() {
    setError(null);
    try {
      const res = await api('/admin/pipeline/full/run', { method: 'POST', body: JSON.stringify({ run_llm: true, recreate_qdrant: true, llm_top_k: 100, final_top_n: 10 }) }, getToken());
      setTaskId(res.task_id); setTask(res.task); await loadRuns();
    } catch (e) { setError(e.message); }
  }
  async function pollTask(id = taskId) { if (!id) return; try { setTask((await api(`/admin/tasks/${id}`, {}, getToken())).task); } catch (e) { setError(e.message); } }
  useEffect(() => { loadStats(); loadRuns(); }, []);
  return <div className="grid two">
    <Card title="Admin Dashboard" subtitle="Run pipeline and monitor warehouse counts.">
      {error && <p className="error">{error}</p>}
      <div className="row top-actions"><Button onClick={loadStats}>Refresh Stats</Button><Button onClick={runPipeline}>Run Full Pipeline</Button></div>
      <AdminStats stats={stats} />
    </Card>
    <Card title="Pipeline Runs and Task Status" subtitle="Track latest task state.">
      <div className="row top-actions"><Button onClick={loadRuns}>Refresh Runs</Button><input placeholder="task id" value={taskId} onChange={e => setTaskId(e.target.value)} /><Button onClick={() => pollTask()}>Check Task</Button></div>
      <h3>Latest Task</h3><TaskStatus task={task} />
      <h3>Runs</h3><RunsTable runs={runs} />
    </Card>
  </div>;
}

function App() {
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState(null);
  const [view, setView] = useState('candidate');
  const [error, setError] = useState(null);

  async function loadMe() { const res = await api('/me', {}, getToken()); setMe(res); return res; }

  async function handleResumeUploaded() {
    const updated = await loadMe();
    setMe(updated);
    setView('candidate');
  }

  useEffect(() => {
    initKeycloak().then(() => loadMe()).then((res) => {
      setReady(true);
      const roles = res.user?.roles || [];
      if (roles.includes('platform_admin') || roles.includes('tenant_admin')) setView('admin');
    }).catch(e => setError(e.message));
  }, []);

  if (error) return <div className="container"><h1>Job Miner</h1><p className="error">{error}</p></div>;
  if (!ready) return <div className="container"><h1>Job Miner</h1><p>Loading Keycloak session...</p></div>;

  const roles = me?.user?.roles || [];
  const canAdmin = roles.includes('platform_admin') || roles.includes('tenant_admin');

  return <div className="container">
    <header>
      <h1>Job Miner</h1>
      <div className="row">
        <span className="muted">{me?.user?.email || me?.user?.preferred_username}</span>
        <Button onClick={() => setView('candidate')}>Candidate Portal</Button>
        {canAdmin && <Button onClick={() => setView('admin')}>Admin Portal</Button>}
        <Button variant="secondary" onClick={logout}>Logout</Button>
      </div>
    </header>
    {view === 'admin' && canAdmin ? <AdminPortal /> : (
      me?.profile_state === 'processing' || me?.next_action === 'wait_for_resume_processing'
        ? <ResumeProcessingPage me={me} refreshMe={loadMe} />
        : (me?.profile_state === 'incomplete' || me?.next_action === 'complete_profile' || me?.next_action === 'retry_resume_upload'
          ? <ResumeUploadPage me={me} onUploaded={handleResumeUploaded} />
          : <CandidatePortal me={me} refreshMe={loadMe} />)
    )}
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
