import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

CONFIG_PATH = Path("/app/config.json")  # mounted at runtime

# ---------- utils ----------
def load_config() -> Dict:
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
        if await page.locator('input[name="loginfmt"], #i0116').first.is_visible():
            await page.fill('input[name="loginfmt"], #i0116', username)
            await page.locator('#idSIButton9').click()
        else:
            return False  # not AAD

        await page.locator('input[name="passwd"], #i0118').wait_for(state="visible", timeout=timeout_ms)
        await page.fill('input[name="passwd"], #i0118', password)
        await page.locator('#idSIButton9').click()

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

# ---------- open Orders page ----------
async def login_and_open_orders(cfg):
    login_url   = cfg.get("login_url", "https://nucore.northwestern.edu/users/sign_in")
    target_url  = cfg.get("target_url", "https://nucore.northwestern.edu/facilities/nufab/orders")
    username    = cfg.get("username", "")
    password    = cfg.get("password", "")
    timeout_ms  = int(cfg.get("timeout_ms", 30000))
    headless    = bool(cfg.get("headless", True))
    wait_for_duo_seconds = int(cfg.get("wait_for_duo_seconds", 120))
    storage_path = cfg.get("storage_state_path", "/app/storage_state.json")
    use_storage  = bool(cfg.get("use_storage_state", False))
    persist_storage_after_login = bool(cfg.get("persist_storage_state_after_login", True))

    if not username or not password:
        print("ERROR: Missing 'username' or 'password' in config.json", file=sys.stderr)
        raise RuntimeError("Missing credentials")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        storage_state_arg = storage_path if (use_storage and Path(storage_path).exists()) else None
        context = await browser.new_context(
            viewport={"width": 1366, "height": 850},
            storage_state=storage_state_arg
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

        # If not authenticated, go through login
        if "users/sign_in" in page.url or page.url == "about:blank":
            await page.goto(login_url, wait_until="domcontentloaded")
            await try_click_sso_entry(page, timeout_ms)

            aad_done = False
            if "login.microsoftonline.com" in page.url:
                aad_done = await microsoft_aad_login(page, username, password, timeout_ms)

            if not aad_done:
                gen_ok = await generic_username_password_login(page, username, password, timeout_ms)
                if not gen_ok:
                    await dump_debug(page, "login_no_fields")
                    print("WARN: could not find login fields; continuing.", file=sys.stderr)

            await handle_duo_iframe_if_present(page, wait_seconds=wait_for_duo_seconds)

        # Force Orders navigation
        await page.goto(target_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeoutError:
            pass

        # If still not on Orders, try clicking nav
        if "facilities/nufab/orders" not in page.url:
            nav_sel = await first_visible_selector(page, [
                'a:has-text("Orders")',
                'a[href*="/facilities/nufab/orders"]',
                'a[href$="/orders"]'
            ], timeout_ms=3000)
            if nav_sel:
                await page.locator(nav_sel).first.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except PWTimeoutError:
                    pass

        # Always dump the result so you can verify
        await dump_debug(page, "after_orders")

        # Persist storage for next time
        if persist_storage_after_login and storage_path:
            try:
                await context.storage_state(path=storage_path)
            except Exception as e:
                print(f"[warn] could not save storage_state: {e}", file=sys.stderr)

        return browser, context, page

# ---------- parse Orders table ----------
async def fetch_new_orders(cfg) -> List[Dict]:
    browser = context = page = None
    try:
        browser, context, page = await login_and_open_orders(cfg)

        # Identify orders table by headers
        table_count = await page.locator("table").count()
        header_map = {}
        target_idx = None
        wanted_headers = ["order", "order detail", "product", "status"]

        for i in range(table_count):
            ths = await page.locator(f"table:nth-of-type({i+1}) thead tr th").all_inner_texts()
            hdrs = [h.strip().lower() for h in ths]
            if all(any(w in h for h in hdrs) for w in wanted_headers):
                target_idx = i + 1
                header_map = {h.strip().lower(): idx for idx, h in enumerate(ths)}
                break

        if target_idx is None:
            await dump_debug(page, "orders_table_not_found")
            print("ERROR: couldn't find orders table.", file=sys.stderr)
            return []

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
        if None in (idx_order, idx_order_detail, idx_product, idx_status):
            await dump_debug(page, "orders_missing_columns")
            print("ERROR: required columns not found.", file=sys.stderr)
            return []

        rows = []
        trs = page.locator(f"table:nth-of-type({target_idx}) tbody tr")
        n = await trs.count()
        for i in range(n):
            tds = await trs.nth(i).locator("td").all_inner_texts()
            if not tds:
                continue
            status_text = tds[idx_status].strip()
            if status_text.lower().startswith("new"):
                try:
                    order_id = tds[idx_order].strip().split()[0]
                    order_detail_id = tds[idx_order_detail].strip().split()[0]
                except Exception:
                    order_id = tds[idx_order].strip()
                    order_detail_id = tds[idx_order_detail].strip()
                product = tds[idx_product].strip()
                rows.append({
                    "order": order_id,
                    "order_detail": order_detail_id,
                    "product": product,
                    "status": status_text
                })

        # Also dump after parsing (helps debugging)
        await dump_debug(page, "after_orders_parse")
        return rows
    finally:
        try:
            if page: await page.close()
            if context: await context.close()
            if browser: await browser.close()
        except Exception:
            pass
