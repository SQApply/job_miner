const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

export async function api(path, options = {}, token = null) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.detail || data.message || text || `HTTP ${res.status}`);
  return data;
}

export { API_BASE };
