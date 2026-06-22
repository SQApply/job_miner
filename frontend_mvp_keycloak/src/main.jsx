import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api } from './lib/api';
import { getToken, initKeycloak, logout } from './lib/keycloak';
import AdminPortalManagement from './components/AdminPortalManagement';
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

function CandidateProfileCard({ profile, onRefresh, onEdit }) {
  const tower = profile?.candidate_tower || {};
  const resume = profile?.resume_profile || {};
  const contact = resume?.contact || {};
  const effective = tower.effective_profile || {};

  const fullName = valueFrom(effective.full_name, tower.full_name, contact.full_name, resume.full_name, 'Candidate');
  const title = valueFrom(effective.current_title, tower.current_title, resume.current_title, resume.headline);
  const company = valueFrom(effective.current_company, tower.current_company, resume.current_company);
  const location = valueFrom(effective.location, tower.location, contact.location, resume.location);
  const email = valueFrom(effective.email, tower.email, contact.email);
  const phone = valueFrom(effective.phone, tower.phone, contact.phone);
  const experience = valueFrom(effective.total_experience_years, tower.total_experience_years, resume.total_experience_years);
  const skills = firstNonEmptyArray(effective.skills, tower.skills, tower.primary_skills, resume.primary_skills);
  const domains = firstNonEmptyArray(effective.domains, tower.domains, resume.domains);
  const profileStatus = valueFrom(tower.status, tower.profile_state, resume.status, resume.profile_state, 'ready');

  return <Card title="Candidate Profile" subtitle="Profile summary used for recommendations.">
    <div className="row top-actions">
      <Button onClick={onRefresh}>Refresh profile</Button>
      <Button variant="secondary" onClick={onEdit}>Edit Profile</Button>
    </div>
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
      <Field label="Experience" value={experience !== null && experience !== undefined ? `${experience} years` : null} />
    </div>
    <div className="detail-section"><h4>Skills</h4><Chips items={skills} /></div>
    <div className="detail-section"><h4>Domains</h4><Chips items={domains} /></div>
  </Card>;
}

function csvFromArray(values) {
  return Array.isArray(values) ? values.join(', ') : (values || '');
}

