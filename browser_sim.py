import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


def env_or(argval: str | None, *envkeys: str, default: str | None = None) -> str | None:
    """Return the first non-empty of [argval, env[envkeys...], default]."""
    if argval:
        return argval
    for k in envkeys:
        v = os.getenv(k)
        if v:
            return v
    return default


async def run():
    parser = argparse.ArgumentParser(
        description="Headless browser simulator that (optionally) logs in, then prints page output."
    )
    parser.add_argument("--url", required=True, help="Target URL to visit after (optional) login.")
    # Optional form-based login
    parser.add_argument("--login-url", help="Login page URL (defaults to --url if not provided).")
    parser.add_argument("--user", help="Username (or set via env USER or LOGIN_USER).")
    parser.add_argument("--password", help="Password (or set via env PASSWORD or LOGIN_PASS).")
    parser.add_argument("--user-selector", default='input[name="username"], input[type="email"], input[name="email"]',
                        help="CSS selector for username/email field.")
    parser.add_argument("--pass-selector", default='input[type="password"], input[name="password"]',
                        help="CSS selector for password field.")
    parser.add_argument("--submit-selector", default='button[type="submit"], input[type="submit"]',
                        help="CSS selector for submit button.")
    parser.add_argument("--wait-selector", default="body",
                        help="CSS selector to wait for before printing output (post-login or at target).")
    # Optional HTTP Basic auth
    parser.add_argument("--http-auth-user", help="HTTP Basic auth username (or HTTP_AUTH_USER env).")
    parser.add_argument("--http-auth-pass", help="HTTP Basic auth password (or HTTP_AUTH_PASS env).")
    # Output controls
    parser.add_argument("--print", dest="print_mode", choices=["html", "text"], default="text",
                        help="Print full HTML or textContent of the page.")
    parser.add_argument("--timeout", type=int, default=15000,
                        help="Default timeout in ms for waits/navigation (default: 15000).")
    parser.add_argument("--no-headless", action="store_true", help="Run with visible browser window.")
    parser.add_argument("--screenshot", default=None, help="Optional path to save a PNG screenshot.")
    parser.add_argument("--user-agent", default=None, help="Override User-Agent.")
    parser.add_argument("--viewport", default="1280x800", help='Viewport, e.g., "1366x768".')
    parser.add_argument("--slowmo", type=int, default=0, help="Slow down actions (ms). Helpful for debugging.")
    args = parser.parse_args()

    # Resolve credentials (support env vars so you don't type passwords into your shell history)
    username = env_or(args.user, "USER", "LOGIN_USER")
    password = env_or(args.password, "PASSWORD", "LOGIN_PASS")
    http_user = env_or(args.http_auth_user, "HTTP_AUTH_USER")
    http_pass = env_or(args.http_auth_pass, "HTTP_AUTH_PASS")

    width, height = (int(v) for v in args.viewport.lower().split("x"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.no_headless, slow_mo=args.slowmo)
        context_kwargs = {"viewport": {"width": width, "height": height}}
        if args.user_agent:
            context_kwargs["user_agent"] = args.user_agent
        if http_user and http_pass:
            context_kwargs["http_credentials"] = {"username": http_user, "password": http_pass}

        context = await browser.new_context(**context_kwargs)

        # Collect console messages (useful for debugging)
        console_msgs = []

        async def on_console(msg):
            console_msgs.append(f"[{msg.type()}] {msg.text()}")

        page = await context.new_page()
        page.set_default_timeout(args.timeout)
        page.on("console", on_console)

        # If login URL + creds provided, attempt form-based login
        if (args.login_url or username or password):
            login_url = args.login_url if args.login_url else args.url
            try:
                resp = await page.goto(login_url, wait_until="domcontentloaded")
                status = resp.status if resp else "n/a"
                # Try to fill only if we actually have both fields and creds
                if username and password:
                    # Wait for fields if present
                    await page.wait_for_selector(args.user_selector)
                    await page.fill(args.user_selector, username)

                    await page.wait_for_selector(args.pass_selector)
                    await page.fill(args.pass_selector, password)

                    # Click submit
                    # Some pages have multiple matches; click the first visible
                    btn = await page.query_selector(args.submit_selector)
                    if btn:
                        await btn.click()
                    else:
                        # Try pressing Enter in password field as fallback
                        await page.press(args.pass_selector, "Enter")

                    # Let navigation/redirects settle
                    try:
                        await page.wait_for_load_state("networkidle", timeout=args.timeout)
                    except PWTimeoutError:
                        pass  # Some apps never fully go idle
                else:
                    # No creds: just proceed to target after loading login page
                    pass
            except PWTimeoutError:
                print("Login step timed out.", file=sys.stderr)

        # Navigate to the target URL (if we logged in on another page)
        if args.login_url and args.url != args.login_url:
            try:
                await page.goto(args.url, wait_until="domcontentloaded")
            except PWTimeoutError:
                pass

        # Wait for a meaningful selector (default: body)
        try:
            await page.wait_for_selector(args.wait_selector)
        except PWTimeoutError:
            print(f"Warning: wait for selector '{args.wait_selector}' timed out.", file=sys.stderr)

        # Optional screenshot
        if args.screenshot:
            out = Path(args.screenshot)
            out.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=True)

        # Print output
        if args.print_mode == "html":
            html = await page.content()
            print(html)
        else:
            # Prefer the documentElement's innerText for a readable dump
            try:
                text = await page.evaluate("() => document.documentElement.innerText")
            except Exception:
                # Fallback: body textContent
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            print(text)

        # Also print some metadata & console logs to stderr (kept separate from main output)
        try:
            print(f"\n---\nURL: {page.url}", file=sys.stderr)
            print(f"Title: {await page.title()}", file=sys.stderr)
        except Exception:
            pass
        if console_msgs:
            print("\nConsole messages:", file=sys.stderr)
            for m in console_msgs:
                print(m, file=sys.stderr)

        await context.close()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
