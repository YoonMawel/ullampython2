from mastodon import Mastodon, StreamListener
import gspread
import time
from oauth2client.service_account import ServiceAccountCredentials
import json, os, re, random, threading, queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytz
import threading

random.seed()

# ===== 설정 =====
MASTODON_ACCESS_TOKEN = 'HCPH-PcENrg4lb6p2FfpkwlmslTm8iWfBrIbTx-rz64'
MASTODON_API_BASE_URL = 'https://ullambana.xyz'
SHEET_NAME = "조사 - 개별(매일 1회)"
WORKSHEET_NAME = "개별자동봇"
INVENTORY_SHEET_NAME = "인벤토리"
GCP_CREDENTIALS_FILE = "credentials.json"
KST = pytz.timezone("Asia/Seoul")
ACTION_SHEET_NAME = "행동력"


#나태 러너 조사 횟수 확장 전역변수 (계속 하드코딩해서 추가해 줘야 함)
EXTRA_LIMIT_USERS = {
    "test": 100,
    "DBYuRa": 3,
    "barcord": 3,
    "AnnYr": 3,
    "Zhiwei": 3,
    "DEMONS": 3,
    "Liu_wonlan": 3,
    "POLARLIGHT": 3,
    "Lee_Sak": 3,
    "P_1122": 3,
    "chy": 3,
    "justlikeTT": 3
}

# ===== 파일 정의 =====
COUNT_FILE = "user_daily_count.json"
REWARD_FILE = "user_reward.json"
LAST_FILE = "user_last_daily.json"

# ===== JSON 로드 =====
def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def parse_item_string(item_str):
    # 예: "금 5개, 약 2개, 횃불 1개"
    results = []
    for token in item_str.split(","):
        match = re.match(r"\s*(.+?)\s*(\d+)개\s*", token.strip())
        if match:
            name = match.group(1).strip()
            count = int(match.group(2))
            results.append((name, count))
    return results

def add_item(user, item_str):
    inventory = get_user_inventory(user)

    for item, qty in parse_item_string(item_str):
        inventory[item] = inventory.get(item, 0) + qty

    update_inventory(user, inventory)

def remove_item(user, item_str):
    inventory = get_user_inventory(user)

    for item, qty in parse_item_string(item_str):
        inventory[item] = max(inventory.get(item, 0) - qty, 0)

    update_inventory(user, inventory)

user_counts = load_json(COUNT_FILE)
user_rewards = load_json(REWARD_FILE)
user_last = load_json(LAST_FILE)

# ===== 구글 시트 연동 =====
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GCP_CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)

SPREADSHEET_ID = "1Wt251QshkAaWWS-QybmSjT7AavatMb2nu--rgKcUDIA"
sheet_main = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
sheet_inventory = client.open_by_key(SPREADSHEET_ID).worksheet(INVENTORY_SHEET_NAME)
sheet_action = client.open_by_key(SPREADSHEET_ID).worksheet(ACTION_SHEET_NAME)

sheet_data_raw = sheet_main.get_all_records()

# 유효한 문장만 필터링
sheet_data = [
    row for row in sheet_data_raw
    if str(row.get("조사 ID", "")).strip() != "" and str(row.get("메인 문장", "")).strip() != ""
]

def get_user_action_info(user):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    all_users = sheet_action.col_values(1)
    user_clean = user.strip().lower()

    row_index = None
    for i, uid in enumerate(all_users):
        if uid.strip().lower() == user_clean:
            row_index = i + 1
            break

    if row_index is None:
        # 유저 없으면 기본 행동력 1로 새로 추가
        sheet_action.append_row([user, 1, today, 0])
        return 1, 0  # max_action, used_count

    row = sheet_action.row_values(row_index)
    max_action = int(row[1]) if len(row) > 1 and row[1].isdigit() else 1
    last_date = row[2] if len(row) > 2 else ""
    used_count = int(row[3]) if len(row) > 3 and row[3].isdigit() else 0

    if last_date != today:
        # 날짜가 바뀌었으면 사용횟수 초기화
        sheet_action.update(f"C{row_index}:D{row_index}", [[today, 0]])
        return max_action, 0

    return max_action, used_count

def consume_action_point(user):
    print(f"[DEBUG] 행동력 차감 시도 - {user}")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    all_users = sheet_action.col_values(1)
    user_clean = user.strip().lower()

    row_index = None
    for i, uid in enumerate(all_users):
        if uid.strip().lower() == user_clean:
            row_index = i + 1
            break

    if row_index is None:
        # 유저 없으면 기본 행동력 1로 추가 후 바로 1 사용
        sheet_action.append_row([user, 1, today, 1])
        return

    row = sheet_action.row_values(row_index)
    last_date = row[2] if len(row) > 2 else ""
    try:
        used = int(row[3].strip())
    except:
        used = 0

    print(f"[DEBUG] {user} → 행동력 차감 직전 used={used}, 날짜={last_date}, 오늘={today}")

    if last_date != today:
        # 날짜가 바뀌었으면 초기화하고 사용 1회
        sheet_action.update(f"C{row_index}:D{row_index}", [[today, 1]])
    else:
        sheet_action.update_cell(row_index, 4, used + 1)

