import gspread
from oauth2client.service_account import ServiceAccountCredentials
import random
from mastodon import Mastodon, StreamListener
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import queue
import time
from bs4 import BeautifulSoup

# 구글 스프레드시트 인증
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gc = gspread.authorize(credentials)

shop_sheet = gc.open("상점").worksheet("가게")
inventory_sheet = gc.open("조사 - 개별(매일 1회)").worksheet("인벤토리")

# 마스토돈 인스턴스 및 봇 인증
mastodon = Mastodon(
    access_token="UE8b79X93UU9LC34f4RBafhJvXzBH3tDJ2bMjasz4Rk",
    api_base_url="https://ullambana.xyz"
)

# 명령어 파싱 정규표현식
command_pattern = re.compile(r'\[\s*(구매|사용)\s*/\s*(.+?)\s*/\s*(\d+)\s*\]')

task_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=15)

shop_cache = None
shop_last_updated = 0  # timestamp
SHOP_CACHE_TTL = 3600

ADMIN_USERS = {"admin", "test", "shop"} #수동 명령어 허용 유저

random_box_sheet = gc.open("상점").worksheet("랜덤상자")
random_box_cache = {}
random_box_last_updated = 0
RANDOM_BOX_CACHE_TTL = 21600 #6시간

# --- 워커 ---
def worker():
    while True:
        task = task_queue.get()
        if task is None:
            break
        user, status_id, action, item_name, count = task
        if action == "구매":
            result = handle_purchase(user, item_name, count)
        elif action == "사용":
            result = handle_gamble(user, item_name, count)
        elif action == "랜덤":
            result = handle_random_box(user, item_name, count)
        else:
            result = f"[{action}]은(는) 알 수 없는 명령입니다."

        mastodon.status_post(
            status=f"@{user}\n{result}",
            in_reply_to_id=status_id,
            visibility="unlisted"
        )
        task_queue.task_done()

# 워커 스레드 시작
for _ in range(15):
    threading.Thread(target=worker, daemon=True).start()

# 상점 데이터를 딕셔너리로 캐싱
def get_shop_items():
    global shop_cache, shop_last_updated
    now = time.time()

    if shop_cache is None or now - shop_last_updated > SHOP_CACHE_TTL:
        records = shop_sheet.get_all_records()
        shop_cache = {row["아이템명"]: row for row in records}
        shop_last_updated = now

    return shop_cache

def reset_shop_cache():
    global shop_cache, shop_last_updated
    shop_cache = None
    shop_last_updated = 0

# 유저 인벤토리 찾기
def get_user_row(username):
    usernames = inventory_sheet.col_values(1)
    if username in usernames:
        return usernames.index(username) + 1
    return None

# 아이템 구매 처리
def handle_purchase(username, item_name, quantity):
    shop = get_shop_items()
    if item_name not in shop:
        return f"'{item_name}'은(는) 판매하지 않는 아이템입니다."

    item = shop[item_name]
    cost = int(item["가격"]) * quantity
    currency = item["재화타입"]  # 금 or 영혼
    row = get_user_row(username)

    if not row:
        return f"{username}님의 인벤토리를 찾을 수 없습니다."

    # 금 or 영혼 열 위치 확인
    col_map = {"금": 2, "영혼": 3}
    curr_col = col_map[currency]
    curr_value = int(inventory_sheet.cell(row, curr_col).value)

    if curr_value < cost:
        return f"{currency}이(가) 부족합니다. {cost} 필요, 현재 {curr_value}"

    # 재화 차감
    inventory_sheet.update_cell(row, curr_col, curr_value - cost)

    # 아이템 열 찾기 (없으면 생성)
    header = inventory_sheet.row_values(1)
    if item_name not in header:
        inventory_sheet.update_cell(1, len(header) + 1, item_name)
        header.append(item_name)

    item_col = header.index(item_name) + 1

    curr_item_qty = int(inventory_sheet.cell(row, item_col).value or "0")
    inventory_sheet.update_cell(row, item_col, curr_item_qty + quantity)

    return f"{username}님이 {item_name} {quantity}개를 구매했습니다."

