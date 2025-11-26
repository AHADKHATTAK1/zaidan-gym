"""Capture HTML snapshots and screenshots for core pages.
Saves outputs to ./snapshots/<name>.html and .png
"""
import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import argparse
import requests
import subprocess
import signal

BASE_URL = "http://127.0.0.1:5000"
OUT_DIR = Path("snapshots")
OUT_DIR.mkdir(exist_ok=True)

PAGES = [
    ("login", "/login"),
    ("register", "/register"),
    ("dashboard", "/dashboard"),
    ("members", "/members"),
    ("fees", "/fees"),
    ("admin", "/admin"),
]

def parse_page_entries(entries):
    parsed = []
    for entry in entries:
        if ":" not in entry:
            raise ValueError(f"Invalid page definition '{entry}'. Use name:/path.")
        name, path = entry.split(":", 1)
        parsed.append((name.strip(), path.strip() or "/"))
    return parsed

def parse_args():
    parser = argparse.ArgumentParser(description="Capture HTML snapshots and screenshots.")
    parser.add_argument("--base-url", default=BASE_URL, help="Root URL (default: %(default)s).")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="Output directory (default: %(default)s).")
    parser.add_argument("--timeout", type=int, default=10, help="Wait timeout in seconds (default: %(default)s).")
    parser.add_argument("--viewport", default="1280x720", help="Viewport as WIDTHxHEIGHT (default: %(default)s).")
    parser.add_argument("--headful", action="store_true", help="Run Chrome with a visible window.")
    parser.add_argument("--page", action="append", metavar="name:/path", help="Extra page to capture (repeatable).")
    parser.add_argument("--wait-server", action="store_true", help="Wait until base URL responds before capture.")
    parser.add_argument("--start-flask", action="store_true", help="Launch a Flask app before capturing.")
    parser.add_argument("--flask-app", default="app.py", help="Flask app entry file (default: %(default)s).")
    return parser.parse_args()

def make_driver(headless=True, viewport="1280x720"):
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    try:
        width, height = (int(dim) for dim in viewport.lower().split("x"))
        driver.set_window_size(width, height)
    except (ValueError, AttributeError):
        print(f"Warning: invalid viewport '{viewport}', using Chrome default.")
    return driver

def wait_for_server(url, timeout=30, interval=1):
    """Poll the base URL until it responds or timeout expires."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code < 500:
                print(f"Server ready: {url} ({r.status_code})")
                return True
        except Exception:
            pass
        print(f"Waiting for server at {url} ...")
        time.sleep(interval)
    print(f"Server not ready after {timeout}s: {url}")
    return False

def start_flask(app_path, port):
    """Start Flask app in subprocess; returns Popen or None on failure."""
    if not Path(app_path).exists():
        print(f"Flask app file not found: {app_path}")
        return None
    env = os.environ.copy()
    env["FLASK_APP"] = app_path
    env["FLASK_RUN_PORT"] = str(port)
    proc = subprocess.Popen(
        ["python", "-m", "flask", "run"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    print(f"Started Flask (pid={proc.pid}) on port {port}")
    return proc

def stop_flask(proc):
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

def capture_all(
    base_url=BASE_URL,
    pages=None,
    out_dir=OUT_DIR,
    timeout=10,
    headless=True,
    viewport="1280x720",
):
    if not pages:
        pages = PAGES
    driver = make_driver(headless=headless, viewport=viewport)
    results = []
    try:
        for name, path in pages:
            url = base_url.rstrip("/") + path
            print(f"Opening {url}")
            driver.get(url)
            try:
                WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except Exception:
                print("Warning: body not found quickly")
            html = driver.page_source
            html_file = out_dir / f"{name}.html"
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(html)
            png_file = out_dir / f"{name}.png"
            ok = driver.save_screenshot(str(png_file))
            results.append((name, url, html_file.as_posix(), png_file.as_posix(), ok))
            print(f"Saved: {html_file}  screenshot:{png_file} ok={ok}")
    finally:
        driver.quit()
    return results

if __name__ == '__main__':
    args = parse_args()
    dynamic_pages = list(PAGES)
    if args.page:
        dynamic_pages.extend(parse_page_entries(args.page))
    target_dir = Path(args.out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    flask_proc = None
    if args.start_flask:
        # derive port from base_url if possible
        try:
            port = int(args.base_url.rsplit(":", 1)[-1].split("/")[0])
        except ValueError:
            port = 5000
        flask_proc = start_flask(args.flask_app, port)
    if args.wait_server or args.start_flask:
        wait_for_server(args.base_url.rstrip("/"), timeout=args.timeout * 3)
    res = capture_all(
        base_url=args.base_url,
        pages=dynamic_pages,
        out_dir=target_dir,
        timeout=args.timeout,
        headless=not args.headful,
        viewport=args.viewport,
    )
    print("Done. Files created:")
    for r in res:
        print(r)
    stop_flask(flask_proc)
