from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..crawl.browser_evidence import BrowserEvidenceReport, BrowserEvidenceSession
from .safety import PortalUrlSafetyError, validate_public_http_url


GENERALIZED_DOM_PAGINATION_CONTRACT_VERSION = "1.0"


_ADVANCE_LISTING_SCRIPT = r"""
(options) => {
  const probeOnly = Boolean(options && options.probeOnly);
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (element) => {
    if (!element || !element.isConnected || element.hidden) return false;
    if (element.getAttribute('aria-hidden') === 'true') return false;
    let style;
    try { style = window.getComputedStyle(element); } catch (_) { return false; }
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const disabled = (element) => Boolean(
    element.disabled ||
    element.getAttribute('aria-disabled') === 'true' ||
    /(^|\s)(disabled|is-disabled)(\s|$)/i.test(String(element.className || ''))
  );
  const label = (element) => normalize(
    element.getAttribute('aria-label') ||
    element.innerText ||
    element.textContent ||
    element.getAttribute('title') ||
    element.getAttribute('rel')
  ).slice(0, 240);
  const negative = /(apply|sign\s*in|log\s*in|register|job\s*alert|upload|submit\s+resume|previous)/i;
  const exactNext = /^(next|next page|load more|show more|view more|see more)( jobs?| openings?| results?| positions?)?\s*(?:[>›»]|arrow)?$/i;
  const explicitMore = /(load|show|view|see)\s+more\s+(jobs?|openings?|results?|positions?)/i;
  const candidates = [];
  const nodes = Array.from(document.querySelectorAll(
    'button, a[href], [role="button"], input[type="button"], input[type="submit"]'
  ));
  for (const element of nodes) {
    if (!visible(element)) continue;
    const text = label(element);
    const rel = normalize(element.getAttribute('rel')).toLowerCase();
    const aria = normalize(element.getAttribute('aria-label'));
    const href = normalize(element.getAttribute('href'));
    if (!text || negative.test(text)) continue;
    if (/\/(?:jobs?|positions?|requisitions?)\/\d+(?:\/|$)/i.test(href)) continue;
    let score = 0;
    if (rel.split(/\s+/).includes('next')) score += 100;
    if (explicitMore.test(text)) score += 90;
    if (exactNext.test(text)) score += 80;
    if (/next/i.test(aria)) score += 30;
    if (/[?&](?:page|p|offset|start)=\d+/i.test(href)) score += 25;
    if (score <= 0) continue;
    candidates.push({element, text, score, disabled: disabled(element)});
  }
  candidates.sort((left, right) => right.score - left.score);
  const enabled = candidates.find((candidate) => !candidate.disabled);
  if (enabled) {
    if (probeOnly) {
      return {
        action: 'probe_click',
        label: enabled.text,
        score: enabled.score,
        enabled_controls: candidates.filter((candidate) => !candidate.disabled).length,
        disabled_controls: candidates.filter((candidate) => candidate.disabled).length
      };
    }
    try { enabled.element.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (_) {}
    try {
      enabled.element.click();
      return {
        action: 'click',
        label: enabled.text,
        enabled_controls: candidates.filter((candidate) => !candidate.disabled).length,
        disabled_controls: candidates.filter((candidate) => candidate.disabled).length
      };
    } catch (error) {
      return {
        action: 'click_failed',
        label: enabled.text,
        error: String(error && error.message || error),
        enabled_controls: candidates.filter((candidate) => !candidate.disabled).length,
        disabled_controls: candidates.filter((candidate) => candidate.disabled).length
      };
    }
  }
  const root = document.scrollingElement || document.documentElement || document.body;
  const height = Math.max(
    Number(root && root.scrollHeight || 0),
    Number(document.body && document.body.scrollHeight || 0)
  );
  const viewport = Math.max(Number(window.innerHeight || 0), 1);
  const before = Math.max(Number(window.scrollY || root && root.scrollTop || 0), 0);
  const atBottom = before + viewport >= height - 4;
  if (probeOnly) {
    return {
      action: 'probe_scroll',
      label: null,
      score: Math.max(0, height - viewport),
      at_bottom_before: atBottom,
      height_before: height,
      viewport,
      scroll_before: before,
      enabled_controls: 0,
      disabled_controls: candidates.filter((candidate) => candidate.disabled).length
    };
  }
  try { window.scrollTo({top: height, behavior: 'auto'}); } catch (_) { window.scrollTo(0, height); }
  return {
    action: 'scroll',
    label: null,
    at_bottom_before: atBottom,
    height_before: height,
    scroll_before: before,
    enabled_controls: 0,
    disabled_controls: candidates.filter((candidate) => candidate.disabled).length
  };
}
"""


