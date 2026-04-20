# # from __future__ import annotations

# # import json
# # from urllib.parse import urlparse

# # from ..crawl.browser_lane import close_session, interaction_run_config, listing_run_config
# # from ..crawl.link_collector import collect_job_links
# # from ..utils import unique_keep_order
# # from .base import BaseAdapter

# # from pathlib import Path
# # import json
# # import base64


# # def _dump_debug_state(name: str, result) -> None:
# #     debug_dir = Path("data/debug")
# #     debug_dir.mkdir(parents=True, exist_ok=True)

# #     html = getattr(result, "cleaned_html", None) or getattr(result, "html", None) or ""
# #     final_url = getattr(result, "url", None) or getattr(result, "final_url", None) or ""
# #     title = getattr(result, "title", None) or ""

# #     (debug_dir / f"{name}.html").write_text(html, encoding="utf-8")

# #     meta = {
# #         "success": getattr(result, "success", None),
# #         "error_message": getattr(result, "error_message", None),
# #         "final_url": final_url,
# #         "title": title,
# #     }
# #     (debug_dir / f"{name}_meta.json").write_text(
# #         json.dumps(meta, indent=2, ensure_ascii=False),
# #         encoding="utf-8",
# #     )

# #     screenshot_b64 = getattr(result, "screenshot", None)
# #     if screenshot_b64:
# #         try:
# #             screenshot_bytes = base64.b64decode(screenshot_b64)
# #             (debug_dir / f"{name}.png").write_bytes(screenshot_bytes)
# #         except Exception:
# #             pass

# # def _dump_debug_state(name: str, result) -> None:
# #     debug_dir = Path("data/debug")
# #     debug_dir.mkdir(parents=True, exist_ok=True)

# #     html = getattr(result, "cleaned_html", None) or getattr(result, "html", None) or ""
# #     final_url = getattr(result, "url", None) or getattr(result, "final_url", None) or ""
# #     title = getattr(result, "title", None) or ""

# #     (debug_dir / f"{name}.html").write_text(html, encoding="utf-8")
# #     (debug_dir / f"{name}_meta.json").write_text(
# #         json.dumps(
# #             {
# #                 "success": getattr(result, "success", None),
# #                 "error_message": getattr(result, "error_message", None),
# #                 "final_url": final_url,
# #                 "title": title,
# #             },
# #             indent=2,
# #             ensure_ascii=False,
# #         ),
# #         encoding="utf-8",
# #     )


# # def _build_pagination_click_js(next_text_patterns: list[str]) -> str:
# #     labels_json = json.dumps([x.lower() for x in next_text_patterns])

# #     return f"""
# #     (() => {{
# #       const labels = new Set({labels_json});

# #       const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

# #       const isVisible = (el) => {{
# #         if (!el) return false;
# #         const style = window.getComputedStyle(el);
# #         const rect = el.getBoundingClientRect();
# #         return style.display !== 'none' &&
# #                style.visibility !== 'hidden' &&
# #                rect.width > 0 &&
# #                rect.height > 0;
# #       }};

# #       const nodes = [...document.querySelectorAll('a, button, [role="button"], span, div, li')]
# #         .filter(isVisible);

# #       const digitNodes = nodes.filter(el => /^\\d+$/.test(norm(el.innerText || el.textContent)));
# #       const activeNode = nodes.find(el => {{
# #         const aria = (el.getAttribute('aria-current') || '').toLowerCase();
# #         const cls = (el.className || '').toString().toLowerCase();
# #         return aria === 'page' || cls.includes('active') || cls.includes('selected') || cls.includes('current');
# #       }});

# #       let activeNum = null;
# #       if (activeNode) {{
# #         const t = norm(activeNode.innerText || activeNode.textContent);
# #         if (/^\\d+$/.test(t)) activeNum = parseInt(t, 10);
# #       }}

# #       let target = null;

# #       if (activeNum !== null) {{
# #         target = digitNodes.find(el => {{
# #           const t = norm(el.innerText || el.textContent);
# #           return /^\\d+$/.test(t) && parseInt(t, 10) === activeNum + 1;
# #         }});
# #       }}

