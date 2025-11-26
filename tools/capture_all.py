"""Headless snapshot script: logs in then saves HTML and screenshots for core pages.

Usage:
  python tools/capture_all.py

Outputs written to `snapshots/`.
"""
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import os

BASE = os.getenv('BASE_URL', 'http://127.0.0.1:5000')
USER = os.getenv('ADMIN_USERNAME', 'admin')
PASS = os.getenv('ADMIN_PASSWORD', 'admin123')
OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'snapshots')
os.makedirs(OUT_DIR, exist_ok=True)

PAGES = [
    ('/login', 'login'),
    ('/register', 'register'),
    ('/dashboard', 'dashboard'),
    ('/members', 'members'),
    ('/fees', 'fees'),
    ('/admin', 'admin'),
]

opts = webdriver.ChromeOptions()
opts.add_argument('--headless=new')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_argument('--window-size=1400,900')
service = Service(ChromeDriverManager().install())

driver = webdriver.Chrome(service=service, options=opts)
try:
    # Login first via form
    driver.get(f"{BASE}/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, 'username')))
    driver.find_element(By.NAME, 'username').send_keys(USER)
    driver.find_element(By.NAME, 'password').send_keys(PASS)
    driver.find_element(By.CSS_SELECTOR, 'form button[type=submit]').click()
    WebDriverWait(driver, 10).until(EC.url_contains('/dashboard'))

    for path, name in PAGES:
        url = f"{BASE}{path}"
        print('Visiting', url)
        driver.get(url)
        WebDriverWait(driver, 10).until(lambda d: d.execute_script('return document.readyState') == 'complete')
        html = driver.page_source
        html_path = os.path.join(OUT_DIR, f"{name}.html")
        png_path = os.path.join(OUT_DIR, f"{name}.png")
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)
        driver.save_screenshot(png_path)
        print('Saved', html_path, png_path)

    print('All snapshots saved in', OUT_DIR)
finally:
    driver.quit()
