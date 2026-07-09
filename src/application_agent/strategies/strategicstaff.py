from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import StrategyDecision, SubmissionResult
from ..tools.resume_file import candidate_contact, resolve_resume_file
from ...common.constants import ApplicationRunStatuses, ApplicationStatuses


SUCCESS_PATTERNS = re.compile(r"(thank you|thanks for applying|application (has been )?(submitted|received)|successfully submitted|we received your application)", re.I)
ERROR_PATTERNS = re.compile(r"(required field|field is required|invalid email|upload.*required|please correct|error)", re.I)
CAPTCHA_PATTERNS = re.compile(r"(captcha|g-recaptcha|hcaptcha|cf-turnstile)", re.I)


class StrategicStaffDirectApplyStrategy:
    """Playwright strategy for careers.strategicstaff.com direct job pages.

    The current Strategic Staffing careers page exposes a simple Apply-now form
    with first name, last name, email, and resume upload. This strategy is
    intentionally domain-scoped; unsupported domains still go through manual
    review instead of a generic blind submitter.
    """

    strategy_key = "strategicstaff_direct_apply"
    supported_domains = {"careers.strategicstaff.com", "strategicstaff.com"}

    def can_apply(self, *, job: dict[str, Any], candidate_profile: dict[str, Any], apply_url: str | None) -> StrategyDecision:
        if not apply_url:
            return StrategyDecision(
                strategy_key=self.strategy_key,
                can_submit=False,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="This StrategicStaff job does not have an apply URL.",
                error_type="missing_apply_url",
            )
        domain = _domain(apply_url)
        if domain not in self.supported_domains:
            return StrategyDecision(
                strategy_key=self.strategy_key,
                can_submit=False,
                run_status=ApplicationRunStatuses.UNSUPPORTED_PORTAL,
                application_status=ApplicationStatuses.AGENT_UNSUPPORTED_PORTAL,
                message=f"StrategicStaff strategy does not support domain {domain or 'unknown'}.",
                error_type="unsupported_domain",
            )
        contact = candidate_contact(candidate_profile, job.get("__resume_profile") or {})
        missing = [name for name in ("first_name", "last_name", "email") if not contact.get(name)]
        if missing:
            return StrategyDecision(
                strategy_key=self.strategy_key,
                can_submit=False,
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Candidate profile is missing required apply fields: " + ", ".join(missing),
                error_type="candidate_required_fields_missing",
                metadata={"missing_fields": missing},
            )
        return StrategyDecision(
            strategy_key=self.strategy_key,
            can_submit=True,
            message="StrategicStaff direct-apply strategy selected.",
            metadata={"portal_domain": domain},
        )

    def submit(self, *, job: dict[str, Any], candidate_profile: dict[str, Any], apply_url: str | None) -> SubmissionResult:
        if not apply_url:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="No apply URL was provided.",
                error_type="missing_apply_url",
            )
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment dependency
            return SubmissionResult(
                run_status=ApplicationRunStatuses.FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Playwright is not installed. Run: pip install playwright && playwright install chromium",
                error_type="playwright_not_installed",
                error_message=str(exc),
            )

        resume_profile = job.get("__resume_profile") or {}
        candidate_id = str(job.get("__candidate_id") or candidate_profile.get("candidate_id") or "candidate")
        contact = candidate_contact(candidate_profile, resume_profile)
        missing = [field for field in ("first_name", "last_name", "email") if not contact.get(field)]
        if missing:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Candidate profile is missing required apply fields: " + ", ".join(missing),
                error_type="candidate_required_fields_missing",
                metadata={"missing_fields": missing},
            )

        try:
            resume_path = resolve_resume_file(candidate_id=candidate_id, candidate_profile=candidate_profile, resume_profile=resume_profile)
        except Exception as exc:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Could not prepare a resume file for upload.",
                error_type="resume_file_unavailable",
                error_message=str(exc),
            )

        if resume_path.stat().st_size > int(os.getenv("JOB_MINER_APPLICATION_AGENT_MAX_RESUME_BYTES", "3145728")):
            return SubmissionResult(
                run_status=ApplicationRunStatuses.PRECHECK_FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="Resume file is larger than the portal maximum of 3 MB.",
                error_type="resume_file_too_large",
                metadata={"resume_path": str(resume_path), "resume_size_bytes": resume_path.stat().st_size},
            )

        headless = os.getenv("JOB_MINER_APPLICATION_AGENT_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}
        timeout_ms = int(os.getenv("JOB_MINER_APPLICATION_AGENT_BROWSER_TIMEOUT_MS", "45000"))
        slow_mo = int(os.getenv("JOB_MINER_APPLICATION_AGENT_SLOW_MO_MS", "0"))
        screenshots_dir = _screenshots_dir()
        screenshot_base = screenshots_dir / f"strategicstaff_{candidate_id}_{int(time.time())}"

        browser = None
        final_url = apply_url
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(apply_url, wait_until="domcontentloaded", timeout=timeout_ms)
                _wait_for_network_idle(page, timeout_ms)
                final_url = page.url
                _safe_screenshot(page, screenshot_base.with_name(screenshot_base.name + "_opened.png"))

                body_text = _page_text(page)
                html = page.content()
                if CAPTCHA_PATTERNS.search(body_text) or CAPTCHA_PATTERNS.search(html):
                    return SubmissionResult(
                        run_status=ApplicationRunStatuses.BLOCKED_CAPTCHA,
                        application_status=ApplicationStatuses.AGENT_BLOCKED_CAPTCHA,
                        message="StrategicStaff page displayed CAPTCHA/anti-bot challenge; candidate action is required.",
                        error_type="captcha_detected",
                        metadata={"final_url": final_url},
                    )

                login_result = _login_if_required(page, job.get("__external_account") or {}, contact, timeout_ms)
                if login_result:
                    return login_result

                _scroll_to_apply_form(page)
                _fill_text_field(page, ["First name", "First Name", "first name", "input[name*='first' i]"], contact["first_name"])
                _fill_text_field(page, ["Last name", "Last Name", "last name", "input[name*='last' i]"], contact["last_name"])
                _fill_text_field(page, ["Email address", "Email", "email", "input[type='email']", "input[name*='email' i]"], contact["email"])
                if contact.get("phone"):
                    _try_fill_text_field(page, ["Phone", "Phone number", "input[type='tel']", "input[name*='phone' i]"], contact["phone"])

                file_input = _find_file_input(page)
                if not file_input:
                    _safe_screenshot(page, screenshot_base.with_name(screenshot_base.name + "_missing_file_input.png"))
                    return SubmissionResult(
                        run_status=ApplicationRunStatuses.FAILED,
                        application_status=ApplicationStatuses.AGENT_FAILED,
                        message="Could not find the resume upload field on the StrategicStaff apply form.",
                        error_type="resume_upload_field_missing",
                        metadata={"final_url": page.url, "screenshot": str(screenshot_base.with_name(screenshot_base.name + "_missing_file_input.png"))},
                    )
                file_input.set_input_files(str(resume_path))
                _safe_screenshot(page, screenshot_base.with_name(screenshot_base.name + "_filled.png"))

                submit = _find_submit_button(page)
                if not submit:
                    return SubmissionResult(
                        run_status=ApplicationRunStatuses.FAILED,
                        application_status=ApplicationStatuses.AGENT_FAILED,
                        message="Could not find the submit/apply button on the StrategicStaff form.",
                        error_type="submit_button_missing",
                        metadata={"final_url": page.url},
                    )

                submit.click()
                _wait_for_network_idle(page, timeout_ms)
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                final_url = page.url
                _safe_screenshot(page, screenshot_base.with_name(screenshot_base.name + "_submitted.png"))

                after_text = _page_text(page)
                if SUCCESS_PATTERNS.search(after_text):
                    confirmation_id = _extract_confirmation_id(after_text, final_url)
                    return SubmissionResult(
                        run_status=ApplicationRunStatuses.SUBMITTED,
                        application_status=ApplicationStatuses.AGENT_APPLIED,
                        message="StrategicStaff application was submitted and a confirmation message was detected.",
                        external_confirmation_id=confirmation_id,
                        metadata={
                            "final_url": final_url,
                            "resume_path": str(resume_path),
                            "screenshot": str(screenshot_base.with_name(screenshot_base.name + "_submitted.png")),
                        },
                    )

                if ERROR_PATTERNS.search(after_text):
                    return SubmissionResult(
                        run_status=ApplicationRunStatuses.FAILED,
                        application_status=ApplicationStatuses.AGENT_FAILED,
                        message="StrategicStaff form submission returned validation errors.",
                        error_type="portal_validation_error",
                        error_message=_short_text(after_text),
                        metadata={"final_url": final_url, "screenshot": str(screenshot_base.with_name(screenshot_base.name + "_submitted.png"))},
                    )

                return SubmissionResult(
                    run_status=ApplicationRunStatuses.NEEDS_REVIEW,
                    application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
                    message="The form was submitted/clicked, but no reliable confirmation message was detected. Please review the portal manually.",
                    error_type="confirmation_not_detected",
                    error_message=_short_text(after_text),
                    metadata={"final_url": final_url, "screenshot": str(screenshot_base.with_name(screenshot_base.name + "_submitted.png"))},
                )
        except PlaywrightTimeoutError as exc:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="StrategicStaff browser automation timed out.",
                error_type="browser_timeout",
                error_message=str(exc),
                metadata={"final_url": final_url},
            )
        except PlaywrightError as exc:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="StrategicStaff browser automation failed.",
                error_type="browser_error",
                error_message=str(exc),
                metadata={"final_url": final_url},
            )
        except Exception as exc:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.FAILED,
                application_status=ApplicationStatuses.AGENT_FAILED,
                message="StrategicStaff application strategy failed unexpectedly.",
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata={"final_url": final_url},
            )
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return (parsed.netloc or parsed.path.split("/")[0]).lower().replace("www.", "") or None