# #       if (!target) {{
# #         target = nodes.find(el => labels.has(norm(el.innerText || el.textContent)));
# #       }}

# #       const rowSig = nodes
# #         .map(el => norm(el.innerText || el.textContent))
# #         .filter(Boolean)
# #         .slice(0, 120)
# #         .join('|')
# #         .slice(0, 6000);

# #       window.__jm_prev_row_sig = rowSig;
# #       window.__jm_prev_active_num = activeNum;
# #       window.__jm_prev_url = location.href;
# #       window.__jm_clicked_pagination = !!target;

# #       if (target) {{
# #         target.scrollIntoView({{ behavior: 'instant', block: 'center' }});
# #         target.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
# #         target.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
# #         target.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, view: window }}));
# #         if (typeof target.click === 'function') target.click();
# #       }}
# #     }})();
# #     """


# # def _build_pagination_wait_js() -> str:
# #     return """
# #     js:() => {
# #       const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

# #       const isVisible = (el) => {
# #         if (!el) return false;
# #         const style = window.getComputedStyle(el);
# #         const rect = el.getBoundingClientRect();
# #         return style.display !== 'none' &&
# #                style.visibility !== 'hidden' &&
# #                rect.width > 0 &&
# #                rect.height > 0;
# #       };

# #       const nodes = [...document.querySelectorAll('a, button, [role="button"], span, div, li')]
# #         .filter(isVisible);

# #       const activeNode = nodes.find(el => {
# #         const aria = (el.getAttribute('aria-current') || '').toLowerCase();
# #         const cls = (el.className || '').toString().toLowerCase();
# #         return aria === 'page' || cls.includes('active') || cls.includes('selected') || cls.includes('current');
# #       });

# #       let activeNum = null;
# #       if (activeNode) {
# #         const t = norm(activeNode.innerText || activeNode.textContent);
# #         if (/^\\d+$/.test(t)) activeNum = parseInt(t, 10);
# #       }

# #       const rowSig = nodes
# #         .map(el => norm(el.innerText || el.textContent))
# #         .filter(Boolean)
# #         .slice(0, 120)
# #         .join('|')
# #         .slice(0, 6000);

# #       if (window.__jm_clicked_pagination === false) return true;

# #       return (
# #         activeNum !== window.__jm_prev_active_num ||
# #         rowSig !== window.__jm_prev_row_sig ||
# #         location.href !== window.__jm_prev_url
# #       );
# #     }
# #     """


# # def _build_detail_button_click_js(index: int, text_patterns: list[str]) -> str:
# #     patterns_json = json.dumps([x.lower() for x in text_patterns])

# #     return f"""
# #     (() => {{
# #       const idx = {index};
# #       const patterns = {patterns_json};

# #       const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
# #       const isVisible = (el) => {{
# #         if (!el) return false;
# #         const style = window.getComputedStyle(el);
# #         const rect = el.getBoundingClientRect();
# #         return style.display !== 'none' &&
# #                style.visibility !== 'hidden' &&
# #                rect.width > 0 &&
# #                rect.height > 0;
# #       }};

# #       const buttons = [...document.querySelectorAll('a, button, [role="button"], span, div')]
# #         .filter(isVisible)
# #         .filter(el => patterns.some(p => norm(el.innerText || el.textContent).includes(p)));

# #       const target = buttons[idx] || null;

# #       window.__jm_prev_url = location.href;
# #       window.__jm_prev_body_sig = (document.body.innerText || '').slice(0, 6000);

# #       if (target) {{
# #         target.scrollIntoView({{ behavior: 'instant', block: 'center' }});
# #         target.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
# #         target.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
# #         target.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, view: window }}));
# #         if (typeof target.click === 'function') target.click();
# #       }}
# #     }})();
# #     """


# # def _build_detail_button_wait_js() -> str:
# #     return """
# #     js:() => {
# #       return (
# #         location.href !== (window.__jm_prev_url || location.href) ||
# #         (document.body.innerText || '').slice(0, 6000) !== (window.__jm_prev_body_sig || '')
# #       );
# #     }
# #     """


