from __future__ import annotations


def build_accept_all_cookies_js() -> str:
    return r"""
    (() => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' &&
               style.visibility !== 'hidden' &&
               style.opacity !== '0' &&
               rect.width > 0 &&
               rect.height > 0;
      };

      const selectors = [
        'button',
        'a',
        '[role="button"]',
        'input[type="button"]',
        'input[type="submit"]',
        'div',
        'span'
      ];

      const acceptPatterns = [
        'accept all',
        'accept cookies',
        'accept cookie',
        'allow all',
        'allow cookies',
        'agree',
        'i agree',
        'yes, i agree',
        'got it',
        'ok'
      ];

      const nodes = Array.from(document.querySelectorAll(selectors.join(',')))
        .filter(isVisible);

      for (const pattern of acceptPatterns) {
        const node = nodes.find((el) => {
          const text = norm(
            el.innerText ||
            el.textContent ||
            el.value ||
            el.getAttribute('aria-label') ||
            el.getAttribute('title')
          );

          return text === pattern || text.includes(pattern);
        });

        if (node) {
          node.scrollIntoView({ behavior: 'instant', block: 'center' });
          node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));

          if (typeof node.click === 'function') {
            node.click();
          }

          return `accepted cookies using pattern: ${pattern}`;
        }
      }

      return 'no accept-all cookie button found';
    })();
    """