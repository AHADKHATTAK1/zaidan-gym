"""Simple Selenium script to exercise local upload and members page.
Prerequisites:
  - Run the Flask app locally (FLASK_APP=app.py flask run)
  - Install selenium and webdriver-manager: pip install selenium webdriver-manager
Usage:
  python selenium_upload_test.py path/to/sample.csv
"""
import sys, time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "http://127.0.0.1:5000"
USERNAME = "admin"
PASSWORD = "admin123"

def login(driver):
    driver.get(f"{BASE_URL}/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "username")))
    driver.find_element(By.NAME, "username").send_keys(USERNAME)
    driver.find_element(By.NAME, "password").send_keys(PASSWORD)
    driver.find_element(By.CSS_SELECTOR, "form button[type=submit]").click()
    WebDriverWait(driver, 10).until(EC.url_contains("/dashboard"))

def go_members(driver):
    driver.get(f"{BASE_URL}/members")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "members")))

def upload_file(driver, file_path: Path):
    go_members(driver)
    # Scroll to upload section
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    file_input = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "dataFile")))
    file_input.send_keys(str(file_path.resolve()))
    driver.find_element(By.CSS_SELECTOR, "#dataUploadForm button[type=submit]").click()
    # Wait for status
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "uploadStatus")))
    status_html = driver.find_element(By.ID, "uploadStatus").get_attribute("innerHTML")
    print("Status:", status_html)


def main():
    if len(sys.argv) < 2:
        print("Usage: python selenium_upload_test.py <file.csv|file.xlsx>")
        sys.exit(1)
    fp = Path(sys.argv[1])
    if not fp.exists():
        print("File not found:", fp)
        sys.exit(1)
    opts = webdriver.ChromeOptions()
    # Use headless for CI but add common flags to improve stability on Windows
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--remote-allow-origins=*")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    try:
        login(driver)
        upload_file(driver, fp)
        # Attempt duplicate upload to see rejection
        upload_file(driver, fp)
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