# # def _build_back_to_listing_js(page_url: str) -> str:
# #     page_url_json = json.dumps(page_url)
# #     return f"""
# #     (() => {{
# #       const pageUrl = {page_url_json};
# #       const prev = location.href;
# #       history.back();
# #       setTimeout(() => {{
# #         if (location.href === prev) {{
# #           location.href = pageUrl;
# #         }}
# #       }}, 700);
# #     }})();
# #     """


# # class PaginatedAdapter(BaseAdapter):
# #     def _pagination_click_js(self, listing) -> str:
# #       return listing.pagination.click_js_override or _build_pagination_click_js(listing.pagination.next_text_patterns)

# #     def _pagination_wait_js(self, listing) -> str:
# #       return listing.pagination.wait_for_js_override or _build_pagination_wait_js()

# #     def _detail_click_js(self, listing, index: int) -> str:
# #       template = listing.detail_click_js_template
# #       if template:
# #           return template.replace("__INDEX__", str(index))
# #       return _build_detail_button_click_js(index, listing.detail_text_patterns)

# #     def _detail_wait_js(self, listing) -> str:
# #       return listing.detail_wait_for or _build_detail_button_wait_js()

# #     def _back_js(self, listing) -> str:
# #       return listing.back_to_listing_js or _build_back_to_listing_js(listing.page_url)

# #     def _back_wait_for(self, listing) -> str:
# #       return listing.back_to_listing_wait_for or listing.initial_wait_for or 'js:() => document.body && document.body.innerText.length > 0'

# #     async def _collect_by_clicking_detail_buttons(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
# #         settings = system_config.browser
# #         listing = blueprint.listing
# #         urls: list[str] = []

# #         for idx in range(50):
# #             try:
# #                 result = await crawler.arun(
# #                     url=listing.page_url,
# #                     config=interaction_run_config(
# #                         settings,
# #                         listing.session_id,
# #                         self._detail_click_js(listing, idx),
# #                         self._detail_wait_js(listing),
# #                     ),
# #                 )
# #                 if idx == 0:
# #                     _dump_debug_state("jobdiva_after_detail_click_0", result)
# #             except Exception as exc:
# #                 if session_logger:
# #                     session_logger.log(
# #                         "detail_button_click_exception",
# #                         page_url=listing.page_url,
# #                         button_index=idx,
# #                         error_message=str(exc),
# #                     )
# #                 break

# #             if not result.success:
# #                 if session_logger:
# #                     session_logger.log(
# #                         "detail_button_click_failed",
# #                         page_url=listing.page_url,
# #                         button_index=idx,
# #                         error_message=result.error_message,
# #                     )
# #                 break

# #             final_url = (getattr(result, "url", None) or getattr(result, "final_url", None) or "").strip()
# #             parsed = urlparse(final_url) if final_url else None

# #             if final_url and parsed and parsed.netloc in blueprint.allowed_hosts and final_url.rstrip("/") != listing.page_url.rstrip("/"):
# #                 urls.append(final_url.rstrip("/"))
# #                 if session_logger:
# #                     session_logger.log(
# #                         "detail_button_captured",
# #                         page_url=listing.page_url,
# #                         button_index=idx,
# #                         captured_url=final_url.rstrip("/"),
# #                     )

# #             try:
# #                 back_result = await crawler.arun(
# #                     url=listing.page_url,
# #                     config=interaction_run_config(
# #                         settings,
# #                         listing.session_id,
# #                         self._back_js(listing),
# #                         self._back_wait_for(listing),
# #                     ),
# #                 )
# #                 if idx == 0:
# #                     _dump_debug_state("jobdiva_after_back_attempt_0", back_result)
# #                 if not back_result.success and session_logger:
# #                     session_logger.log(
# #                         "back_to_listing_failed",
# #                         page_url=listing.page_url,
# #                         button_index=idx,
# #                         error_message=back_result.error_message,
# #                     )
# #             except Exception as exc:
# #                 if session_logger:
# #                     session_logger.log(
# #                         "back_to_listing_exception",
# #                         page_url=listing.page_url,
# #                         button_index=idx,
# #                         error_message=str(exc),
# #                     )
# #                 break

# #         return unique_keep_order(urls)

