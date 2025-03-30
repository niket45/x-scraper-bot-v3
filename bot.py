import re
import json
import time
import random
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from telegram import Update, ChatMember
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes
from tabulate import tabulate  
from telegram.constants import ChatMemberStatus

# Configuration settings for the scraper
CONFIG = {
    "headless": True,
    "base_timeout_min": 3,
    "base_timeout_max": 6,
    "page_load_timeout": 90,
    "max_retries": 3,
    "scroll_delay": 6,
    "max_scroll_attempts": 30,
    "max_expand_attempts": 20,
    "max_click_retries": 3,
    "rate_limit_delay": 60
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0"
]

global_data = {
    "comments": [],
    "verify": {},
    "track_usernames": [],
    "summary": "",
    "step": None,
    "track_step": 1
}
ignored_users = set()
driver = None

def random_delay(min_secs, max_secs):
    time.sleep(random.uniform(min_secs, max_secs))

def load_cookies(driver, file_path):
    print(f"Loading cookies from {file_path}...")
    try:
        with open(file_path, 'r') as file:
            cookies = json.load(file)
        if not isinstance(cookies, list):
            raise ValueError("Cookies file must contain a list of cookies")
        current_time = int(time.time())
        valid_cookies = [cookie for cookie in cookies if cookie.get('expires', -1) == -1 or cookie.get('expires', -1) > current_time]
        for cookie in valid_cookies:
            if 'domain' not in cookie:
                cookie['domain'] = '.x.com'
            driver.add_cookie(cookie)
        print(f"Loaded and applied {len(valid_cookies)} valid cookies.")
        return True
    except Exception as e:
        print(f"Failed to load cookies: {e}")
        return False

def save_cookies(driver, file_path):
    try:
        cookies = driver.get_cookies()
        with open(file_path, 'w') as file:
            json.dump(cookies, file, indent=2)
        print(f"Saved {len(cookies)} cookies to {file_path}")
    except Exception as e:
        print(f"Failed to save cookies: {e}")

def simulate_human_activity(driver):
    print("Simulating human-like activity...")
    try:
        actions = ActionChains(driver)
        actions.move_by_offset(random.randint(10, 100), random.randint(10, 100)).perform()
        random_delay(0.5, 1.5)
        actions.move_by_offset(random.randint(-50, 50), random.randint(-50, 50)).click().perform()
        random_delay(1, 3)
    except Exception as e:
        print(f"Error simulating human activity: {e}")

def expand_replies(driver, target_url):
    print("Expanding replies...")
    expanded = False
    texts_to_match = ["Show more replies", "Load more", "Show", "more replies", "Show more", "View more replies"]
    attempts = 0
    previous_reply_count = 0

    while attempts < CONFIG["max_expand_attempts"]:
        if target_url not in driver.current_url:
            print("Page URL changed unexpectedly. Re-navigating to target URL...")
            driver.get(target_url)
            WebDriverWait(driver, CONFIG["page_load_timeout"]).until(
                EC.presence_of_element_located((By.XPATH, '//article'))
            )
            print("Main post reloaded.")

        show_more_button = None
        for text in texts_to_match:
            try:
                show_more_button = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, f"//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"))
                )
                if show_more_button:
                    break
            except:
                continue
        
        if not show_more_button:
            try:
                show_more_button = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, '//div[@role="button" and contains(@class, "css-") and contains(., "more")]'))
                )
            except:
                pass

        if not show_more_button:
            print("No 'Show more' buttons found after trying all selectors.")
            break

        print(f'Clicking "Show more" button (Attempt {attempts + 1}/{CONFIG["max_expand_attempts"]})...')
        click_success = False
        for click_attempt in range(CONFIG["max_click_retries"]):
            try:
                driver.execute_script("arguments[0].scrollIntoView(true);", show_more_button)
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable(show_more_button))
                actions = ActionChains(driver)
                actions.move_to_element(show_more_button).click().perform()
                random_delay(CONFIG["base_timeout_min"] + 3, CONFIG["base_timeout_max"] + 3)
                click_success = True
                expanded = True
                break
            except Exception as e:
                print(f"Error clicking 'Show more' button (Click attempt {click_attempt + 1}/{CONFIG['max_click_retries']}): {e}")
                random_delay(2, 4)
        
        if not click_success:
            print("Failed to click 'Show more' button after retries. Stopping expansion.")
            break

        current_reply_count = len(driver.find_elements(By.XPATH, '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"])]'))
        if current_reply_count == previous_reply_count:
            print("No new replies loaded after clicking 'Show more'. Stopping expansion.")
            break
        previous_reply_count = current_reply_count
        attempts += 1
    
    print("Finished expanding replies." if expanded else "No 'Show more' buttons found after all attempts.")

