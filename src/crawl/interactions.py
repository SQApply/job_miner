from __future__ import annotations

import json


_VISIBLE_JS = """
const jmVisible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none' &&
         style.visibility !== 'hidden' &&
         rect.width > 0 &&
         rect.height > 0;
};
const jmNorm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
const jmClick = (el) => {
  el.scrollIntoView({ behavior: 'instant', block: 'center' });
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  if (typeof el.click === 'function') el.click();
};
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_click_by_text_js(
    *,
    text_patterns: list[str],
    selectors: str = "button, a, [role='button'], input[type='button'], div, span, li",
    exact_text: str | None = None,
    state_prefix: str = "__jm",
) -> str:
    patterns = [p.lower() for p in text_patterns if p]
    return f"""
(() => {{
  {_VISIBLE_JS}
  const selectors = {_json(selectors)};
  const patterns = {_json(patterns)};
  const exactText = {_json(exact_text.lower() if exact_text else None)};
  const nodes = [...document.querySelectorAll(selectors)].filter(jmVisible);
  const target = nodes.find(el => {{
    const text = jmNorm(el.innerText || el.textContent || el.value);
    if (exactText) return text === exactText;
    return patterns.some(p => text === p || text.includes(p));
  }}) || null;

  window.{state_prefix}_prev_url = location.href;
  window.{state_prefix}_prev_body_sig = (document.body.innerText || '').slice(0, 10000);
  window.{state_prefix}_clicked = !!target;

  if (target) jmClick(target);
  return target ? 'clicked matching control' : 'matching control not found';
}})();
"""


def build_wait_for_body_or_url_change_js(state_prefix: str = "__jm") -> str:
    return f"""
js:() => {{
  const currentBody = (document.body.innerText || '').slice(0, 10000);
  if (window.{state_prefix}_clicked === false) return true;
  return (
    location.href !== (window.{state_prefix}_prev_url || location.href) ||
    currentBody !== (window.{state_prefix}_prev_body_sig || '')
  );
}}
"""


def build_load_more_click_js(*, href_contains: str, button_text_patterns: list[str]) -> str:
    patterns = [p.lower() for p in button_text_patterns if p]
    href_contains_js = href_contains or ""
    return f"""
(() => {{
  {_VISIBLE_JS}
  const patterns = {_json(patterns)};
  const hrefNeedle = {_json(href_contains_js)};
  const countJobs = () => hrefNeedle
    ? document.querySelectorAll(`a[href*="${{hrefNeedle}}"]`).length
    : document.querySelectorAll('a[href]').length;

  const nodes = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], div, span')]
    .filter(jmVisible);
  const target = nodes.find(el => {{
    const text = jmNorm(el.innerText || el.textContent || el.value);
    return patterns.some(p => text === p || text.includes(p));
  }}) || null;

  window.__jm_prev_job_count = countJobs();
  window.__jm_had_load_more = !!target;
  window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 10000);

  if (target) jmClick(target);
  return target ? 'clicked load-more control' : 'load-more control not found';
}})();
"""


def build_load_more_wait_js(*, href_contains: str, button_text_patterns: list[str]) -> str:
    patterns = [p.lower() for p in button_text_patterns if p]
    href_contains_js = href_contains or ""
    return f"""
js:() => {{
  const jmNorm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const jmVisible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const patterns = {_json(patterns)};
  const hrefNeedle = {_json(href_contains_js)};
  const countJobs = () => hrefNeedle
    ? document.querySelectorAll(`a[href*="${{hrefNeedle}}"]`).length
    : document.querySelectorAll('a[href]').length;
  const hasLoadMore = () => [...document.querySelectorAll('button, a, [role="button"], input[type="button"], div, span')]
    .filter(jmVisible)
    .some(el => {{
      const text = jmNorm(el.innerText || el.textContent || el.value);
      return patterns.some(p => text === p || text.includes(p));
    }});

  const now = countJobs();
  const prev = window.__jm_prev_job_count || 0;
  const buttonGone = (window.__jm_had_load_more || false) && !hasLoadMore();
  const bodyChanged = (document.body.innerText || '').slice(0, 10000) !== (window.__jm_prev_body_sig || '');
  return now > prev || buttonGone || bodyChanged;
}}
"""


def build_anchor_pagination_click_js(next_text_patterns: list[str]) -> str:
    patterns = [p.lower() for p in next_text_patterns if p]
    return f"""