# #     async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
# #         settings = system_config.browser
# #         listing = blueprint.listing

# #         if session_logger:
# #             session_logger.log(
# #                 "listing_start",
# #                 page_url=listing.page_url,
# #                 browser_session_id=listing.session_id,
# #             )

# #         initial_result = await crawler.arun(
# #             url=listing.page_url,
# #             config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
# #         )
# #         _dump_debug_state("jobdiva_listing_initial", initial_result)

# #         if not initial_result.success:
# #             if session_logger:
# #                 session_logger.log(
# #                     "listing_initial_failed",
# #                     page_url=listing.page_url,
# #                     error_message=initial_result.error_message,
# #                 )
# #             raise RuntimeError(f"Initial listing crawl failed: {initial_result.error_message}")
# #         if blueprint.id == "judge_jobs":
# #             _dump_debug_state("judge_listing_initial", initial_result)
        
# #         from pathlib import Path
# #         import base64

# #         debug_dir = Path("data/debug")
# #         debug_dir.mkdir(parents=True, exist_ok=True)

# #         if hasattr(initial_result, "screenshot") and initial_result.screenshot:
# #             screenshot_bytes = base64.b64decode(initial_result.screenshot)
# #             (debug_dir / "jobdiva_listing_initial.png").write_bytes(screenshot_bytes)

# #         latest_result = initial_result

# #         if listing.detail_capture_mode == "click_buttons":
# #             urls = await self._collect_by_clicking_detail_buttons(
# #                 crawler,
# #                 blueprint,
# #                 system_config,
# #                 session_logger=session_logger,
# #             )
# #         else:
# #             urls = collect_job_links(
# #                 latest_result,
# #                 page_url=listing.page_url,
# #                 allowed_hosts=blueprint.allowed_hosts,
# #                 href_contains=listing.item_href_contains,
# #                 detail_text_patterns=listing.detail_text_patterns,
# #                 exclude_exact_urls=listing.exclude_exact_urls,
# #             )

# #         if session_logger:
# #             session_logger.log(
# #                 "listing_initial_complete",
# #                 page_url=listing.page_url,
# #                 discovered_urls=len(urls),
# #             )

# #         stable_rounds = 0
# #         page_turns = 0

# #         while listing.pagination.enabled and page_turns < listing.pagination.max_pages:
# #             page_turns += 1

# #             if session_logger:
# #                 session_logger.log(
# #                     "pagination_click_start",
# #                     page_url=listing.page_url,
# #                     page_turn=page_turns,
# #                     discovered_urls=len(urls),
# #                 )

# #             try:
# #                 result = await crawler.arun(
# #                     url=listing.page_url,
# #                     config=interaction_run_config(
# #                         settings,
# #                         listing.session_id,
# #                         self._pagination_click_js(listing),
# #                         self._pagination_wait_js(listing),
# #                     ),
# #                 )
# #                 if blueprint.id == "judge_jobs" and page_turns == 1:
# #                     _dump_debug_state("judge_after_pagination_click_1", result)
# #                 # if page_turns == 1:
# #                 #     _dump_debug_state("jobdiva_after_pagination_click_1", result)
# #             except Exception as exc:
# #                 if session_logger:
# #                     session_logger.log(
# #                         "pagination_click_exception",
# #                         page_url=listing.page_url,
# #                         page_turn=page_turns,
# #                         error_message=str(exc),
# #                     )
# #                 break

# #             if not result.success:
# #                 stable_rounds += 1
# #                 if session_logger:
# #                     session_logger.log(
# #                         "pagination_click_failed",
# #                         page_url=listing.page_url,
# #                         page_turn=page_turns,
# #                         stable_rounds=stable_rounds,
# #                         error_message=result.error_message,
# #                     )
# #                 if stable_rounds >= listing.pagination.stop_after_stable_rounds:
# #                     break
# #                 continue

# #             latest_result = result

