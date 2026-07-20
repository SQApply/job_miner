from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator


DOM_SNAPSHOT_CONTRACT_VERSION = "1.0"
NODE_MAP_GLOBAL = "__jobMinerNodeMap"


class DomEvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DomRect(DomEvidenceModel):
    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class DomNodeEvidence(DomEvidenceModel):
    node_token: str = Field(min_length=1, max_length=200)
    parent_token: str | None = Field(default=None, max_length=200)
    depth: int = Field(ge=0, le=200)
    tag: str = Field(min_length=1, max_length=80)
    role: str | None = Field(default=None, max_length=100)
    text: str | None = Field(default=None, max_length=2_000)
    href: str | None = Field(default=None, max_length=4_096)
    attributes: dict[str, str] = Field(default_factory=dict)
    clickable: bool = False
    visible: bool = True
    in_shadow_tree: bool = False
    shadow_host: bool = False
    child_element_count: int = Field(default=0, ge=0)
    structural_signature: str = Field(default="", max_length=1_000)
    rect: DomRect | None = None

    @field_validator("role", "text", "href", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = " ".join(str(value).split())
        return normalized or None


class InlineJsonEvidence(DomEvidenceModel):
    script_id: str | None = Field(default=None, max_length=300)
    script_type: str = Field(default="application/json", max_length=200)
    text_length: int = Field(default=0, ge=0)
    truncated: bool = False
    sample_sha256: str = Field(min_length=64, max_length=64)
    payload: Any | None = None
    parse_error: str | None = Field(default=None, max_length=500)


class FrameDomSnapshot(DomEvidenceModel):
    contract_version: str = DOM_SNAPSHOT_CONTRACT_VERSION
    frame_id: str = Field(min_length=1, max_length=100)
    frame_url: str = Field(min_length=1, max_length=4_096)
    title: str | None = Field(default=None, max_length=1_000)
    html_length: int = Field(default=0, ge=0)
    nodes: list[DomNodeEvidence] = Field(default_factory=list)
    inline_json: list[InlineJsonEvidence] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = Field(default=None, max_length=2_000)

    @property
    def clickable_nodes(self) -> list[DomNodeEvidence]:
        return [node for node in self.nodes if node.clickable]

    @property
    def linkless_clickable_nodes(self) -> list[DomNodeEvidence]:
        return [node for node in self.nodes if node.clickable and not node.href]


def frame_snapshot_from_payload(
    payload: Any,
    *,
    frame_id: str,
    frame_url: str,
    json_sanitizer: Callable[[Any], Any] | None = None,
) -> FrameDomSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("DOM snapshot evaluation must return an object")
    inline_json: list[InlineJsonEvidence] = []
    for item in list(payload.get("inlineJson") or []):
        if not isinstance(item, dict):
            continue
        raw_sample = str(item.get("raw_sample") or "")
        parsed_payload: Any | None = None
        parse_error: str | None = None
        if raw_sample and not bool(item.get("truncated")):
            try:
                parsed_payload = json.loads(raw_sample)
            except (TypeError, ValueError) as exc:
                parse_error = f"{type(exc).__name__}: {exc}"[:500]
            else:
                if json_sanitizer is not None:
                    parsed_payload = json_sanitizer(parsed_payload)
        inline_json.append(
            InlineJsonEvidence(
                script_id=item.get("script_id"),
                script_type=str(item.get("script_type") or "application/json"),
                text_length=int(item.get("text_length") or 0),
                truncated=bool(item.get("truncated")),
                sample_sha256=hashlib.sha256(raw_sample.encode("utf-8")).hexdigest(),
                payload=parsed_payload,
                parse_error=parse_error,
            )
        )
    return FrameDomSnapshot(
        frame_id=frame_id,
        frame_url=str(payload.get("url") or frame_url),
        title=payload.get("title"),
        html_length=int(payload.get("htmlLength") or 0),
        nodes=list(payload.get("nodes") or []),
        inline_json=inline_json,
        truncated=bool(payload.get("truncated")),
    )


# Evaluated independently inside every approved Playwright frame.  The node map
# stores live Element references for Phase 7C interaction without persisting an
# XPath or fragile CSS selector.
DOM_SNAPSHOT_SCRIPT = r"""
(options) => {
  const maxNodes = Math.max(1, Number(options.maxNodes || 12000));
  const maxText = Math.max(32, Number(options.maxTextChars || 700));
  const maxAttrValue = Math.max(16, Number(options.maxAttributeValueChars || 300));
  const maxAttrs = Math.max(1, Number(options.maxAttributesPerNode || 20));
  const maxInlineScripts = Math.max(0, Number(options.maxInlineScripts || 30));
  const maxInlineJsonChars = Math.max(0, Number(options.maxInlineJsonChars || 500000));
  const includeHidden = Boolean(options.includeHidden);
  const tokenPrefix = String(options.tokenPrefix || 'f0');
  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const clamp = (value, limit) => normalize(value).slice(0, limit);
  const round = (value) => Math.round(Number(value || 0) * 100) / 100;

  const sensitiveName = /(password|passwd|secret|token|cookie|session|csrf|authorization|api[-_]?key)/i;
  const safeExact = new Set([
    'id', 'class', 'role', 'name', 'type', 'title', 'aria-label',
    'aria-labelledby', 'aria-describedby', 'aria-expanded', 'aria-controls',
    'aria-current', 'data-job-id', 'data-requisition-id', 'data-req-id',
    'data-id', 'data-automation-id', 'data-testid'
  ]);
  const safeAttribute = (name) => {
    const lowered = String(name || '').toLowerCase();
    if (!lowered || sensitiveName.test(lowered)) return false;
    if (safeExact.has(lowered) || lowered.startsWith('aria-')) return true;
    return /^data-(job|req|requisition|position|posting|vacancy|automation|test)/.test(lowered);
  };

  const implicitRole = (element) => {
    const tag = element.tagName.toLowerCase();
    if (tag === 'a' && element.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'main') return 'main';
    if (tag === 'nav') return 'navigation';
    if (tag === 'article') return 'article';
    if (tag === 'section') return 'region';
    if (tag === 'li') return 'listitem';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = String(element.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    return null;
  };

  const isVisible = (element) => {
    if (element.hidden || element.getAttribute('aria-hidden') === 'true') return false;
    let style;
    try { style = window.getComputedStyle(element); } catch (_) { return false; }
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    if (Number(style.opacity || 1) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const nodeMap = new Map();
  window.__jobMinerNodeMap = nodeMap;
  const nodes = [];
  let sequence = 0;
  let truncated = false;

  const walk = (element, parentToken, depth, inShadowTree) => {
    if (!(element instanceof Element)) return;
    if (nodes.length >= maxNodes) { truncated = true; return; }
    const tag = element.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'template', 'meta', 'link'].includes(tag)) return;
    const visible = isVisible(element);
    if (!includeHidden && !visible) return;

    const nodeToken = `${tokenPrefix}:n${++sequence}`;
    nodeMap.set(nodeToken, element);
    const explicitRole = clamp(element.getAttribute('role'), 100);
    const role = explicitRole || implicitRole(element);
    const clickable = Boolean(
      ['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag) ||
      ['button', 'link', 'menuitem', 'tab', 'option'].includes(role) ||
      element.hasAttribute('onclick') || Number(element.tabIndex) >= 0
    );
    const directText = Array.from(element.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent || '')
      .join(' ');
    let text = directText;
    if (!normalize(text) && (clickable || role === 'heading' || element.children.length === 0)) {
      text = element.innerText || element.textContent || '';
    }
    const ariaText = element.getAttribute('aria-label') || element.getAttribute('title') || '';
    text = clamp([ariaText, text].filter(Boolean).join(' '), maxText) || null;

    const attributes = {};
    for (const attribute of Array.from(element.attributes || [])) {
      if (Object.keys(attributes).length >= maxAttrs) break;
      if (!safeAttribute(attribute.name)) continue;
      attributes[attribute.name.toLowerCase()] = clamp(attribute.value, maxAttrValue);
    }
    const rect = element.getBoundingClientRect();
    const childTags = Array.from(element.children || [])
      .slice(0, 16)
      .map((child) => child.tagName.toLowerCase());
    const href = element.href && /^https?:/i.test(String(element.href))
      ? String(element.href).slice(0, 4096)
      : null;
    nodes.push({
      node_token: nodeToken,
      parent_token: parentToken || null,
      depth,
      tag,
      role: role || null,
      text,
      href,
      attributes,
      clickable,
      visible,
      in_shadow_tree: Boolean(inShadowTree),
      shadow_host: Boolean(element.shadowRoot),
      child_element_count: Number(element.children.length || 0),
      structural_signature: `${tag}|${role || ''}|${clickable ? 'c' : ''}|${childTags.join(',')}`.slice(0, 1000),
      rect: {
        x: round(rect.x), y: round(rect.y),
        width: Math.max(0, round(rect.width)),
        height: Math.max(0, round(rect.height))
      }
    });

    if (element.shadowRoot) {
      for (const child of Array.from(element.shadowRoot.children || [])) {
        walk(child, nodeToken, depth + 1, true);
        if (truncated) return;
      }
    }
    for (const child of Array.from(element.children || [])) {
      walk(child, nodeToken, depth + 1, inShadowTree);
      if (truncated) return;
    }
  };

  const root = document.body || document.documentElement;
  if (root) walk(root, null, 0, false);

  const inlineJson = [];
  const jsonScripts = Array.from(document.scripts || []).filter((script) => {
    const type = String(script.type || '').toLowerCase().split(';', 1)[0];
    return type === 'application/json' || type === 'application/ld+json' ||
      script.id === '__NEXT_DATA__' || script.id === '__NUXT_DATA__';
  });
  for (const script of jsonScripts.slice(0, maxInlineScripts)) {
    const raw = String(script.textContent || '');
    inlineJson.push({
      script_id: clamp(script.id, 300) || null,
      script_type: clamp(script.type || 'application/json', 200),
      text_length: raw.length,
      truncated: raw.length > maxInlineJsonChars,
      raw_sample: raw.slice(0, maxInlineJsonChars)
    });
  }

  return {
    url: String(location.href || ''),
    title: clamp(document.title, 1000) || null,
    htmlLength: document.documentElement ? document.documentElement.outerHTML.length : 0,
    nodes,
    inlineJson,
    truncated
  };
}
"""