def _screenshots_dir() -> Path:
    root = Path(__file__).resolve().parents[3]
    path = root / "data" / "application_agent" / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _wait_for_network_idle(page: Any, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        # Some third-party scripts keep the page busy. The DOM is enough for this form.
        pass


def _page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def _login_if_required(page: Any, external_account: dict[str, Any], contact: dict[str, Any], timeout_ms: int) -> SubmissionResult | None:
    """Handle optional login walls on StrategicStaff pages.

    The current StrategicStaff demo page normally does not require login. The
    earlier strategy called this helper but did not define it, causing a
    NameError before the browser could reach the apply form. This function is
    intentionally conservative: if no visible password field is present, it
    returns None and the application continues. If a login wall is detected and
    candidate-approved credentials are unavailable or fail, it stops with a
    reviewable status instead of guessing credentials or bypassing verification.
    """
    try:
        password_inputs = page.locator("input[type='password']")
        if password_inputs.count() <= 0:
            return None
    except Exception:
        return None

    username = str(
        (external_account or {}).get("username_email")
        or (external_account or {}).get("email")
        or contact.get("email")
        or ""
    ).strip()
    password = str((external_account or {}).get("password_plaintext_dev") or "").strip()
    allow_login = bool((external_account or {}).get("allow_agent_login", True))

    if not allow_login or not username or not password:
        return SubmissionResult(
            run_status=ApplicationRunStatuses.NEEDS_REVIEW,
            application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
            message="StrategicStaff showed a login wall, but candidate-approved portal credentials are missing.",
            error_type="external_login_required",
            metadata={"final_url": getattr(page, "url", None)},
        )

    try:
        _try_fill_text_field(page, ["Email", "Email address", "Username", "input[type='email']", "input[name*='email' i]", "input[name*='user' i]"], username)
        _try_fill_text_field(page, ["Password", "input[type='password']", "input[name*='password' i]"], password)
        login_button = _first_visible_locator(
            page,
            [
                "button[type='submit']",
                "input[type='submit']",
                "text=/sign in/i",
                "text=/log in/i",
                "text=/login/i",
            ],
        )
        if login_button is None:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.NEEDS_REVIEW,
                application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
                message="StrategicStaff showed a login wall, but the login button could not be found.",
                error_type="external_login_button_missing",
                metadata={"final_url": getattr(page, "url", None)},
            )
        login_button.click()
        _wait_for_network_idle(page, timeout_ms)
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass
        if page.locator("input[type='password']").count() > 0:
            return SubmissionResult(
                run_status=ApplicationRunStatuses.NEEDS_REVIEW,
                application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
                message="StrategicStaff login did not complete. Candidate/manual review is required.",
                error_type="external_login_failed",
                metadata={"final_url": getattr(page, "url", None)},
            )
        return None
    except Exception as exc:
        return SubmissionResult(
            run_status=ApplicationRunStatuses.NEEDS_REVIEW,
            application_status=ApplicationStatuses.AGENT_NEEDS_REVIEW,
            message="StrategicStaff login automation failed before reaching the application form.",
            error_type="external_login_failed",
            error_message=str(exc),
            metadata={"final_url": getattr(page, "url", None)},
        )