# #             if listing.detail_capture_mode == "click_buttons":
# #                 new_urls = await self._collect_by_clicking_detail_buttons(
# #                     crawler,
# #                     blueprint,
# #                     system_config,
# #                     session_logger=session_logger,
# #                 )
# #             else:
# #                 new_urls = collect_job_links(
# #                     latest_result,
# #                     page_url=listing.page_url,
# #                     allowed_hosts=blueprint.allowed_hosts,
# #                     href_contains=listing.item_href_contains,
# #                     detail_text_patterns=listing.detail_text_patterns,
# #                     exclude_exact_urls=listing.exclude_exact_urls,
# #                 )

# #             merged = unique_keep_order(urls + new_urls)

# #             if len(merged) > len(urls):
# #                 urls = merged
# #                 stable_rounds = 0
# #                 if session_logger:
# #                     session_logger.log(
# #                         "pagination_click_growth",
# #                         page_url=listing.page_url,
# #                         page_turn=page_turns,
# #                         discovered_urls=len(urls),
# #                     )
# #             else:
# #                 stable_rounds += 1
# #                 if session_logger:
# #                     session_logger.log(
# #                         "pagination_click_no_growth",
# #                         page_url=listing.page_url,
# #                         page_turn=page_turns,
# #                         stable_rounds=stable_rounds,
# #                         discovered_urls=len(urls),
# #                     )

# #                 if stable_rounds >= listing.pagination.stop_after_stable_rounds:
# #                     break

# #         await close_session(crawler, listing.session_id)

# #         if session_logger:
# #             session_logger.log(
# #                 "listing_complete",
# #                 page_url=listing.page_url,
# #                 discovered_urls=len(urls),
# #                 pagination_turns=page_turns,
# #             )

# #         return urls


# from __future__ import annotations

# from ..crawl.browser_lane import close_session, interaction_run_config, listing_run_config
# from ..crawl.link_collector import collect_job_links
# from ..utils import unique_keep_order
# from .base import BaseAdapter


# def _build_cookie_dismiss_js() -> str:
#     return """
#     (() => {
#       const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

#       const isVisible = (el) => {
#         if (!el) return false;
#         const style = window.getComputedStyle(el);
#         const rect = el.getBoundingClientRect();
#         return style.display !== 'none' &&
#                style.visibility !== 'hidden' &&
#                rect.width > 0 &&
#                rect.height > 0;
#       };

#       const patterns = ['accept', 'accept all', 'allow all', 'agree', 'i agree', 'got it', 'continue', 'ok'];

#       const nodes = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], span, div')]
#         .filter(isVisible);

#       const btn = nodes.find(el => {
#         const t = norm(el.innerText || el.textContent || el.value);
#         return patterns.some(p => t === p || t.includes(p));
#       });

#       if (btn) {
#         btn.scrollIntoView({ behavior: 'instant', block: 'center' });
#         btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
#         btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
#         btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
#         if (typeof btn.click === 'function') btn.click();
#       }
#     })();
#     """


# class PaginatedAdapter(BaseAdapter):
#     async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
#         settings = system_config.browser
#         listing = blueprint.listing

#         if session_logger:
#             session_logger.log(
#                 "listing_start",
#                 page_url=listing.page_url,
#                 browser_session_id=listing.session_id,
#             )

#         initial_result = await crawler.arun(
#             url=listing.page_url,
#             config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
#         )

#         if not initial_result.success:
#             if session_logger:
#                 session_logger.log(
#                     "listing_initial_failed",
#                     page_url=listing.page_url,
#                     error_message=initial_result.error_message,
#                 )
#             raise RuntimeError(f"Initial listing crawl failed: {initial_result.error_message}")

#         # Dismiss consent once inside the same session, then refresh listing state
#         try:
#             await crawler.arun(
#                 url=listing.page_url,
#                 config=interaction_run_config(
#                     settings,
#                     listing.session_id,
#                     _build_cookie_dismiss_js(),
#                     'js:() => true',
#                 ),
#             )
#         except Exception:
#             pass

#         latest_result = await crawler.arun(
#             url=listing.page_url,
#             config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
#         )

#         urls = collect_job_links(
#             latest_result,
#             page_url=listing.page_url,
#             allowed_hosts=blueprint.allowed_hosts,
#             href_contains=listing.item_href_contains,
#             detail_text_patterns=listing.detail_text_patterns,
#             exclude_exact_urls=listing.exclude_exact_urls,
#         )