def scrape_usernames_from_viewport(driver, usernames_set):
    print("Scraping usernames from current viewport...")
    try:
        selectors = [
            '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//div[@data-testid="User-Name"]//span[contains(text(), "@")]',
            '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//a[@role="link" and contains(@href, "/") and contains(text(), "@")]',
            '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//span[contains(text(), "@")]',
            '//div[@data-testid="Tweet-User-Avatar" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//following-sibling::div//span[contains(text(), "@")]',
            '//div[@data-testid="Tweet-User-Avatar" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//following-sibling::div//a[contains(@href, "/") and contains(text(), "@")]',
            '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//div[contains(@class, "css-") and contains(@class, "user-name")]//span[contains(text(), "@")]',
            '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"]) and not(ancestor::*[contains(., "Discover more")])]//a[@role="link" and contains(@href, "/") and contains(text(), "@")]'
        ]

        for selector in selectors:
            try:
                username_elements = driver.find_elements(By.XPATH, selector)
                for element in username_elements:
                    text = element.text.strip()
                    if text.startswith('@') and text != '@softyyy_tweets':
                        usernames_set.add(text)
            except:
                continue
        
        print(f"Total unique usernames found so far: {len(usernames_set)}")
    except Exception as e:
        print(f"Error scraping usernames from viewport: {e}")

