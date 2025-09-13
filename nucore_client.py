import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

CONFIG_PATH = Path("/app/config.json")  # mounted at runtime

# ---------- config ----------
def load_config() -> Dict:
    if not CONFIG_PATH.exists():
        print("ERROR: /app/config.json not found. Mount it: -v $PWD/config.json:/app/config.json:ro", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: failed to read config.json: {e}", file=sys.stderr)
        sys.exit(1)

# ---------- helpers ----------
async def dump_debug(page, label):
    out = Path("/app/out"); out.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(out / f"{label}.png"), full_page=True)
    except Exception: pass
    try:
        (out / f"{label}.url.txt").write_text(page.url, encoding="utf-8")
        (out / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    except Exception: pass

async def first_visible_selector(page, candidates, timeout_ms):
    for sel in candidates:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return sel
        except Exception:
            continue
    return None

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
        # username/email
        if await page.locator('input[name="loginfmt"], #i0116').first.is_visible():
            await page.fill('input[name="loginfmt"], #i0116', username)
            await page.locator('#idSIButton9').click()
        else:
            return False
        # password
        await page.locator('input[name="passwd"], #i0118').wait_for(state="visible", timeout=timeout_ms)
        await page.fill('input[name="passwd"], #i0118', password)
        await page.locator('#idSIButton9').click()
        # stay signed in?
        try:
            await page.locator('#idBtn_Back, #idSIButton9').first.wait_for(timeout=8000)
            if await page.locator('#idBtn_Back').is_visible():
                await page.locator('#idBtn_Back').click()
            else:
                await page.locator('#idSIButton9').click()
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeoutError:
            pass
        return True
    except Exception:
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
        return
    # try to click push inside the frame (best-effort)
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
    # wait for duo to go away or proceed
    try:
        await page.wait_for_function(
            "() => !document.querySelector('iframe#duo_iframe, iframe[name=duo_iframe]')",
            timeout=wait_seconds * 1000
        )
    except PWTimeoutError:
        pass

# ---------- scrape ----------
async def fetch_new_orders(cfg) -> List[Dict]:
    login_url   = cfg.get("login_url", "https://nucore.northwestern.edu/users/sign_in")
    target_url  = cfg.get("target_url", "https://nucore.northwestern.edu/facilities/nufab/orders")
    username    = cfg.get("username", "")
    password    = cfg.get("password", "")
    timeout_ms  = int(cfg.get("timeout_ms", 30000))
    headless    = bool(cfg.get("headless", True))
    wait_for_duo_seconds = int(cfg.get("wait_for_duo_seconds", 120))

    if not username or not password:
        print("ERROR: missing username/password in config.json", file=sys.stderr)
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1366, "height": 850})
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        # Start login
        await page.goto(login_url, wait_until="domcontentloaded")
        await try_click_sso_entry(page, timeout_ms)

        # Microsoft AAD?
        if "login.microsoftonline.com" in page.url:
            await microsoft_aad_login(page, username, password, timeout_ms)
        else:
            gen_ok = await generic_username_password_login(page, username, password, timeout_ms)
            if not gen_ok:
                await dump_debug(page, "login_no_fields")
                print("WARN: could not find login fields; proceeding anyway.", file=sys.stderr)

        await handle_duo_iframe_if_present(page, wait_seconds=wait_for_duo_seconds)

        # Go to orders page
        await page.goto(target_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeoutError:
            pass

        # Find the first table whose headers include required names
        table_count = await page.locator("table").count()
        target_table_idx = None
        wanted_headers = ["order", "order detail", "product", "status"]

        for i in range(table_count):
            ths = await page.locator(f"table:nth-of-type({i+1}) thead tr th").all_inner_texts()
            hdrs = [h.strip().lower() for h in ths]
            if all(any(w in h for h in hdrs) for w in ["order", "product"]):
                # more strict: must have order detail & status too
                if all(any(w in h for h in hdrs) for w in wanted_headers):
                    target_table_idx = i + 1
                    header_map = {h.strip().lower(): idx for idx, h in enumerate(ths)}
                    break

        if target_table_idx is None:
            await dump_debug(page, "orders_table_not_found")
            print("ERROR: couldn't find orders table.", file=sys.stderr)
            await context.close(); await browser.close()
            return []

        # helper to get column index by name (allow partial match)
        def col_idx(name: str) -> Optional[int]:
            lname = name.lower()
            for k, idx in header_map.items():
                if lname in k:
                    return idx
            return None

        idx_order = col_idx("order")
        idx_order_detail = col_idx("order detail")
        idx_product = col_idx("product")
        idx_status = col_idx("status")

        rows = []
        trs = page.locator(f"table:nth-of-type({target_table_idx}) tbody tr")
        n = await trs.count()
        for i in range(n):
            tds = await trs.nth(i).locator("td").all_inner_texts()
            if not tds or idx_status is None or idx_order is None or idx_order_detail is None or idx_product is None:
                continue
            status_text = tds[idx_status].strip()
            if status_text.lower().startswith("new"):  # filter "New"
                try:
                    order_id = tds[idx_order].strip().split()[0]
                    order_detail_id = tds[idx_order_detail].strip().split()[0]
                except Exception:
                    # fallback: use raw text
                    order_id = tds[idx_order].strip()
                    order_detail_id = tds[idx_order_detail].strip()
                product = tds[idx_product].strip()
                rows.append({
                    "order": order_id,
                    "order_detail": order_detail_id,
                    "product": product,
                    "status": status_text
                })

        await context.close()
        await browser.close()
        return rows