#         if session_logger:
#             session_logger.log(
#                 "listing_initial_complete",
#                 page_url=listing.page_url,
#                 discovered_urls=len(urls),
#             )

#         stable_rounds = 0
#         turns = 0

#         while listing.pagination.enabled and turns < listing.pagination.max_turns:
#             turns += 1

#             if session_logger:
#                 session_logger.log(
#                     "pagination_click_start",
#                     page_url=listing.page_url,
#                     page_turn=turns,
#                     discovered_urls=len(urls),
#                 )

#             result = await crawler.arun(
#                 url=listing.page_url,
#                 config=interaction_run_config(
#                     settings,
#                     listing.session_id,
#                     listing.pagination.click_js,
#                     listing.pagination.wait_for_js,
#                 ),
#             )

#             if not result.success:
#                 stable_rounds += 1
#                 if session_logger:
#                     session_logger.log(
#                         "pagination_click_failed",
#                         page_url=listing.page_url,
#                         page_turn=turns,
#                         stable_rounds=stable_rounds,
#                         error_message=result.error_message,
#                     )
#                 if stable_rounds >= listing.pagination.stop_after_stable_rounds:
#                     break
#                 continue

#             latest_result = result

#             new_urls = collect_job_links(
#                 latest_result,
#                 page_url=listing.page_url,
#                 allowed_hosts=blueprint.allowed_hosts,
#                 href_contains=listing.item_href_contains,
#                 detail_text_patterns=listing.detail_text_patterns,
#                 exclude_exact_urls=listing.exclude_exact_urls,
#             )

#             merged = unique_keep_order(urls + new_urls)

#             if len(merged) > len(urls):
#                 urls = merged
#                 stable_rounds = 0
#                 if session_logger:
#                     session_logger.log(
#                         "pagination_click_growth",
#                         page_url=listing.page_url,
#                         page_turn=turns,
#                         discovered_urls=len(urls),
#                     )
#             else:
#                 stable_rounds += 1
#                 if session_logger:
#                     session_logger.log(
#                         "pagination_click_no_growth",
#                         page_url=listing.page_url,
#                         page_turn=turns,
#                         stable_rounds=stable_rounds,
#                         discovered_urls=len(urls),
#                     )
#                 if stable_rounds >= listing.pagination.stop_after_stable_rounds:
#                     break

#         await close_session(crawler, listing.session_id)

#         if session_logger:
#             session_logger.log(
#                 "listing_complete",
#                 page_url=listing.page_url,
#                 discovered_urls=len(urls),
#                 pagination_turns=turns,
#             )

#         return urls


from __future__ import annotations

from ..crawl.browser_lane import close_session, interaction_run_config, listing_run_config
from ..crawl.link_collector import (
    collect_job_links,
    page_has_multiple_pages,
    page_has_pagination,
)
from ..utils import unique_keep_order
from .base import BaseAdapter


def _build_cookie_dismiss_js() -> str:
    return """
    (() => {
      const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' &&
               style.visibility !== 'hidden' &&
               rect.width > 0 &&
               rect.height > 0;
      };

      const patterns = ['accept', 'accept all', 'allow all', 'agree', 'i agree', 'got it', 'continue', 'ok'];

      const nodes = [...document.querySelectorAll('button, a, [role="button"], input[type="button"], span, div')]
        .filter(isVisible);

      const btn = nodes.find(el => {
        const t = norm(el.innerText || el.textContent || el.value);
        return patterns.some(p => t === p || t.includes(p));
      });

      if (btn) {
        btn.scrollIntoView({ behavior: 'instant', block: 'center' });
        btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        if (typeof btn.click === 'function') btn.click();
      }
    })();
    """