_LISTING_STATE_SCRIPT = r"""
() => {
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (element) => {
    if (!element || !element.isConnected || element.hidden) return false;
    if (element.getAttribute('aria-hidden') === 'true') return false;
    let style;
    try { style = window.getComputedStyle(element); } catch (_) { return false; }
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const disabled = (element) => Boolean(
    element.disabled ||
    element.getAttribute('aria-disabled') === 'true' ||
    /(^|\s)(disabled|is-disabled)(\s|$)/i.test(String(element.className || ''))
  );
  const positive = /^(next(?:\s+page)?|(?:load|show|view|see)\s+more)(?:\s+(?:jobs?|openings?|results?|positions?))?\s*(?:[>›»]|arrow)?$/i;
  let enabledControls = 0;
  let disabledControls = 0;
  for (const element of Array.from(document.querySelectorAll(
    'button, a[href], [role="button"], input[type="button"], input[type="submit"]'
  ))) {
    if (!visible(element)) continue;
    const text = normalize(
      element.getAttribute('aria-label') ||
      element.innerText ||
      element.textContent ||
      element.getAttribute('title') ||
      element.getAttribute('rel')
    );
    if (!positive.test(text) || /(apply|sign\s*in|log\s*in|register|previous)/i.test(text)) continue;
    const href = normalize(element.getAttribute('href'));
    if (/\/(?:jobs?|positions?|requisitions?)\/\d+(?:\/|$)/i.test(href)) continue;
    if (disabled(element)) disabledControls += 1;
    else enabledControls += 1;
  }
  const root = document.scrollingElement || document.documentElement || document.body;
  const height = Math.max(
    Number(root && root.scrollHeight || 0),
    Number(document.body && document.body.scrollHeight || 0)
  );
  const viewport = Math.max(Number(window.innerHeight || 0), 1);
  const scroll = Math.max(Number(window.scrollY || root && root.scrollTop || 0), 0);
  return {
    height,
    viewport,
    scroll,
    at_bottom: scroll + viewport >= height - 4,
    enabled_controls: enabledControls,
    disabled_controls: disabledControls
  };
}
"""


@dataclass(frozen=True)
class GeneralizedDomPaginationOptions:
    stable_rounds_required: int = 2
    stalled_control_rounds: int = 3
    settle_time_ms: int = 1_200
    load_state_timeout_ms: int = 8_000

    def __post_init__(self) -> None:
        if not 1 <= self.stable_rounds_required <= 10:
            raise ValueError("stable_rounds_required must be between 1 and 10")
        if not 1 <= self.stalled_control_rounds <= 10:
            raise ValueError("stalled_control_rounds must be between 1 and 10")
        if not 0 <= self.settle_time_ms <= 30_000:
            raise ValueError("settle_time_ms must be between 0 and 30000")
        if not 500 <= self.load_state_timeout_ms <= 60_000:
            raise ValueError("load_state_timeout_ms must be between 500 and 60000")


@dataclass(frozen=True)
class GeneralizedDomPaginationStep:
    report: BrowserEvidenceReport
    action: str
    label: str | None
    state: dict[str, Any]
    errors: tuple[str, ...] = ()