function ProfileEditForm({ profile, onCancel, onSaved }) {
  const tower = profile?.candidate_tower || {};
  const resume = profile?.resume_profile || {};
  const contact = resume?.contact || {};
  const effective = tower.effective_profile || {};
  const [form, setForm] = useState(() => ({
    full_name: valueFrom(effective.full_name, tower.full_name, contact.full_name, resume.full_name, ''),
    email: valueFrom(effective.email, tower.email, contact.email, ''),
    phone: valueFrom(effective.phone, tower.phone, contact.phone, ''),
    location: valueFrom(effective.location, tower.location, contact.location, resume.location, ''),
    current_title: valueFrom(effective.current_title, tower.current_title, resume.current_title, ''),
    current_company: valueFrom(effective.current_company, tower.current_company, resume.current_company, ''),
    total_experience_years: valueFrom(effective.total_experience_years, tower.total_experience_years, resume.total_experience_years, ''),
    skills: csvFromArray(firstNonEmptyArray(effective.skills, tower.skills, resume.primary_skills)),
    domains: csvFromArray(firstNonEmptyArray(effective.domains, tower.domains, resume.domains)),
    target_roles: csvFromArray(firstNonEmptyArray(effective.target_roles, tower.target_roles)),
    preferred_locations: csvFromArray(firstNonEmptyArray(effective.preferred_locations, tower.preferred_locations)),
    remote_preference: valueFrom(effective.remote_preference, tower.remote_preference, 'no_preference'),
    linkedin_url: valueFrom(effective.linkedin_url, tower.linkedin_url, ''),
    github_url: valueFrom(effective.github_url, tower.github_url, ''),
    portfolio_url: valueFrom(effective.portfolio_url, tower.portfolio_url, ''),
  }));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function csvToList(value) {
    return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
  }

  async function submit(event) {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const payload = {
        full_name: form.full_name,
        email: form.email,
        phone: form.phone,
        location: form.location,
        current_title: form.current_title,
        current_company: form.current_company,
        total_experience_years: form.total_experience_years === '' ? null : Number(form.total_experience_years),
        skills: csvToList(form.skills),
        domains: csvToList(form.domains),
        target_roles: csvToList(form.target_roles),
        preferred_locations: csvToList(form.preferred_locations),
        remote_preference: form.remote_preference || 'no_preference',
        linkedin_url: form.linkedin_url,
        github_url: form.github_url,
        portfolio_url: form.portfolio_url,
      };
      const result = await api('/me/profile', { method: 'PATCH', body: JSON.stringify(payload) }, getToken());
      await onSaved(result);
    } catch (e) {
      setError(e.message || 'Could not save profile');
    } finally {
      setSaving(false);
    }
  }

  return <Card title="Edit Candidate Profile" subtitle="Name and email are locked. Update missing profile fields, skills, domains, and matching preferences.">
    <form className="profile-edit-form" onSubmit={submit}>
      <div className="profile-grid edit-grid">
        <label><span>Full name</span><input value={form.full_name} readOnly /></label>
        <label><span>Email</span><input value={form.email} readOnly /></label>
        <label><span>Phone</span><input value={form.phone} onChange={(e) => update('phone', e.target.value)} /></label>
        <label><span>Location</span><input value={form.location} onChange={(e) => update('location', e.target.value)} placeholder="Delhi NCR, India" /></label>
        <label><span>Current title</span><input value={form.current_title} onChange={(e) => update('current_title', e.target.value)} /></label>
        <label><span>Current company</span><input value={form.current_company} onChange={(e) => update('current_company', e.target.value)} /></label>
        <label><span>Total experience years</span><input type="number" step="0.1" min="0" max="60" value={form.total_experience_years} onChange={(e) => update('total_experience_years', e.target.value)} /></label>
        <label><span>Remote preference</span><select value={form.remote_preference} onChange={(e) => update('remote_preference', e.target.value)}><option value="no_preference">No preference</option><option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="onsite">Onsite</option></select></label>
      </div>
      <label className="full-width-field"><span>Skills <small>comma separated</small></span><textarea rows="4" value={form.skills} onChange={(e) => update('skills', e.target.value)} /></label>
      <label className="full-width-field"><span>Domains <small>comma separated</small></span><textarea rows="3" value={form.domains} onChange={(e) => update('domains', e.target.value)} /></label>
      <label className="full-width-field"><span>Target roles <small>comma separated</small></span><input value={form.target_roles} onChange={(e) => update('target_roles', e.target.value)} placeholder="AI Engineer, LLM Engineer, Data Scientist" /></label>
      <label className="full-width-field"><span>Preferred locations <small>comma separated</small></span><input value={form.preferred_locations} onChange={(e) => update('preferred_locations', e.target.value)} placeholder="Remote, India, Dubai" /></label>
      <div className="profile-grid edit-grid">
        <label><span>LinkedIn URL</span><input value={form.linkedin_url} onChange={(e) => update('linkedin_url', e.target.value)} /></label>
        <label><span>GitHub URL</span><input value={form.github_url} onChange={(e) => update('github_url', e.target.value)} /></label>
        <label><span>Portfolio URL</span><input value={form.portfolio_url} onChange={(e) => update('portfolio_url', e.target.value)} /></label>
      </div>
      <p className="muted">Changing skills, domains, location, experience, title, target roles, preferred locations, or remote preference will refresh recommendations. Contact details are saved without regenerating recommendations.</p>
      {error && <p className="error">{error}</p>}
      <div className="row top-actions">
        <Button type="submit" disabled={saving}>{saving ? 'Saving...' : 'Save profile'}</Button>
        <Button variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Button>
      </div>
    </form>
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