def _first_visible_locator(page: Any, selectors: list[str]) -> Any | None:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=2000):
                return loc
        except Exception:
            continue
    return None


def _short_text(text: str, limit: int = 1200) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    return compact[:limit]


def _safe_screenshot(page: Any, path: Path) -> None:
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


def _scroll_to_apply_form(page: Any) -> None:
    try:
        page.locator("#apply-now").scroll_into_view_if_needed(timeout=5000)
        return
    except Exception:
        pass
    for selector in ("text=Apply now", "text=Apply for job"):
        try:
            page.locator(selector).first.scroll_into_view_if_needed(timeout=5000)
            break
        except Exception:
            continue


def _fill_text_field(page: Any, selectors: list[str], value: str | None) -> None:
    if not value:
        raise ValueError("Required value missing for field fill.")
    if not _try_fill_text_field(page, selectors, value):
        raise ValueError("Could not find required form field: " + ", ".join(selectors[:3]))


def _try_fill_text_field(page: Any, selectors: list[str], value: str | None) -> bool:
    if not value:
        return False
    for selector in selectors:
        try:
            if selector.startswith("input") or selector.startswith("textarea"):
                loc = page.locator(selector).first
            else:
                loc = page.get_by_label(selector, exact=False).first
            if loc.count() > 0:
                loc.fill(str(value), timeout=5000)
                return True
        except Exception:
            continue
    # Gravity Forms sometimes has labels not associated with inputs. Fall back by proximity/placeholder/name.
    normalized = selectors[0].lower().replace(" ", "")
    fallback_selectors = [
        f"input[placeholder*='{selectors[0]}' i]",
        f"input[aria-label*='{selectors[0]}' i]",
        f"input[name*='{normalized}' i]",
        f"input[id*='{normalized}' i]",
    ]
    for selector in fallback_selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                loc.fill(str(value), timeout=5000)
                return True
        except Exception:
            continue
    return False


def _find_file_input(page: Any) -> Any | None:
    for selector in ("input[type='file'][name*='resume' i]", "input[type='file'][id*='resume' i]", "input[type='file']"):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _find_submit_button(page: Any) -> Any | None:
    selectors = [
        "input[type='submit']",
        "button[type='submit']",
        "text=/submit application/i",
        "text=/submit/i",
        "text=/apply now/i",
        "text=/apply for job/i",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).last if selector.startswith("text=") else page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=3000):
                return loc
        except Exception:
            continue
    return None


def _extract_confirmation_id(text: str, url: str) -> str:
    match = re.search(r"(?:reference|confirmation|application)\s*(?:id|number|#)?\s*[:#]?\s*([A-Z0-9\-]{5,})", text or "", re.I)
    if match:
        return match.group(1)
    return f"strategicstaff:{abs(hash((url, time.time()))) % 10_000_000}"