(() => {{
  {_VISIBLE_JS}
  const nextPatterns = {_json(patterns)};
  const nodes = [...document.querySelectorAll('a, button, [role="button"], span, div, li')].filter(jmVisible);
  const textOf = (el) => jmNorm(el.innerText || el.textContent || el.value);
  const toClickable = (el) => el?.matches?.('button, a, [role="button"]') ? el : el?.querySelector?.('button, a, [role="button"]');

  const activeEl = nodes.find(el => {{
    const cls = (el.className || '').toString().toLowerCase();
    const aria = (el.getAttribute('aria-current') || '').toLowerCase();
    return aria === 'page' || cls.includes('active') || cls.includes('current') || cls.includes('selected');
  }});

  let activeNum = null;
  if (activeEl) {{
    const t = textOf(activeEl);
    if (/^\d+$/.test(t)) activeNum = parseInt(t, 10);
  }}

  let target = null;
  if (Number.isFinite(activeNum)) {{
    target = nodes.map(toClickable).filter(Boolean).find(el => /^\d+$/.test(textOf(el)) && parseInt(textOf(el), 10) === activeNum + 1) || null;
  }}
  if (!target) {{
    target = nodes.map(toClickable).filter(Boolean).find(el => {{
      const t = textOf(el);
      const aria = jmNorm(el.getAttribute('aria-label') || '');
      return nextPatterns.some(p => t === p || t.includes(p) || aria.includes(p));
    }}) || null;
  }}

  window.__jm_prev_url = location.href;
  window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 12000);
  window.__jm_clicked_pagination = !!target;

  if (target) jmClick(target);
  return target ? 'clicked pagination control' : 'pagination control not found';
}})();
"""


def build_anchor_pagination_wait_js() -> str:
    return """
js:() => {
  if (window.__jm_clicked_pagination === false) return true;
  const currentBody = (document.body.innerText || '').slice(0, 12000);
  return (
    location.href !== (window.__jm_prev_url || location.href) ||
    currentBody !== (window.__jm_prev_body_sig || '')
  );
}
"""


def build_url_param_pagination_click_js(*, page_param: str, start_page: int, step: int, url_template: str | None = None) -> str:
    return f"""
(() => {{
  const pageParam = {_json(page_param)};
  const startPage = {int(start_page)};
  const step = {int(step)};
  const template = {_json(url_template)};
  const currentUrl = new URL(window.location.href);
  const currentPage = parseInt(currentUrl.searchParams.get(pageParam) || String(startPage), 10);
  const nextPage = currentPage + step;

  window.__jm_prev_url = location.href;
  window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 12000);
  window.__jm_clicked_pagination = true;

  if (template) {{
    window.location.href = template.replace('__PAGE__', String(nextPage));
  }} else {{
    currentUrl.searchParams.set(pageParam, String(nextPage));
    window.location.href = currentUrl.toString();
  }}
  return `navigating to page ${{nextPage}}`;
}})();
"""


def build_url_param_pagination_wait_js(fallback_wait_for_js: str = "") -> str:
    if fallback_wait_for_js:
        return fallback_wait_for_js
    return build_anchor_pagination_wait_js()


def build_infinite_scroll_js() -> str:
    return """
(() => {
  window.__jm_prev_scroll_y = window.scrollY;
  window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 12000);
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });
  return 'scrolled to bottom';
})();
"""


def build_infinite_scroll_wait_js() -> str:
    return """
js:() => {
  const currentBody = (document.body.innerText || '').slice(0, 12000);
  return window.scrollY !== (window.__jm_prev_scroll_y || 0) || currentBody !== (window.__jm_prev_body_sig || '');
}
"""


def build_detail_button_click_js(*, index: int, selector: str, text_patterns: list[str], exact_text: str | None = None) -> str:
    patterns = [p.lower() for p in text_patterns if p]
    return f"""
(() => {{
  {_VISIBLE_JS}
  const idx = {int(index)};
  const selector = {_json(selector)};
  const patterns = {_json(patterns)};
  const exactText = {_json(exact_text.lower() if exact_text else None)};
  const buttons = [...document.querySelectorAll(selector)]
    .filter(jmVisible)
    .filter(el => {{
      const text = jmNorm(el.innerText || el.textContent || el.value);
      if (exactText) return text === exactText;
      return patterns.some(p => text === p || text.includes(p));
    }});
  const target = buttons[idx] || null;
  window.__jm_prev_url = location.href;
  window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 12000);
  window.__jm_listing_url = location.href;
  window.__jm_clicked_detail = !!target;
  if (target) jmClick(target);
  return target ? `clicked detail button ${{idx}}` : `detail button ${{idx}} not found`;
}})();
"""


def build_detail_button_wait_js() -> str:
    return """
js:() => {
  if (window.__jm_clicked_detail === false) return true;
  const currentBody = (document.body.innerText || '').slice(0, 12000);
  return (
    location.href !== (window.__jm_prev_url || location.href) ||
    currentBody !== (window.__jm_prev_body_sig || '')
  );
}
"""


def build_back_to_listing_js(page_url: str) -> str:
    return f"""
(() => {{
  const fallbackUrl = {_json(page_url)};
  const prev = location.href;
  history.back();
  setTimeout(() => {{
    if (location.href === prev) {{
      location.href = window.__jm_listing_url || fallbackUrl;
    }}
  }}, 900);
  return 'back to listing requested';
}})();
"""