function createEmptyCatalogFilters() {
  return {
    freshness: '',
    work_modes: [],
    employment_types: [],
    locations: [],
    companies: [],
    skills: [],
  };
}

function selectedCatalogFacetOptions(options = [], selectedValues = []) {
  const visible = Array.isArray(options) ? [...options] : [];
  const knownValues = new Set(visible.map((option) => String(option?.value || '').toLocaleLowerCase()));
  for (const selectedValue of selectedValues || []) {
    const value = String(selectedValue || '').trim();
    if (!value || knownValues.has(value.toLocaleLowerCase())) continue;
    visible.push({ value, label: value, count: 0 });
  }
  return visible;
}

function CatalogFilterGroup({
  title,
  options = [],
  selectedValues = [],
  selectedValue = '',
  selectionMode = 'multiple',
  isOpen,
  onToggleOpen,
  onToggleValue,
  emptyMessage = 'No matching options',
}) {
  const selected = new Set((selectedValues || []).map((value) => String(value).toLocaleLowerCase()));
  const renderedOptions = selectionMode === 'multiple'
    ? selectedCatalogFacetOptions(options, selectedValues)
    : options;

  return <section className="catalog-filter-group">
    <button
      type="button"
      className="catalog-filter-group-toggle"
      onClick={onToggleOpen}
      aria-expanded={isOpen}
    >
      <span>{title}</span>
      <span className={`catalog-filter-chevron ${isOpen ? 'open' : ''}`} aria-hidden="true">⌄</span>
    </button>
    {isOpen && <div className="catalog-filter-options">
      {!renderedOptions.length ? <p className="catalog-filter-empty">{emptyMessage}</p> : renderedOptions.map((option) => {
        const value = String(option?.value || '');
        const label = String(option?.label || value);
        const optionId = `${title.replace(/\s+/g, '-').toLocaleLowerCase()}-${value.replace(/[^a-z0-9]+/gi, '-').toLocaleLowerCase()}`;
        const isChecked = selectionMode === 'single'
          ? selectedValue === value
          : selected.has(value.toLocaleLowerCase());
        return <label key={value} className={`catalog-filter-option ${isChecked ? 'selected' : ''}`} htmlFor={optionId}>
          <input
            id={optionId}
            type={selectionMode === 'single' ? 'radio' : 'checkbox'}
            name={selectionMode === 'single' ? 'catalog-freshness' : undefined}
            value={value}
            checked={isChecked}
            onChange={() => onToggleValue(value)}
          />
          <span className="catalog-filter-option-label">{label}</span>
          <span className="catalog-filter-option-count">{option?.count === undefined || option?.count === null ? '' : Number(option.count).toLocaleString()}</span>
        </label>;
      })}
    </div>}
  </section>;
}