class PaginatedAdapter(BaseAdapter):
    async def discover_job_urls(self, crawler, blueprint, system_config, session_logger=None) -> list[str]:
        settings = system_config.browser
        listing = blueprint.listing

        if session_logger:
            session_logger.log(
                "listing_start",
                page_url=listing.page_url,
                browser_session_id=listing.session_id,
            )

        initial_result = await crawler.arun(
            url=listing.page_url,
            config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
        )

        if not initial_result.success:
            if session_logger:
                session_logger.log(
                    "listing_initial_failed",
                    page_url=listing.page_url,
                    error_message=initial_result.error_message,
                )
            raise RuntimeError(f"Initial listing crawl failed: {initial_result.error_message}")

        # Dismiss cookies once in same session
        try:
            await crawler.arun(
                url=listing.page_url,
                config=interaction_run_config(
                    settings,
                    listing.session_id,
                    _build_cookie_dismiss_js(),
                    'js:() => true',
                ),
            )
        except Exception:
            pass

        latest_result = await crawler.arun(
            url=listing.page_url,
            config=listing_run_config(settings, listing.session_id, listing.initial_wait_for),
        )

        urls = collect_job_links(
            latest_result,
            page_url=listing.page_url,
            allowed_hosts=blueprint.allowed_hosts,
            href_contains=listing.item_href_contains,
            detail_text_patterns=listing.detail_text_patterns,
            exclude_exact_urls=listing.exclude_exact_urls,
        )

        initial_count = len(urls)
        has_pagination = page_has_pagination(latest_result)
        has_multiple_pages = page_has_multiple_pages(latest_result)

        if session_logger:
            session_logger.log(
                "listing_initial_complete",
                page_url=listing.page_url,
                discovered_urls=initial_count,
                has_pagination=has_pagination,
                has_multiple_pages=has_multiple_pages,
            )

        stable_rounds = 0
        turns = 0
        any_growth = False

        while listing.pagination.enabled and turns < listing.pagination.max_turns:
            turns += 1

            if session_logger:
                session_logger.log(
                    "pagination_click_start",
                    page_url=listing.page_url,
                    page_turn=turns,
                    discovered_urls=len(urls),
                )

            result = await crawler.arun(
                url=listing.page_url,
                config=interaction_run_config(
                    settings,
                    listing.session_id,
                    listing.pagination.click_js,
                    listing.pagination.wait_for_js,
                ),
            )

            if not result.success:
                stable_rounds += 1
                if session_logger:
                    session_logger.log(
                        "pagination_click_failed",
                        page_url=listing.page_url,
                        page_turn=turns,
                        stable_rounds=stable_rounds,
                        error_message=result.error_message,
                    )
                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break
                continue

            latest_result = result

            new_urls = collect_job_links(
                latest_result,
                page_url=listing.page_url,
                allowed_hosts=blueprint.allowed_hosts,
                href_contains=listing.item_href_contains,
                detail_text_patterns=listing.detail_text_patterns,
                exclude_exact_urls=listing.exclude_exact_urls,
            )

            merged = unique_keep_order(urls + new_urls)

            if len(merged) > len(urls):
                urls = merged
                stable_rounds = 0
                any_growth = True
                if session_logger:
                    session_logger.log(
                        "pagination_click_growth",
                        page_url=listing.page_url,
                        page_turn=turns,
                        discovered_urls=len(urls),
                    )
            else:
                stable_rounds += 1
                if session_logger:
                    session_logger.log(
                        "pagination_click_no_growth",
                        page_url=listing.page_url,
                        page_turn=turns,
                        stable_rounds=stable_rounds,
                        discovered_urls=len(urls),
                    )
                if stable_rounds >= listing.pagination.stop_after_stable_rounds:
                    break

        await close_session(crawler, listing.session_id)

        # Important: stop before detail extraction if pagination is present but stalled
        if listing.pagination.enabled and has_multiple_pages and initial_count > 0 and not any_growth:
            message = (
                "Pagination detected on listing page, but no new URLs were discovered "
                "after pagination attempts. Aborting before detail extraction."
            )
            if session_logger:
                session_logger.log(
                    "pagination_stalled_abort",
                    page_url=listing.page_url,
                    discovered_urls=len(urls),
                    pagination_turns=turns,
                    message=message,
                )
            raise RuntimeError(message)

        if session_logger:
            session_logger.log(
                "listing_complete",
                page_url=listing.page_url,
                discovered_urls=len(urls),
                pagination_turns=turns,
            )

        return urls