def can_use_action_point(user):
    max_action, used = get_user_action_info(user)
    return used < max_action

def reset_action_points():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    all_records = sheet_action.get_all_values()

    for i, row in enumerate(all_records[1:], start=2):  # 첫 줄은 헤더니까 2번 줄부터
        sheet_action.update(f"C{i}:D{i}", [[today, 0]])

    print(f"[{today}] 행동력 전체 초기화 완료됨.")


# ===== 시트 기반 인벤토리 도우미 함수 =====
def get_user_inventory(user):
    headers = sheet_inventory.row_values(1)
    all_users = sheet_inventory.col_values(1)
    user_clean = user.strip().lower()

    row_index = None
    for i, uid in enumerate(all_users):
        if uid.strip().lower() == user_clean:
            row_index = i + 1
            break

    if row_index is None:
        sheet_inventory.append_row([user, 0, 0, "-"])
        return {"금": 0, "영혼": 0}

    row = sheet_inventory.row_values(row_index)
    gold = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
    soul = int(row[2]) if len(row) > 2 and row[2].isdigit() else 0
    items_raw = row[3] if len(row) > 3 else "-"
    items = {}

    if items_raw and items_raw != "-":
        for token in items_raw.split(","):
            match = re.match(r"\s*(.+?)x(\d+)개\s*", token.strip())
            if match:
                name = match.group(1).strip()
                count = int(match.group(2))
                items[name] = count

    items["금"] = gold
    items["영혼"] = soul
    return items

def update_inventory(user, inventory):
    all_users = sheet_inventory.col_values(1)
    user_clean = user.strip().lower()
    row_index = None

    for i, uid in enumerate(all_users):
        if uid.strip().lower() == user_clean:
            row_index = i + 1
            break

    if row_index is None:
        row_index = len(all_users) + 1
        sheet_inventory.update_cell(row_index, 1, user)

    gold = inventory.get("금", 0)
    soul = inventory.get("영혼", 0)
    item_strs = [
        f"{k}x{v}개" for k, v in inventory.items()
        if k not in ["금", "영혼"] and v > 0
    ]
    item_cell = ", ".join(item_strs) if item_strs else "-"
    sheet_inventory.update(f"B{row_index}:D{row_index}", [[gold, soul, item_cell]])

# ===== 마스토돈 세팅 =====
mastodon = Mastodon(
    access_token=MASTODON_ACCESS_TOKEN,
    api_base_url=MASTODON_API_BASE_URL
)

me = mastodon.account_verify_credentials()
print(f"로그인 봇: @{me['acct']}")

mention_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=10)


def reset_daily_counts():
    while True:
        # 자정까지 기다렸다가 리셋
        now = datetime.now(KST)
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        wait_seconds = (target - now).total_seconds()
        print(f"자정까지 {wait_seconds:.2f}초 대기 중...")
        threading.Event().wait(wait_seconds)

        # 초기화
        user_counts.clear()
        save_json(COUNT_FILE, user_counts)
        user_rewards.clear()
        save_json(REWARD_FILE, user_rewards)

        reset_action_points()
        print("일일 조사 기록 초기화 완료")
        mastodon.status_post(
            status="일일 조사 횟수와 보상 기록, 행동력이 초기화 되었습니다.",
            visibility="public"
        )