class GeneralizedDomPaginationDriver:
    """Advance a public listing without XPath, CSS configuration, or site rules.

    The JavaScript is a bounded interaction policy, not a persisted selector.
    It considers only visible generic pagination controls and bottom scrolling;
    every resulting document is captured again through the existing public-host
    evidence boundary.
    """

    def __init__(self, options: GeneralizedDomPaginationOptions | None = None) -> None:
        self.options = options or GeneralizedDomPaginationOptions()

    async def advance(
        self,
        session: BrowserEvidenceSession,
        *,
        requested_url: str,
    ) -> GeneralizedDomPaginationStep:
        errors: list[str] = []
        raw_action: Any
        selected_frame = session.page
        try:
            ranked_frames: list[tuple[int, int, int, Any, dict[str, Any]]] = []
            frames = list(getattr(session.page, "frames", []) or [session.page])
            for position, frame in enumerate(frames):
                frame_url = str(getattr(frame, "url", "") or requested_url)
                try:
                    validate_public_http_url(frame_url, allowed_hosts=session.allowed_hosts)
                except PortalUrlSafetyError:
                    continue
                try:
                    probe = await frame.evaluate(
                        _ADVANCE_LISTING_SCRIPT,
                        {"probeOnly": True},
                    )
                except Exception as exc:
                    errors.append(
                        f"pagination_frame_probe_failed: {type(exc).__name__}: {exc}"[:1_000]
                    )
                    continue
                payload = probe if isinstance(probe, dict) else {}
                has_control = int(payload.get("enabled_controls") or 0) > 0
                score = max(0, int(payload.get("score") or 0))
                ranked_frames.append((int(has_control), score, -position, frame, payload))
            if ranked_frames:
                _, _, _, selected_frame, _ = max(
                    ranked_frames,
                    key=lambda item: (item[0], item[1], item[2]),
                )
            raw_action = await selected_frame.evaluate(
                _ADVANCE_LISTING_SCRIPT,
                {"probeOnly": False},
            )
        except Exception as exc:
            raw_action = {"action": "interaction_failed", "label": None}
            errors.append(f"pagination_interaction_failed: {type(exc).__name__}: {exc}"[:1_000])
        action_payload = raw_action if isinstance(raw_action, dict) else {}
        action = str(action_payload.get("action") or "unknown")[:100]
        label = str(action_payload.get("label") or "").strip()[:240] or None

        try:
            await session.page.wait_for_load_state(
                "networkidle",
                timeout=self.options.load_state_timeout_ms,
            )
        except Exception as exc:
            errors.append(f"pagination_network_idle_timeout: {type(exc).__name__}: {exc}"[:1_000])
        if self.options.settle_time_ms:
            try:
                await session.page.wait_for_timeout(self.options.settle_time_ms)
            except Exception as exc:
                errors.append(f"pagination_settle_failed: {type(exc).__name__}: {exc}"[:1_000])

        snapshot = await session.collector.snapshot_current_page(
            session.page,
            requested_url,
            allowed_hosts=session.allowed_hosts,
        )
        try:
            raw_state = await selected_frame.evaluate(_LISTING_STATE_SCRIPT)
        except Exception as exc:
            raw_state = {}
            errors.append(f"pagination_state_failed: {type(exc).__name__}: {exc}"[:1_000])
        state = raw_state if isinstance(raw_state, dict) else {}
        state = {
            "height": max(0, int(state.get("height") or 0)),
            "viewport": max(0, int(state.get("viewport") or 0)),
            "scroll": max(0, int(state.get("scroll") or 0)),
            "at_bottom": state.get("at_bottom") is True,
            "enabled_controls": max(0, int(state.get("enabled_controls") or 0)),
            "disabled_controls": max(0, int(state.get("disabled_controls") or 0)),
        }
        return GeneralizedDomPaginationStep(
            report=snapshot,
            action=action,
            label=label,
            state=state,
            errors=tuple(errors),
        )