def scroll_and_load_replies(driver):
    print("Scrolling to load more replies and scraping usernames incrementally...")
    usernames_set = set()
    scroll_attempts = 0
    last_height = driver.execute_script("return document.body.scrollHeight")
    previous_reply_count = 0

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"])]'))
        )
        print("Initial replies loaded.")
    except TimeoutException:
        print("No initial replies found after waiting. Proceeding with scrolling.")

    while scroll_attempts < CONFIG["max_scroll_attempts"]:
        scrape_usernames_from_viewport(driver, usernames_set)
        driver.execute_script("window.scrollBy(0, 500);")
        random_delay(CONFIG["scroll_delay"], CONFIG["scroll_delay"] + 2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        current_reply_count = len(driver.find_elements(By.XPATH, '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"])]'))

        if new_height == last_height and current_reply_count == previous_reply_count:
            print("No more content to load.")
            break
        
        last_height = new_height
        previous_reply_count = current_reply_count
        scroll_attempts += 1
        print(f"Scroll attempt {scroll_attempts}/{CONFIG['max_scroll_attempts']}, found {current_reply_count} replies in viewport.")
        try:
            WebDriverWait(driver, 10).until(
                lambda d: len(d.find_elements(By.XPATH, '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"])]')) > previous_reply_count
            )
        except:
            pass

    print("Scrolling to top for final scrape...")
    driver.execute_script("window.scrollTo(0, 0);")
    random_delay(5, 7)
    print("Performing final scrape of usernames...")
    scrape_usernames_from_viewport(driver, usernames_set)
    
    return list(usernames_set)

def scrape_x_usernames(url, cookies_file, driver_instance):
    rate_limited = False
    try:
        print("Navigating to https://x.com...")
        driver_instance.get("https://x.com")
        random_delay(3, 5)

        if not load_cookies(driver_instance, cookies_file):
            print("Failed to load cookies.")
            return []
        
        print("Refreshing page to apply cookies...")
        driver_instance.refresh()
        random_delay(3, 5)

        try:
            WebDriverWait(driver_instance, 10).until(
                EC.presence_of_element_located((By.XPATH, '//a[contains(@href, "/home")]'))
            )
            print("Authentication successful.")
        except:
            try:
                driver_instance.find_element(By.XPATH, '//a[contains(text(), "Sign in")]')
                print("Authentication failed: Cookies invalid or expired.")
                return []
            except:
                print("Authentication status unclear. Proceeding.")

        save_cookies(driver_instance, cookies_file)
        simulate_human_activity(driver_instance)

        attempt = 0
        navigation_success = False
        while attempt <= CONFIG["max_retries"] and not navigation_success:
            try:
                print(f"Navigating to {url} (Attempt {attempt + 1}/{CONFIG['max_retries'] + 1})...")
                driver_instance.get(url)
                WebDriverWait(driver_instance, CONFIG["page_load_timeout"]).until(
                    EC.presence_of_element_located((By.XPATH, '//article'))
                )
                random_delay(5, 10)
                WebDriverWait(driver_instance, 20).until(
                    EC.presence_of_element_located((By.XPATH, '//article[@data-testid="tweet"]'))
                )
                print("Main post and initial content loaded.")
                navigation_success = True
            except TimeoutException:
                attempt += 1
                print("Timed out waiting for page to load. Retrying...")
                if attempt > CONFIG["max_retries"]:
                    print("Max retries reached.")
                    return []
                random_delay(5 * (2 ** attempt), 10)

        try:
            captcha = driver_instance.find_element(By.XPATH, '//iframe[contains(@src, "captcha")]')
            print("CAPTCHA detected. Cannot proceed automatically.")
            return []
        except:
            pass

        try:
            driver_instance.find_element(By.XPATH, '//div[contains(text(), "Too Many Requests")]')
            print("Rate limiting detected.")
            random_delay(CONFIG["rate_limit_delay"], CONFIG["rate_limit_delay"] + 5)
            rate_limited = True
        except:
            pass

        simulate_human_activity(driver_instance)
        expand_replies(driver_instance, url)
        usernames = scroll_and_load_replies(driver_instance)

        reply_elements = driver.find_elements(By.XPATH, '//article[@data-testid="tweet" and not(ancestor::*[@data-testid="placementTracking"])]')
        print(f"Found {len(reply_elements)} reply articles in final viewport.")
        all_articles = driver.find_elements(By.XPATH, '//article')
        print(f"Found {len(all_articles)} total articles (including main post).")

        return usernames

    except Exception as e:
        print(f"An error occurred: {e}")
        return []

def initialize_driver():
    global driver
    if driver is None:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        user_agent = random.choice(USER_AGENTS)
        options.add_argument(f"user-agent={user_agent}")
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            driver_path = ChromeDriverManager(driver_version="latest").install()
            import os
            correct_path = os.path.join(os.path.dirname(driver_path), "chromedriver")
            os.chmod(correct_path, 0o755)  # Fix permissions
            driver = webdriver.Chrome(service=webdriver.chrome.service.Service(correct_path), options=options)
            print("ChromeDriver initialized with path:", correct_path)
        except WebDriverException as e:
            print(f"Failed to initialize ChromeDriver: {e}")
            raise
    return driver

def close_driver():
    global driver
    if driver is not None:
        print("Closing ChromeDriver...")
        driver.quit()
        driver = None

def is_valid_url(text):
    url_pattern = re.compile(r'^(https?://[^\s]+)$')
    return bool(url_pattern.match(text.strip()))

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await chat.get_member(user.id)
    return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await is_admin(update, context):
            await update.message.reply_text("🚫 This command is for admins only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_data
    global_data = {
        "comments": [],
        "verify": {},
        "track_usernames": [],
        "summary": "",
        "step": "waiting_main",
        "track_step": 1
    }
    await update.message.reply_text("Send me the main tweet link (data will be shared across all groups).")

@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if global_data["step"] is None:
        return await update.message.reply_text("Use /start first.")

    step = global_data["step"]
    text = update.message.text.strip()
    cookies_file = "cookies.json"  
    driver_instance = initialize_driver()

    if step == "waiting_main":
        if not is_valid_url(text):
            return await update.message.reply_text("Please send a valid link (e.g., https://...). referred from X")
        usernames = [u for u in scrape_x_usernames(text, cookies_file, driver_instance) if u not in ignored_users]
        if not usernames:
            return await update.message.reply_text("No usernames found in the link! Ensure cookies are valid.")
        global_data["comments"] = usernames
        global_data["step"] = "waiting_tweet_count"
        await update.message.reply_text(f"Extracted {len(usernames)} usernames from the link. Now send the number of verification tweets.")

    elif step == "waiting_tweet_count":
        if not text.isdigit():
            return await update.message.reply_text("Please enter a valid number.")
        
        count = int(text)
        if count < 1:
            return await update.message.reply_text("Please enter a number greater than 0.")
        
        global_data["verify"] = {i: [] for i in range(1, count + 1)}
        global_data["step"] = "waiting_verification_links"
        await update.message.reply_text(f"Send me {count} verification tweet links separated by commas (e.g., link1, link2, link3).")

    elif step == "waiting_verification_links":
        links = [link.strip() for link in text.split(",")]
        expected_count = len(global_data["verify"])

        if len(links) != expected_count:
            return await update.message.reply_text(f"Please provide exactly {expected_count} links separated by commas.")

        for i, link in enumerate(links, start=1):
            if not is_valid_url(link):
                return await update.message.reply_text(f"Invalid link at position {i}: '{link}'. All inputs must be valid URLs.")
            usernames = [u for u in scrape_x_usernames(link, cookies_file, driver_instance) if u not in ignored_users]
            if not usernames:
                return await update.message.reply_text(f"No usernames found in link {i}: '{link}'! Ensure cookies are valid.")
            global_data["verify"][i] = usernames
            global_data["track_usernames"].append(f"Link {i}")

        close_driver()
        await list_command(update, context)
        await summary_command(update, context)
        global_data["step"] = None

@admin_only
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not global_data["comments"]:
        return await update.message.reply_text("No data found. Use /start first.")

    main_tweet_users = global_data["comments"]
    verified_users = global_data["verify"]
    track_usernames = global_data["track_usernames"]

    table_data = []
    for username in main_tweet_users:
        row = [username]
        for step, track_name in enumerate(track_usernames, start=1):
            row.append("✅" if username in verified_users.get(step, []) else "❌")
        table_data.append(row)

    headers = ["Username"] + track_usernames
    table_output = tabulate(table_data, headers=headers, tablefmt="grid")

    for chunk in split_message(table_output):
        await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="MarkdownV2")

@admin_only
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not global_data["comments"]:
        return await update.message.reply_text("No data found. Use /start first.")

    main_tweet_users = global_data["comments"]
    verified_users = global_data["verify"]
    total_tracks = len(verified_users)

    missing_counts = {}
    scammer_count = 0

    for username in main_tweet_users:
        missing_count = sum(1 for step in range(1, total_tracks + 1) 
                          if username not in verified_users.get(step, []))
        if missing_count > 0:
            scammer_count += 1
            missing_counts.setdefault(missing_count, []).append(username)

    missing_summary = "\n".join(
        f"{count} tweets missed by:\n" + "\n".join(users) + "\n"
        for count, users in sorted(missing_counts.items(), reverse=True)
    ) if missing_counts else "No scammers found (all users fully verified)."

    output_percentage = round(((len(main_tweet_users) - scammer_count) / len(main_tweet_users)) * 100, 2) if main_tweet_users else 0

    final_summary = f"""
Total count in main tweet: {len(main_tweet_users)}
Total Scammers Count: {scammer_count}
Output Percentage: {output_percentage}%

{missing_summary}
    """.strip()

    global_data["summary"] = final_summary

    for chunk in split_message(final_summary):
        await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="MarkdownV2")

@admin_only
async def ignore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ignored_users
    text = update.message.text.replace("/ignore", "").strip()
    if not text:
        ignored_list = "\n".join(ignored_users) if ignored_users else "No ignored users."
        return await update.message.reply_text(f"Currently ignored users:\n{ignored_list}")

    usernames = re.findall(r"@\w+", text)
    if usernames:
        ignored_users.update(usernames)
        await update.message.reply_text(f"Added to ignore list: {', '.join(usernames)}")
    else:
        ignored_users.clear()
        await update.message.reply_text("Ignore list cleared.")

@admin_only
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global global_data
    global_data = {
        "comments": [],
        "verify": {},
        "track_usernames": [],
        "summary": "",
        "step": None,
        "track_step": 1
    }
    close_driver()
    await update.message.reply_text("Global data cleared. Ignore list is still active. Start fresh with /start.")

def split_message(text, chunk_size=4000):
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable not set")
    app = Application.builder().token(bot_token).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("ignore", ignore_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is running...")
    try:
        app.run_polling()
    finally:
        close_driver()

if __name__ == "__main__":
    main()
