import json
import sys
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

CONFIG_PATH = Path("/app/config.json")  # mount this at runtime

# ---------- utils ----------
def load_config():
    if not CONFIG_PATH.exists():
        print("ERROR: /app/config.json not found. Mount it with -v $PWD/config.json:/app/config.json:ro", file=sys.stderr)
        sys.exit(1)
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: failed to read config.json: {e}", file=sys.stderr)
        sys.exit(1)

async def dump_debug(page, label):
    out = Path("/app/out"); out.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(out / f"{label}.png"), full_page=True)
    except Exception as e:
        print(f"[debug] screenshot failed: {e}", file=sys.stderr)
    try:
        (out / f"{label}.url.txt").write_text(page.url, encoding="utf-8")
        (out / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    except Exception as e:
        print(f"[debug] html dump failed: {e}", file=sys.stderr)

async def first_visible_selector(page, candidates, timeout_ms):
    for sel in candidates:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return sel
        except Exception:
            continue
    return None

# ---------- IDP / login helpers ----------
async def try_click_sso_entry(page, timeout_ms):
    SSO_CANDIDATES = [
        'a:has-text("NetID")', 'button:has-text("NetID")',
        'a:has-text("Single Sign-On")', 'button:has-text("Single Sign-On")',
        'a:has-text("Northwestern")', 'button:has-text("Northwestern")',
        'a[href*="sso"]', 'a[href*="shibb"]', 'a[href*="weblogin"]', 'a[href*="login"]'
    ]
    sel = await first_visible_selector(page, SSO_CANDIDATES, timeout_ms=2000)
    if sel:
        await page.locator(sel).first.click()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except PWTimeoutError:
            pass
        return True
    return False

async def microsoft_aad_login(page, username, password, timeout_ms):
    try:
        # Email/username
        if await page.locator('input[name="loginfmt"], #i0116').first.is_visible():
            await page.fill('input[name="loginfmt"], #i0116', username)
            await page.locator('#idSIButton9').click()
        else:
            return False  # not AAD

        # Password
        await page.locator('input[name="passwd"], #i0118').wait_for(state="visible", timeout=timeout_ms)
        await page.fill('input[name="passwd"], #i0118', password)
        await page.locator('#idSIButton9').click()

        # Stay signed in?
        try:
            await page.locator('#idBtn_Back, #idSIButton9').first.wait_for(timeout=8000)
            if await page.locator('#idBtn_Back').is_visible():
                await page.locator('#idBtn_Back').click()  # "No"
            else:
                await page.locator('#idSIButton9').click()  # "Yes"
        except Exception:
            pass

        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeoutError:
            pass
        return True
    except Exception as e:
        print(f"[warn] AAD flow error: {e}", file=sys.stderr)
        return False

async def generic_username_password_login(page, username, password, timeout_ms):
    USER_SELECTORS = [
        'input[name="user[email]"]', '#user_email',
        'input#username', 'input[name="username"]', 'input[name="j_username"]',
        'input[name*="netid" i]', 'input[name*="email" i]',
        'input[name*="user" i][type="text"]',
        'input[type="email"]', 'input[type="text"]'
    ]
    PASS_SELECTORS = [
        'input[name="user[password]"]', '#user_password',
        'input#password', 'input[name="password"]', 'input[name="j_password"]',
        'input[type="password"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Sign in")', 'button:has-text("Log in")', 'button:has-text("Login")',
        'input[name="commit"]'
    ]
    user_sel = await first_visible_selector(page, USER_SELECTORS, timeout_ms=4000)
    pass_sel = await first_visible_selector(page, PASS_SELECTORS, timeout_ms=4000)
    if not user_sel or not pass_sel:
        return False
    await page.fill(user_sel, username)
    await page.fill(pass_sel, password)
    submit_sel = await first_visible_selector(page, SUBMIT_SELECTORS, timeout_ms=2500)
    if submit_sel:
        await page.locator(submit_sel).first.click()
    else:
        await page.press(pass_sel, "Enter")
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeoutError:
        pass
    return True

async def handle_duo_iframe_if_present(page, wait_seconds):
    try:
        await page.frame_locator("iframe#duo_iframe, iframe[name='duo_iframe']").first.wait_for(timeout=5000)
    except Exception:
        return  # no Duo
    print(f"Detected Duo iframe — waiting up to {wait_seconds}s for approval…", file=sys.stderr)
    # Best effort: try clicking a push button
    try:
        f = next((fr for fr in page.frames if (fr.name and 'duo' in fr.name.lower()) or ('duo' in (fr.url or '').lower())), None)
        if f:
            for s in ['button:has-text("Send"), button:has-text("Push")',
                      'button:has-text("Send Me a Push")',
                      'button:has-text("Continue")',
                      'button:has-text("Other options")']:
                try:
                    if await f.locator(s).first.is_visible():
                        await f.locator(s).first.click()
                        break
                except Exception:
                    continue
    except Exception:
        pass
    try:
        await page.wait_for_function(
            "() => !document.querySelector('iframe#duo_iframe, iframe[name=duo_iframe]')",
            timeout=wait_seconds * 1000
        )
    except PWTimeoutError:
        pass

# ---------- main ----------
async def run():
    cfg = load_config()
    login_url   = cfg.get("login_url", "https://nucore.northwestern.edu/users/sign_in")
    target_url  = cfg.get("target_url", "https://nucore.northwestern.edu/facilities/nufab/orders")
    username    = cfg.get("username", "")
    password    = cfg.get("password", "")
    print_mode  = (cfg.get("print_mode", "text") or "text").lower()
    save_shot   = bool(cfg.get("save_screenshot", True))
    timeout_ms  = int(cfg.get("timeout_ms", 30000))
    headless    = bool(cfg.get("headless", True))
    debug_dump  = bool(cfg.get("debug_dump", True))
    login_success_text = cfg.get("login_success_text", "Signed in successfully.")
    storage_path = cfg.get("storage_state_path", "/app/storage_state.json")
    use_storage  = bool(cfg.get("use_storage_state", False))
    persist_storage_after_login = bool(cfg.get("persist_storage_state_after_login", True))
    wait_for_duo_seconds = int(cfg.get("wait_for_duo_seconds", 120))
    download_csv = bool(cfg.get("download_csv", True))
    csv_out_path = cfg.get("csv_out_path", "/app/out/orders.csv")

    if not username or not password:
        print("ERROR: Missing 'username' or 'password' in config.json", file=sys.stderr)
        sys.exit(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        storage_state_arg = storage_path if (use_storage and Path(storage_path).exists()) else None
        context = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            storage_state=storage_state_arg,
            accept_downloads=True
        )
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        # If cookies exist, try target directly
        if storage_state_arg:
            await page.goto(target_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeoutError:
                pass

        # If not already at orders (and not authenticated), go through login
        if "users/sign_in" in page.url or page.url == "about:blank":
            # Start at sign-in
            await page.goto(login_url, wait_until="domcontentloaded")

            # Click SSO/NetID if present
            await try_click_sso_entry(page, timeout_ms)

            # Microsoft AAD?
            aad_done = False
            if "login.microsoftonline.com" in page.url:
                aad_done = await microsoft_aad_login(page, username, password, timeout_ms)

            # Else generic
            if not aad_done:
                gen_ok = await generic_username_password_login(page, username, password, timeout_ms)
                if not gen_ok and debug_dump:
                    await dump_debug(page, "login_no_fields")

            # Duo (if present)
            await handle_duo_iframe_if_present(page, wait_seconds=wait_for_duo_seconds)

        # Go to orders page
        try:
            await page.goto(target_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeoutError:
                pass
        except PWTimeoutError:
            print("Warning: navigation to orders timed out.", file=sys.stderr)

        # Verify we're on orders (look for export link or heading)
        ORDERS_SIGNALS = [
            'a.js--exportSearchResults',
            'a:has-text("Export as CSV")',
            'a[href$="/facilities/nufab/orders.csv"]',
            'h1:has-text("Orders")', 'h2:has-text("Orders")'
        ]
        found_orders_ui = await first_visible_selector(page, ORDERS_SIGNALS, timeout_ms=4000)

        if not found_orders_ui:
            if debug_dump:
                await dump_debug(page, "orders_page_check_failed")
            print("WARNING: Orders UI not detected — you may not be signed in. See /app/out/orders_page_check_failed.*", file=sys.stderr)

        # Optionally download the CSV
        if download_csv:
            try:
                csv_sel = await first_visible_selector(page, [
                    'a.js--exportSearchResults',
                    'a:has-text("Export as CSV")',
                    'a[href$="/facilities/nufab/orders.csv"]'
                ], timeout_ms=3000)
                if csv_sel:
                    outdir = Path("/app/out"); outdir.mkdir(parents=True, exist_ok=True)
                    # Playwright download flow
                    try:
                        async with page.expect_download() as dlinfo:
                            await page.locator(csv_sel).first.click()
                        download = await dlinfo.value
                        await download.save_as(csv_out_path)
                        print(f"[ok] Saved CSV to {csv_out_path}", file=sys.stderr)
                    except Exception as e:
                        print(f"[warn] CSV download failed: {e}", file=sys.stderr)
                else:
                    print("[warn] CSV export link not found.", file=sys.stderr)
            except Exception as e:
                print(f"[warn] CSV step error: {e}", file=sys.stderr)

        # Persist cookies for next time
        if persist_storage_after_login and storage_path:
            try:
                await context.storage_state(path=storage_path)
            except Exception as e:
                print(f"[warn] could not save storage_state: {e}", file=sys.stderr)

        # Dump final state for debugging if wanted
        if save_shot:
            await dump_debug(page, "after_orders")

        # Print page output
        try:
            if print_mode == "html":
                print(await page.content())
            else:
                print(await page.evaluate("() => document.documentElement.innerText"))
        except Exception as e:
            if debug_dump:
                await dump_debug(page, "final_print_error")
            print(f"[warn] printing failed: {e}", file=sys.stderr)

        await context.close()
        await browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