function catalogJobSkills(job) {
  const values = [
    ...(Array.isArray(job?.required_skills) ? job.required_skills : []),
    ...(Array.isArray(job?.skills) ? job.skills : []),
    ...(Array.isArray(job?.preferred_skills) ? job.preferred_skills : []),
  ];
  const seen = new Set();
  return values
    .map((value) => String(value || '').trim())
    .filter((value) => {
      const key = value.toLocaleLowerCase();
      if (!value || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 6);
}

function formatRelativeJobAge(value) {
  if (!value) return 'Date unavailable';
  const timestamp = new Date(value).getTime();
  if (Number.isNaN(timestamp)) return 'Date unavailable';
  const elapsed = Math.max(0, Date.now() - timestamp);
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 60) return minutes <= 1 ? 'just now' : `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`;
  return new Date(value).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

function catalogFreshnessText(job) {
  const rawSourceText = String(job?.posted_date || '').trim();
  if (job?.freshness_source === 'source_posted_date') {
    return rawSourceText ? `Posted ${rawSourceText}` : `Posted ${formatRelativeJobAge(job?.freshness_at)}`;
  }
  return `Added ${formatRelativeJobAge(job?.freshness_at)}`;
}

function activeCatalogFilterCount(filters) {
  if (!filters) return 0;
  return (filters.freshness ? 1 : 0)
    + ['work_modes', 'employment_types', 'locations', 'companies', 'skills']
      .reduce((count, key) => count + (Array.isArray(filters[key]) ? filters[key].length : 0), 0);
}

function AllJobsPage({ onBack, onAction }) {
  const [jobs, setJobs] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [limit] = useState(24);
  const [query, setQuery] = useState('');
  const [searchText, setSearchText] = useState('');
  const [filters, setFilters] = useState(createEmptyCatalogFilters);
  const [facets, setFacets] = useState({});
  const [sort, setSort] = useState('newest');
  const [freshnessNote, setFreshnessNote] = useState('');
  const [expandedGroups, setExpandedGroups] = useState({
    freshness: true,
    work_modes: true,
    employment_types: false,
    locations: true,
    companies: false,
    skills: false,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const latestRequestRef = useRef(0);

  async function loadJobs({
    nextOffset = offset,
    nextQuery = query,
    nextFilters = filters,
    nextSort = sort,
  } = {}) {
    const requestId = ++latestRequestRef.current;
    setError(null);
    setLoading(true);
    try {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(nextOffset),
        sort: nextSort,
      });
      if (nextQuery) params.set('q', nextQuery);
      if (nextFilters.freshness) params.set('freshness', nextFilters.freshness);
      for (const field of ['work_modes', 'employment_types', 'locations', 'companies', 'skills']) {
        for (const value of nextFilters[field] || []) params.append(field, value);
      }

      const res = await api(`/me/jobs/all?${params.toString()}`, {}, getToken());
      if (requestId !== latestRequestRef.current) return;
      setJobs(res.jobs || []);
      setTotal(Number(res.total || 0));
      setOffset(Number(res.offset || 0));
      setQuery(res.q || '');
      setSort(res.sort || nextSort);
      setFilters({ ...createEmptyCatalogFilters(), ...(res.filters || nextFilters) });
      setFacets(res.facets || {});
      setFreshnessNote(res.freshness_note || '');
    } catch (e) {
      if (requestId === latestRequestRef.current) setError(e.message || 'Could not load jobs');
    } finally {
      if (requestId === latestRequestRef.current) setLoading(false);
    }
  }

  function submitSearch(event) {
    event.preventDefault();
    loadJobs({ nextOffset: 0, nextQuery: searchText.trim() });
  }

  function updateFilters(nextFilters) {
    setFilters(nextFilters);
    loadJobs({ nextOffset: 0, nextFilters });
  }

  function toggleMultiFilter(field, value) {
    const currentValues = Array.isArray(filters[field]) ? filters[field] : [];
    const present = currentValues.some((currentValue) => String(currentValue).toLocaleLowerCase() === String(value).toLocaleLowerCase());
    const nextValues = present
      ? currentValues.filter((currentValue) => String(currentValue).toLocaleLowerCase() !== String(value).toLocaleLowerCase())
      : [...currentValues, value];
    updateFilters({ ...filters, [field]: nextValues });
  }

  function setFreshness(value) {
    updateFilters({ ...filters, freshness: value });
  }

  function clearFilters() {
    updateFilters(createEmptyCatalogFilters());
  }

  function clearSearch() {
    setSearchText('');
    loadJobs({ nextOffset: 0, nextQuery: '' });
  }

  function toggleGroup(name) {
    setExpandedGroups((current) => ({ ...current, [name]: !current[name] }));
  }

  async function handleAction(item, action) {
    await onAction(item, action);
  }

  useEffect(() => {
    loadJobs({ nextOffset: 0, nextQuery: '', nextFilters: createEmptyCatalogFilters(), nextSort: 'newest' });
  }, []);

  const currentStart = total ? offset + 1 : 0;
  const currentEnd = Math.min(offset + jobs.length, total);
  const canPrevious = offset > 0;
  const canNext = offset + limit < total;
  const filterCount = activeCatalogFilterCount(filters);

  return <div className="all-jobs-page">
    <div className="catalog-page-heading">
      <div>
        <button type="button" className="catalog-back-link" onClick={onBack}>← Back to candidate portal</button>
        <h1>All Jobs</h1>
        <p>Search the complete catalog and narrow the results using job freshness, work mode, skills, location, company, and employment type.</p>
      </div>
    </div>

    <div className="catalog-layout">
      <aside className="catalog-filter-sidebar" aria-label="Job filters">
        <div className="catalog-filter-header">
          <div>
            <h2>All Filters</h2>
            {filterCount > 0 && <span className="catalog-filter-active-count">{filterCount} active</span>}
          </div>
          <button type="button" className="catalog-clear-filters" onClick={clearFilters} disabled={loading || filterCount === 0}>Clear all</button>
        </div>

        <CatalogFilterGroup
          title="Job freshness"
          options={[{ value: '', label: 'Any time' }, ...(facets.freshness || [])]}
          selectedValue={filters.freshness}
          selectionMode="single"
          isOpen={expandedGroups.freshness}
          onToggleOpen={() => toggleGroup('freshness')}
          onToggleValue={setFreshness}
          emptyMessage="No dated jobs available"
        />
        <CatalogFilterGroup
          title="Work mode"
          options={facets.work_modes || []}
          selectedValues={filters.work_modes}
          isOpen={expandedGroups.work_modes}
          onToggleOpen={() => toggleGroup('work_modes')}
          onToggleValue={(value) => toggleMultiFilter('work_modes', value)}
        />
        <CatalogFilterGroup
          title="Employment type"
          options={facets.employment_types || []}
          selectedValues={filters.employment_types}
          isOpen={expandedGroups.employment_types}
          onToggleOpen={() => toggleGroup('employment_types')}
          onToggleValue={(value) => toggleMultiFilter('employment_types', value)}
        />
        <CatalogFilterGroup
          title="Location"
          options={facets.locations || []}
          selectedValues={filters.locations}
          isOpen={expandedGroups.locations}
          onToggleOpen={() => toggleGroup('locations')}
          onToggleValue={(value) => toggleMultiFilter('locations', value)}
        />
        <CatalogFilterGroup
          title="Company"
          options={facets.companies || []}
          selectedValues={filters.companies}
          isOpen={expandedGroups.companies}
          onToggleOpen={() => toggleGroup('companies')}
          onToggleValue={(value) => toggleMultiFilter('companies', value)}
        />
        <CatalogFilterGroup
          title="Skills"
          options={facets.skills || []}
          selectedValues={filters.skills}
          isOpen={expandedGroups.skills}
          onToggleOpen={() => toggleGroup('skills')}
          onToggleValue={(value) => toggleMultiFilter('skills', value)}
        />
      </aside>

      <section className="catalog-results-panel" aria-live="polite">
        <form className="catalog-search-row" onSubmit={submitSearch}>
          <input
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="Search title, company, location, or skill"
            aria-label="Search jobs"
          />
          <Button type="submit" disabled={loading}>Search</Button>
          {query && <Button type="button" variant="secondary" onClick={clearSearch} disabled={loading}>Clear search</Button>}
        </form>

        <div className="catalog-results-toolbar">
          <div className="catalog-results-summary">
            <strong>{loading ? 'Loading jobs…' : `${currentStart}-${currentEnd} of ${total.toLocaleString()} jobs`}</strong>
            {query && <span>for “{query}”</span>}
            {filterCount > 0 && <span>· {filterCount} filter{filterCount === 1 ? '' : 's'} applied</span>}
          </div>
          <label className="catalog-sort-control">
            <span>Sort by</span>
            <select
              value={sort}
              onChange={(event) => {
                const nextSort = event.target.value;
                setSort(nextSort);
                loadJobs({ nextOffset: 0, nextSort });
              }}
              disabled={loading}
            >
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="title_asc">Title: A to Z</option>
              <option value="title_desc">Title: Z to A</option>
            </select>
          </label>
        </div>

        {freshnessNote && <p className="catalog-freshness-note">{freshnessNote}</p>}
        {error && <p className="error">{error}</p>}

        {loading ? <EmptyState message="Loading jobs and filter options…" /> : !jobs.length ? <EmptyState message="No jobs match the current search and filters." /> : <div className="catalog-job-list">
          {jobs.map((job) => {
            const skills = catalogJobSkills(job);
            return <article className="catalog-job-card" key={job.job_id}>
              <div className="catalog-job-card-main">
                <div className="catalog-job-title-row">
                  <h2>{getJobTitle(job)}</h2>
                  <span className="catalog-job-freshness">◷ {catalogFreshnessText(job)}</span>
                </div>
                <div className="catalog-job-company">{getCompany(job)}</div>
                <div className="catalog-job-meta">
                  <span>⌖ {getLocation(job)}</span>
                  {job.employment_type && <span>{job.employment_type}</span>}
                </div>
                {job.summary && <p className="catalog-job-summary">{String(job.summary).slice(0, 280)}</p>}
                {!!skills.length && <div className="catalog-job-skills"><Chips items={skills} max={6} /></div>}
              </div>
              <div className="catalog-job-actions">
                <Button onClick={() => handleAction(job, 'apply-click')} disabled={!getApplyUrl(job)}>Apply</Button>
                <Button variant="secondary" onClick={() => handleAction(job, 'save')}>Save</Button>
              </div>
            </article>;
          })}
        </div>}

        <div className="pagination-bar catalog-pagination-bar">
          <Button variant="secondary" disabled={!canPrevious || loading} onClick={() => loadJobs({ nextOffset: Math.max(offset - limit, 0) })}>Previous</Button>
          <span className="catalog-pagination-status">Page {total ? Math.floor(offset / limit) + 1 : 0} of {total ? Math.ceil(total / limit) : 0}</span>
          <Button variant="secondary" disabled={!canNext || loading} onClick={() => loadJobs({ nextOffset: offset + limit })}>Next</Button>
        </div>
      </section>
    </div>
  </div>;
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
  const processing = me?.candidate_processing || {};
  const resumeUploadStatus = String(processing.resume_upload_status || '').toLowerCase();
  const asyncResumeError = (me?.next_action === 'retry_resume_upload' || resumeUploadStatus === 'failed')
    ? (processing.resume_upload_error || processing.resume_upload_status_message || 'Resume processing failed. Please upload the corrected resume and try again.')
    : null;

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

      {asyncResumeError && <div className="error-block">
        <strong>Previous resume upload failed.</strong>
        <p>{asyncResumeError}</p>
      </div>}

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



function CandidatePortal({ me, refreshMe, candidateView, setCandidateView }) {
  const [profile, setProfile] = useState(null);
  const [recs, setRecs] = useState([]);
  const [source, setSource] = useState('llm');
  const [saved, setSaved] = useState([]);
  const [apps, setApps] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [recommendationStatus, setRecommendationStatus] = useState(null);
  const [notice, setNotice] = useState(null);

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

  async function loadRecommendationStatus() {
    try {
      const res = await api('/me/recommendations/status', {}, getToken());
      setRecommendationStatus(res);
      return res;
    } catch {
      return null;
    }
  }

  async function handleProfileSaved(result) {
    await loadProfile();
    await refreshMe();
    setCandidateView('dashboard');
    if (result?.recommendations_refresh_required) {
      setNotice('Profile saved. Recommendations are refreshing in the background.');
      await loadRecommendationStatus();
    } else {
      setNotice('Profile saved. Recommendations were not regenerated because matching inputs did not change.');
    }
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
    loadRecommendationStatus();
    if (me.profile_state === 'ready') loadRecs(source);
    loadSavedAndApps();
  }, [me?.candidate_link?.candidate_id, me?.profile_state]);

  useEffect(() => {
    const status = String(recommendationStatus?.status || '').toLowerCase();
    if (!['pending', 'queued', 'running', 'processing'].includes(status)) return undefined;
    const timer = window.setInterval(async () => {
      const next = await loadRecommendationStatus();
      const nextStatus = String(next?.status || '').toLowerCase();
      if (['llm_ready', 'baseline_ready', 'completed', 'no_matches', 'no_jobs'].includes(nextStatus)) {
        await loadRecs(source);
      }
    }, 10000);
    return () => window.clearInterval(timer);
  }, [recommendationStatus?.status, source]);

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

  if (candidateView === 'all-jobs') {
    return <AllJobsPage onBack={() => setCandidateView('dashboard')} onAction={jobAction} />;
  }

  if (candidateView === 'profile') {
    return <ProfileEditForm profile={profile} onCancel={() => setCandidateView('dashboard')} onSaved={handleProfileSaved} />;
  }

  const recStatus = String(recommendationStatus?.status || '').toLowerCase();
  const recommendationsRefreshing = ['pending', 'queued', 'running', 'processing'].includes(recStatus);

  return <div className="candidate-layout">
    <div className="left-column">
      <CandidateProfileCard profile={profile} onRefresh={loadProfile} onEdit={() => setCandidateView('profile')} />
      <SavedJobsCard saved={saved} onRefresh={loadSavedAndApps} />
      <ApplicationsCard apps={apps} onRefresh={loadSavedAndApps} />
    </div>
    <div className="right-column">
      <Card title="Recommended Jobs" subtitle={isIncomplete ? "Complete your profile and upload a resume to generate recommendations." : "Review jobs and act directly from this list."}>
        {notice && <p className="info-banner">{notice}</p>}
        {recommendationsRefreshing && <p className="info-banner">{recommendationStatus?.message || 'Recommendations are refreshing in the background.'}</p>}
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
        <div className="view-all-jobs-row">
          <Button variant="secondary" onClick={() => setCandidateView('all-jobs')}>View all jobs</Button>
        </div>
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
  return <>
    <AdminPortalManagement />
    <div className="grid two">
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
    </div>
  </>;
}

function App() {
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState(null);
  const [view, setView] = useState('candidate');
  const [candidateView, setCandidateView] = useState('dashboard');
  const [error, setError] = useState(null);

  async function loadMe() { const res = await api('/me', {}, getToken()); setMe(res); return res; }

  function openCandidateProfile() {
    setView('candidate');
    setCandidateView('profile');
  }

  function openAdminPortal() {
    setView('admin');
    setCandidateView('dashboard');
  }

  async function handleResumeUploaded() {
    const updated = await loadMe();
    setMe(updated);
    setView('candidate');
    setCandidateView('dashboard');
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
        <Button onClick={openCandidateProfile}>Candidate Portal</Button>
        {canAdmin && <Button onClick={openAdminPortal}>Admin Portal</Button>}
        <Button variant="secondary" onClick={logout}>Logout</Button>
      </div>
    </header>
    {view === 'admin' && canAdmin ? <AdminPortal /> : (
      me?.profile_state === 'processing' || me?.next_action === 'wait_for_resume_processing'
        ? <ResumeProcessingPage me={me} refreshMe={loadMe} />
        : (me?.profile_state === 'incomplete' || me?.next_action === 'complete_profile' || me?.next_action === 'retry_resume_upload'
          ? <ResumeUploadPage me={me} onUploaded={handleResumeUploaded} />
          : <CandidatePortal me={me} refreshMe={loadMe} candidateView={candidateView} setCandidateView={setCandidateView} />)
    )}
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