# 도박 아이템 사용 처리
def handle_gamble(username, item_name, quantity):
    shop = get_shop_items()
    item = shop.get(item_name)
    if not item or str(item.get("도박여부", "")).upper() != "TRUE":
        return f"{item_name}은(는) 도박 아이템이 아닙니다."

    row = get_user_row(username)
    if not row:
        return f"{username}님의 인벤토리를 찾을 수 없습니다."

    header = inventory_sheet.row_values(1)
    if item_name not in header:
        return f"'{item_name}'에 대한 인벤토리 열이 없습니다."

    item_col = header.index(item_name) + 1
    curr_qty = int(inventory_sheet.cell(row, item_col).value or "0")
    if curr_qty < quantity:
        return f"{item_name}이(가) 부족합니다. 현재 {curr_qty}개 보유 중"

    # (1) 가격 확인
    unit_cost = int(item["가격"])
    total_cost = unit_cost * quantity

    # (2) 현재 금화 확인
    curr_gold = int(inventory_sheet.cell(row, 2).value)
    if curr_gold < total_cost:
        return f"금이 부족합니다. {total_cost}골드 필요, 현재 {curr_gold}골드 보유 중"

    # (3) 도박 실행
    results = []
    payout = 0
    for _ in range(quantity):
        r = random.random()
        if r < 0.30:
            multiplier = 0
        elif r < 0.70:
            multiplier = 1
        elif r < 0.95:
            multiplier = 2
        else:
            multiplier = 5
        results.append(multiplier)
        payout += unit_cost * multiplier

    # (4) 결과 반영
    inventory_sheet.update_cell(row, 2, curr_gold - total_cost + payout)
    inventory_sheet.update_cell(row, item_col, curr_qty - quantity)

    return (
        f"{username}님이 '{item_name}' {quantity}개를 사용했습니다.\n"
        f"배수 결과: {results}\n"
        f"획득 금화: 금 {payout}개 (지출 {total_cost} → 최종 보유금 {curr_gold - total_cost + payout})"
    )

def handle_random_box(username, item_name, quantity):
    box_map = get_random_box_pools()
    if item_name not in box_map:
        return f"{item_name}은(는) 랜덤 보상 아이템이 아닙니다."

    row = get_user_row(username)
    if not row:
        return f"{username}님의 인벤토리를 찾을 수 없습니다."

    header = inventory_sheet.row_values(1)
    if item_name not in header:
        return f"{item_name}에 대한 인벤토리 열이 없습니다."

    item_col = header.index(item_name) + 1
    curr_qty = int(inventory_sheet.cell(row, item_col).value or "0")
    if curr_qty < quantity:
        return f"{item_name}이(가) 부족합니다. 현재 {curr_qty}개 보유 중"

    granted = []
    for _ in range(quantity):
        reward = random.choice(box_map[item_name])
        granted.append(reward)

        # 보상 아이템 열 없으면 추가
        if reward not in header:
            inventory_sheet.update_cell(1, len(header) + 1, reward)
            header.append(reward)

        reward_col = header.index(reward) + 1
        current_count = int(inventory_sheet.cell(row, reward_col).value or "0")
        inventory_sheet.update_cell(row, reward_col, current_count + 1)

    # 상자 수량 차감
    inventory_sheet.update_cell(row, item_col, curr_qty - quantity)

    return f"{username}님이 {item_name} {quantity}개를 열었습니다. 획득한 아이템: {', '.join(granted)}"



def get_random_box_pools():
    global random_box_cache, random_box_last_updated
    now = time.time()

    if not random_box_cache or now - random_box_last_updated > RANDOM_BOX_CACHE_TTL:
        records = random_box_sheet.get_all_records()
        box_map = {}
        for row in records:
            box_name = row["상자명"].strip()
            item = row["구성 아이템"].strip()
            if box_name and item:
                box_map.setdefault(box_name, []).append(item)

        random_box_cache = box_map
        random_box_last_updated = now

    return random_box_cache

def reset_random_box_cache():
    global random_box_cache, random_box_last_updated
    random_box_cache = {}
    random_box_last_updated = 0

# --- 리스너 정의 ---
class ShopBotListener(StreamListener):
    def on_notification(self, notification):
        if notification["type"] != "mention":
            return  # 멘션이 아닌 알림은 무시

        print("on_notification 호출됨 (mention 타입 감지됨)")
        try:
            status = notification["status"]
            content_html = status["content"]
            content = BeautifulSoup(content_html, "html.parser").get_text()
            user = status["account"]["acct"]
            status_id = status["id"]
            print(f"멘션 감지됨: @{user} / 내용: {content}")

            # [상점갱신] 수동 명령 처리
            if "[상점갱신]" in content and user in ADMIN_USERS:
                reset_shop_cache()
                mastodon.status_post(
                    status=f"@{user}\n상점 품목을 갱신했습니다.",
                    in_reply_to_id=status_id,
                    visibility="unlisted"
                )
                return

            # [상자갱신] 수동 명령 처리
            if "[상자갱신]" in content and user in ADMIN_USERS:
                reset_random_box_cache()
                mastodon.status_post(
                    status=f"@{user}\n랜덤 상자 내용을 갱신했습니다.",
                    in_reply_to_id=status_id,
                    visibility="unlisted"
                )
                return

            # 명령어 처리
            box_map = get_random_box_pools()
            matches = command_pattern.findall(content)
            if not matches:
                return

            for action, item_name, count_str in matches:
                count = int(count_str)
                if action == "사용" and item_name in box_map:
                    task_queue.put((user, status_id, "랜덤", item_name, count))
                else:
                    task_queue.put((user, status_id, action, item_name, count))

        except Exception as e:
            print(f"멘션 처리 중 오류: {e}")

# 로그인된 봇 계정 정보 출력
me = mastodon.account_verify_credentials()
print(f"상점봇 로그인됨: @{me['username']} ({me['acct']})")

# --- 스트리밍 시작 ---
print("상점봇 스트리밍 시작...")
mastodon.stream_user(ShopBotListener())