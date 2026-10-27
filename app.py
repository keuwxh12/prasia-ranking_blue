import os
import time
import re
import gspread
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from selenium import webdriver

# --- 1. 페이지 및 설정 초기화 ---
st.set_page_config(page_title="프라시아 전기 랭킹 크롤러", page_icon="⚔️", layout="centered")

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1FIN_OHmXrXKDB2BYaJ-mKeRFWo7W2-g0KW_UZzC8zTI/edit"
TARGET_SHEET_NAME = "결사원 상세 리스트"

# 월드 및 클래스 매핑
WORLD_MAP = {
    "2-1": "론도1", "2-2": "론도2", "2-3": "론도3", "2-4": "론도4", "2-5": "론도5",
    "3-1": "라인소프1", "3-2": "라인소프2", "3-3": "라인소프3", "3-4": "라인소프4", "3-5": "라인소프5",
    "5-1": "아민타1", "5-2": "아민타2", "5-3": "아민타3", "5-4": "아민타4", "5-5": "아민타5",
    "8-1": "가리안1", "8-2": "가리안2", "8-3": "가리안3", "8-4": "가리안4", "8-5": "가리안5",
    "10-1": "사도바1", "10-2": "사도바2", "10-3": "사도바3", "10-4": "사도바4", "10-5": "사도바5",
    "11-1": "제롬1", "11-2": "제롬2", "11-3": "제롬3", "11-4": "제롬4", "11-5": "제롬5",
    "12-1": "아티산1", "12-2": "아티산2", "12-3": "아티산3", "12-4": "아티산4", "12-5": "아티산5"
}

CLASS_MAP = {
    "enforcer": "집행관", "runescribe": "주문각인사", "incensearcher": "향사수",
    "mirageblade": "환영검사", "solarsentinel": "태양감시자", "abyssrevenant": "심연추방자", "wildwarrior": "야만투사"
}

WORLDS = list(WORLD_MAP.keys())
CLASSES = list(CLASS_MAP.keys())

def get_gspread_client():
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        credentials_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(credentials_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"구글 시트 인증 에러: {e}")
        return None

def parse_row_text(text):
    level_match = re.search(r'Lv\.(\d+)', text)
    level = level_match.group(1) if level_match else ""
    subjugation_match = re.search(r'토벌\s*등급\s*(\d+)', text)
    subjugation = subjugation_match.group(1) if subjugation_match else ""
    guild_match = re.search(r'결사명\s*(.+?)(?=\s*토벌|\s*Lv|\s*$)', text)
    guild = guild_match.group(1).strip() if guild_match else "-"
    
    name = ""
    name_match = re.search(r'(?:닉네임|Lv\.\d+)\s*([가-힣a-zA-Z0-9_ -]{1,20})', text)
    if name_match:
        name = name_match.group(1)
        name = re.sub(r'결사명.*|토벌.*', '', name).strip()
    if not name or name == level:
        fallback_match = re.search(r'^\d+[-]?\s*(.*?)\s*Lv\.\d+', text)
        if fallback_match:
            candidate = fallback_match.group(1)
            candidate = re.sub(r'결사명.*|토벌.*', '', candidate).strip()
            if candidate and not candidate.isdigit():
                name = candidate
    if not name or len(name) < 2 or (name.isdigit() and len(name) <= 1):
        name = ""
    return name, guild, subjugation, level

def crawl_worker(task_chunk, worker_id):
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    driver = webdriver.Chrome(options=options)
    results = []
    try:
        for idx, (world_code, cls_code, server_name, cls_name) in enumerate(task_chunk):
            target_url = f"https://wp.nexon.com/records/ranking?world={world_code}&class={cls_code}"
            rows = []
            for attempt in range(2):
                try:
                    driver.get(target_url)
                    time.sleep(2.2)
                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                    rows = soup.select("ul[class*='list'] > li")
                    if not rows:
                        rows = soup.select("table tbody tr")
                    if rows:
                        break
                except Exception:
                    time.sleep(1)

            for row in rows:
                row_text = row.get_text(strip=True)
                if "순위" in row_text or "닉네임" in row_text or not row_text:
                    continue
                name, guild, subjugation, level = parse_row_text(row_text)
                if not name:
                    continue
                results.append({
                    "server_name": server_name, "guild": guild, "cls_name": cls_name,
                    "name": name, "level": level, "subjugation": subjugation
                })
    finally:
        driver.quit()
    return results

