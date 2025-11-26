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

opts = webdriver.ChromeOptions()
opts.add_argument('--headless=new')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
service = Service(ChromeDriverManager().install())

driver = webdriver.Chrome(service=service, options=opts)
try:
    driver.get(f"{BASE}/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, 'username')))
    driver.find_element(By.NAME, 'username').send_keys(USER)
    driver.find_element(By.NAME, 'password').send_keys(PASS)
    driver.find_element(By.CSS_SELECTOR, 'form button[type=submit]').click()
    WebDriverWait(driver, 10).until(EC.url_contains('/dashboard'))
    driver.get(f"{BASE}/members")
    # Wait for page load
    WebDriverWait(driver, 10).until(lambda d: d.execute_script('return document.readyState') == 'complete')
    html = driver.page_source
    with open(os.path.join(OUT_DIR, 'members.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    driver.save_screenshot(os.path.join(OUT_DIR, 'members.png'))
    print('Saved snapshots to', OUT_DIR)
finally:
    driver.quit()
