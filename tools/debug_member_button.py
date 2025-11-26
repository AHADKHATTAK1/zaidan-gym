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
opts.add_argument('--window-size=1400,900')
# enable browser logging
opts.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
service = Service(ChromeDriverManager().install())

print('Starting browser')
driver = webdriver.Chrome(service=service, options=opts)
try:
    driver.get(f"{BASE}/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, 'username')))
    driver.find_element(By.NAME, 'username').send_keys(USER)
    driver.find_element(By.NAME, 'password').send_keys(PASS)
    driver.find_element(By.CSS_SELECTOR, 'form button[type=submit]').click()
    WebDriverWait(driver, 10).until(EC.url_contains('/dashboard'))

    driver.get(f"{BASE}/members")
    WebDriverWait(driver, 10).until(lambda d: d.execute_script('return document.readyState') == 'complete')
    # wait for members to be populated (cards)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, '#members .card')))
    print('Members loaded')
    # Click the first Details button
    details = driver.find_elements(By.XPATH, "//button[contains(., 'Details')]")
    if not details:
        print('No Details button found')
    else:
        details[0].click()
        # wait for modal
        WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, 'memberDetailsModal')))
        print('Details modal visible')
        shot = os.path.join(OUT_DIR, 'members_details_click.png')
        driver.save_screenshot(shot)
        print('Saved screenshot', shot)
    # print console logs
    try:
        logs = driver.get_log('browser')
        print('Browser console logs:')
        for entry in logs:
            print(entry)
    except Exception as e:
        print('Could not get browser logs:', e)
finally:
    driver.quit()