def reminder_loop():
    while True:
        now = datetime.now(KST)
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        wait_seconds = (target - now).total_seconds()
        hours = int(wait_seconds // 3600)
        minutes = int((wait_seconds % 3600) // 60)
        mastodon.status_post(
            status=f"조사 초기화까지 {hours}시간 {minutes}분 대기 중. . .",
            visibility="public"
        )
        time.sleep(10800)  # 3시간 = 10800초

def handle_daily_survey(user, status_id): #응답 처리
    count = user_counts.get(user, 0)
    max_count = EXTRA_LIMIT_USERS.get(user, 1)  #기본은 1회, 예외 유저는 커스텀
    if count >= max_count:
        mastodon.status_post(
            status=f"@{user}\n금일 조사 횟수를 모두 소진하였습니다.",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return

    candidate = random.choice(sheet_data)
    sentence_id = str(candidate.get("조사 ID"))
    message = candidate.get("메인 문장", "").strip()
    message = f"[ID {sentence_id}]\n{message}"

    has_followup = candidate.get("추가 상황 여부", "").strip() == "TRUE"
    followup_key = candidate.get("추가 키워드", "").strip()

    user_counts[user] = count + 1
    user_last[user] = sentence_id
    save_json(COUNT_FILE, user_counts)
    save_json(LAST_FILE, user_last)

    # === 보상 지급 ===
    if give_daily_spirit(user):
        message += "\n(보상: 영혼 1개 지급됨)"
    else:
        message += "\n(오늘의 영혼 보상은 이미 받았습니다)"

    if has_followup and followup_key:
        message += f"\n추가 조사 가능: [일일/{followup_key}]"
    mastodon.status_post(
        status=f"@{user}\n{message}",
        in_reply_to_id=status_id,
        visibility="unlisted"
    )

def has_received_spirit_today(user):
    sheet_log = client.open_by_key(SPREADSHEET_ID).worksheet("일일영혼지급")
    records = sheet_log.get_all_values()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    for row in records[1:]:  # 첫 줄은 헤더니까 생략
        if len(row) >= 2 and row[0] == user and row[1] == today:
            return True
    return False

def give_daily_spirit(user):
    if has_received_spirit_today(user):
        return False  # 이미 오늘 지급했음
    add_item(user, "영혼 1개")
    log_daily_spirit_given(user)
    return True  # 새로 지급했음

def log_daily_spirit_given(user):
    sheet_log = client.open_by_key(SPREADSHEET_ID).worksheet("일일영혼지급")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    sheet_log.append_row([user, today])

def handle_followup(user, status_id, followup_key):  # 추가 선택지 처리
    last_id = user_last.get(user)
    if not last_id:
        mastodon.status_post(
            status=f"@{user}\n추가 조사를 진행할 수 있는 문장이 없습니다. 먼저 [일일조사]를 진행해주세요.",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return  # 빠뜨리면 안 됨!!

    if not can_use_action_point(user):
        mastodon.status_post(
            status=f"@{user}\n행동력이 부족하여 추가 조사를 진행할 수 없습니다.",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return  # 여기서도 return 필수!

    consume_action_point(user) #행동력 소모 카운트

    match = next((row for row in sheet_data if str(row.get("조사 ID")) == last_id), None)
    if not match or match.get("추가 키워드", "").strip() != followup_key:
        mastodon.status_post(
            status=f"@{user}\n해당 키워드는 유효하지 않습니다.",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return

    reward_key = f"{user}:{last_id}"
    already_claimed = reward_key in user_rewards
    response = match.get("추가 문장", "").strip()

    if already_claimed:
        response += "\n(이미 아이템을 획득한 문장입니다)"
        mastodon.status_post(
            status=f"@{user}\n{response}",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        return

    # ===== 소모 재화 처리 =====
    consume_str = match.get("소모 재화", "").strip()
    if consume_str:
        parsed_items = parse_item_string(consume_str)
        inventory = get_user_inventory(user)
        insufficient = []

        for name, amount in parsed_items:
            if inventory.get(name, 0) < amount:
                insufficient.append(f"{name} {amount}개")

        if insufficient:
            response += "\n[진행 불가] 다음 재화가 부족합니다: " + ", ".join(insufficient)
            mastodon.status_post(
                status=f"@{user}\n{response}",
                in_reply_to_id=status_id,
                visibility="unlisted"
            )
            return
        else:
            remove_item(user, consume_str)
            response += f"\n[소모됨] {consume_str}"

    # ===== 아이템 처리 =====
    item = match.get("지급 아이템", "").strip()
    if item:
        items = [x.strip() for x in item.split(",") if x.strip()]
        for itm in items:
            if any(word in itm for word in ["분실", "도난", "차감"]):
                remove_item(user, itm)
                response += f"\n[분실] {itm}"
            else:
                add_item(user, itm)
                response += f"\n[획득] {itm}"
    else:
        response += "\n(아이템이 없는 추가 선택지입니다)"

    user_rewards[reward_key] = True
    save_json(REWARD_FILE, user_rewards)

    mastodon.status_post(
        status=f"@{user}\n{response}",
        in_reply_to_id=status_id,
        visibility="unlisted"
    )

def parse_input(text):
    match_daily = re.search(r"\[일일조사\]", text)
    match_followup = re.search(r"\[일일\/([^\]]+)\]", text)
    if match_daily:
        return "daily", None
    elif match_followup:
        return "followup", match_followup.group(1).strip()
    return None, None

def handle_mention(notification):
    status = notification["status"]
    user = status["account"]["acct"]
    content = re.sub('<[^<]+?>', '', status["content"]).strip()
    print(f"멘션 수신: @{user} → {content}")

    kind, detail = parse_input(content)
    if kind == "daily":
        handle_daily_survey(user, status["id"])
    elif kind == "followup":
        handle_followup(user, status["id"], detail)

class BotListener(StreamListener):
    def on_notification(self, notification):
        if notification["type"] == "mention":
            mention_queue.put(notification)

def process_mentions():
    while True:
        try:
            notification = mention_queue.get()
            if notification:
                executor.submit(handle_mention, notification) #병렬 처리
        except Exception as e:
            print(f"[ERROR] mention 처리 중 오류: {e}")

# 스레드 시작
threading.Thread(target=process_mentions, daemon=True).start()
threading.Thread(target=reset_daily_counts, daemon=True).start()
threading.Thread(target=reminder_loop, daemon=True).start()
mastodon.stream_user(BotListener())