def run_crawling_process(status_placeholder):
    status_placeholder.text("1. 구글 서비스 계정 인증 중...")
    gc = get_gspread_client()
    if not gc:
        return False, "❌ 구글 시트 클라이언트 인증 실패"

    try:
        doc = gc.open_by_url(SPREADSHEET_URL)
        worksheet = doc.worksheet(TARGET_SHEET_NAME)
    except Exception as e:
        return False, f"❌ 구글 시트 연결 실패: {e}"

    status_placeholder.text("2. 멀티스레드 고속 크롤링 시작 (총 245개 페이지)... 잠시만 기다려주세요.")
    start_time = time.time()

    all_tasks = []
    for world_code in WORLDS:
        server_name = WORLD_MAP[world_code]
        for cls_code in CLASSES:
            cls_name = CLASS_MAP[cls_code]
            all_tasks.append((world_code, cls_code, server_name, cls_name))

    max_workers = 5
    chunk_size = len(all_tasks) // max_workers + (1 if len(all_tasks) % max_workers != 0 else 0)
    task_chunks = [all_tasks[i:i + chunk_size] for i in range(0, len(all_tasks), chunk_size)]

    all_ae_rows = []
    all_g_rows = []
    seen_names = set()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(crawl_worker, chunk, i+1) for i, chunk in enumerate(task_chunks)]
        for future in as_completed(futures):
            try:
                chunk_results = future.result()
                for data in chunk_results:
                    unique_key = f"{data['server_name']}_{data['name']}"
                    if unique_key not in seen_names:
                        seen_names.add(unique_key)
                        all_ae_rows.append([data['server_name'], data['guild'], data['cls_name'], data['name'], data['level']])
                        all_g_rows.append([data['subjugation']])
            except Exception as fe:
                print(f"⚠️ 스레드 작업 중 예외 발생: {fe}")

    end_time = time.time()
    elapsed = end_time - start_time

    if all_ae_rows:
        status_placeholder.text("3. 구글 시트 데이터 초기화 및 덮어쓰는 중...")
        try:
            worksheet.batch_clear(["A5:E10000", "G5:G10000"])
        except Exception:
            pass

        last_row = 4 + len(all_ae_rows)
        worksheet.update(values=all_ae_rows, range_name=f"A5:E{last_row}")
        worksheet.update(values=all_g_rows, range_name=f"G5:G{last_row}")
        return True, f"✅ 업데이트 완료! (소요 시간: {elapsed:.2f}초, 수집 건수: {len(all_ae_rows)}건)"
    else:
        return False, "⚠️ 수집된 데이터가 0건이어서 시트에 덮어쓰지 않았습니다."

# --- 웹 화면 UI ---
st.title("⚔️ 프라시아 전기 결사원 상세 랭킹 크롤러")
st.markdown("버튼을 누르면 넥슨 랭킹 데이터를 수집하여 **[결사원 상세 리스트]** 구글 시트에 최신화합니다.")
st.info(f"대상 구글 시트: [{TARGET_SHEET_NAME}]({SPREADSHEET_URL})")

status_placeholder = st.empty()

if st.button("🚀 랭킹 데이터 수집 및 시트 갱신 시작", type="primary"):
    with st.spinner("🔄 크롤링이 진행 중입니다. 완료될 때까지 창을 닫지 마세요... (약 2~3분 소요)"):
        success, message = run_crawling_process(status_placeholder)
    if success:
        status_placeholder.success(message)
        st.balloons()
    else:
        status_placeholder.error(message)
