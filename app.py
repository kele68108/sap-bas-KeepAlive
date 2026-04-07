import os
import sys
import time
import threading
import queue
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, jsonify, render_template_string, request, make_response

from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 核心配置與多賬號動態加載
# ==========================================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
WEB_TOKEN = os.environ.get("WEB_TOKEN", "default_token")
PORT = int(os.environ.get("PORT", 8080))

def format_url(url_str):
    if not url_str: return None
    url_str = url_str.strip()
    if url_str.startswith("https://"): url_str = url_str[8:]
    elif url_str.startswith("http://"): url_str = url_str[7:]
    return f"https://{url_str}"

ACCOUNTS = []
for i in range(1, 11):
    email = os.environ.get(f"SAP_EMAIL_{i}")
    if email:
        ACCOUNTS.append({
            "id": i,
            "email": email,
            "password": os.environ.get(f"SAP_PASSWORD_{i}"),
            "region_url": format_url(os.environ.get(f"REGION_URL_{i}")),
            "joba_min": os.environ.get(f"JOBA_MINUTE_{i}", "50"),
            "jobb_hrs": os.environ.get(f"JOBB_HOURS_{i}", "*/12"),
            "jobb_min": os.environ.get(f"JOBB_MINUTE_{i}", "30"),
            "tunnel_url": format_url(os.environ.get(f"TUNNEL_URL_{i}")),
            "fail_count": 0,
            "auto_restart_count": 0,
            "probe_paused": False
        })

task_queue = queue.Queue()
system_busy_event = threading.Event()

# 中文大寫序列化引擎
def get_node_name(acc_id):
    nums = ["零", "壹", "貳", "叁", "肆", "伍", "陸", "柒", "捌", "玖", "拾"]
    return f"SAP_BAS_ {nums[acc_id]}號機" if 1 <= acc_id <= 10 else f"SAP_BAS_ {acc_id}號機"

def get_node_count_str(count):
    nums = ["零", "壹", "貳", "叁", "肆", "伍", "陸", "柒", "捌", "玖", "拾"]
    return nums[count] if 0 <= count <= 10 else str(count)

def format_reboot_times(hrs_str, min_str):
    if ',' in hrs_str:
        return "/".join([f"{h}:{min_str}" for h in hrs_str.split(',')])
    return f"{hrs_str}:{min_str}"

# ==========================================
# 2. 極簡復古日誌系統
# ==========================================
log_queue = deque(maxlen=2000)

class MemoryHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        log_queue.append(msg)

logger = logging.getLogger('SAP_BAS_BOT')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

mem_handler = MemoryHandler()
mem_handler.setFormatter(formatter)
logger.addHandler(mem_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# ==========================================
# 3. 核心通訊組件
# ==========================================
bot = telebot.TeleBot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

def check_tg_auth(message):
    return str(message.chat.id) == TG_CHAT_ID

def send_tg_msg(text):
    if bot and TG_CHAT_ID:
        try:
            bot.send_message(TG_CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"<SYS_ERR_> 遠端推送接口異常: {str(e)} [FAIL]")

def send_tg_photo(photo_path, caption=""):
    if bot and TG_CHAT_ID and os.path.exists(photo_path):
        try:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(TG_CHAT_ID, photo, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"<SYS_ERR_> 圖像流回傳阻斷: {str(e)} [FAIL]")

# ==========================================
# 4. 業務邏輯層
# ==========================================
class SAPController:
    @staticmethod
    def get_workspace_info(account):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{account['region_url']}/index.html")
                
                page.locator("input[name='j_username'], input[type='email']").fill(account['email'])
                if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                     page.locator("button#logOnFormSubmit, button[type='submit']").click()
                     time.sleep(2)
                page.locator("input[name='j_password'], input[type='password']").fill(account['password'])
                page.locator("button#logOnFormSubmit, button[type='submit']").click()
                page.wait_for_url("**/index.html*", timeout=60000)
                
                req_headers = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}
                response = context.request.get(f"{account['region_url']}/ws-manager/api/v1/workspace", headers=req_headers)
                workspaces = response.json()
                browser.close()

                if workspaces:
                    ws = workspaces[0]
                    ws_uuid = ws.get("id") or ws.get("config", {}).get("id")
                    status = ws.get("runtime", {}).get("status", "UNKNOWN")
                    return True, ws_uuid, status
                return False, None, "Not Found"
        except Exception as e:
            return False, None, str(e)

    @staticmethod
    def execute_lifecycle_action(action_type, account):
        acc_id = account['id']
        node_name = get_node_name(acc_id)
        region_url = account['region_url']
        email = account['email']
        password = account['password']
        
        logger.info(f"<EXEC_JOB> 進程提權，執行核心序列: [{action_type}] ({node_name})")
        work_dir = "/tmp"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1920, 'height': 1080})
                page = context.new_page()
                api_request = context.request

                try:
                    logger.info(f" > AUTH_REQ_ {node_name} 請求建立神經元會話... [WAIT]")
                    page.goto(f"{region_url}/index.html")
                    page.locator("input[name='j_username'], input[type='email']").fill(email)
                    if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                         page.locator("button#logOnFormSubmit, button[type='submit']").click()
                         time.sleep(2)
                    page.locator("input[name='j_password'], input[type='password']").fill(password)
                    page.locator("button#logOnFormSubmit, button[type='submit']").click()
                    
                    page.wait_for_url("**/index.html*", timeout=60000)
                    
                    try:
                        page.wait_for_load_state("networkidle")
                        ok_btn = page.locator("button:has-text('OK'), ui5-button:has-text('OK')").first
                        if ok_btn.is_visible(timeout=5000):
                            checkbox = page.locator("input[type='checkbox']").first
                            if checkbox.is_visible(): checkbox.check()
                            ok_btn.click()
                            time.sleep(3) 
                    except Exception:
                        pass
                    
                    logger.info(f" < AUTH_ACK_ {node_name} 鑑權通過，已接管遠端 API 總線... [ OK ]")
                    req_headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    ws_api_url = f"{region_url}/ws-manager/api/v1/workspace"
                    workspaces = api_request.get(ws_api_url, headers=req_headers).json()
                    
                    if not workspaces:
                        logger.error(f"[!!FATAL!!] {node_name} 掛載區未偵測到有效容器實體 [FAIL]")
                        return False
                        
                    ws = workspaces[0]
                    ws_uuid = ws.get("id") or ws.get("config", {}).get("id")
                    username = ws.get("config", {}).get("username", "")
                    display_name = ws.get("config", {}).get("labels", {}).get("ws-manager.devx.sap.com/displayname", ws_uuid)
                    status = ws.get("runtime", {}).get("status")
                    
                    if action_type == "STOP" and status == "STOPPED":
                        msg = f"► <b>指令調度合併 ({node_name})</b>\n目標容器 [<b>{display_name}</b>] 已處於 <b>掛起態 (STOPPED)</b>，動作跳過。"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ {node_name} 算力已掛起，停止指令合併 [ OK ]")
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type == "START" and status == "RUNNING":
                        msg = f"► <b>指令調度合併 ({node_name})</b>\n目標容器 [<b>{display_name}</b>] 已處於 <b>運行態 (RUNNING)</b>，動作跳過。\n💡 <i>若邊緣隧道阻斷請使用 /restart 進行硬重置。</i>"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ {node_name} 狀態已激活，喚醒指令合併 [ OK ]")
                        account['probe_paused'] = False
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True

                    csrf_headers = req_headers.copy()
                    csrf_headers["X-CSRF-Token"] = "Fetch"
                    csrf_token = api_request.get(ws_api_url, headers=csrf_headers).headers.get("x-csrf-token", "")
                    
                    def set_status(target_suspend, target_status):
                        encoded_username = urllib.parse.quote(username)
                        action_url = f"{region_url}/ws-manager/api/v1/workspace/{ws_uuid}?all=false&username={encoded_username}"
                        headers = {"X-CSRF-Token": csrf_token, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
                        payload = {"suspended": target_suspend, "WorkspaceDisplayName": display_name}
                        api_request.put(action_url, headers=headers, data=payload)
                        
                        for _ in range(30):
                            time.sleep(10)
                            curr_ws = next((w for w in api_request.get(ws_api_url, headers=req_headers).json() if w.get("id") == ws_uuid or w.get("config", {}).get("id") == ws_uuid), {})
                            curr_status = curr_ws.get("runtime", {}).get("status", "UNKNOWN")
                            logger.info(f" > SYS_POLL_ {node_name} 狀態輪詢: 期望={target_status}, 當前={curr_status} [WAIT]")
                            if curr_status == target_status:
                                return True
                        return False

                    if action_type in ["RESTART", "STOP"] and status == "RUNNING":
                        logger.info(f" > SYS_HALT_ {node_name} 下發掛起信令，釋放算力資源... [WAIT]")
                        if set_status(True, "STOPPED"):
                            logger.info(f" < TASK_END_ {node_name} 資源釋放完成，已安全掛起 [ OK ]")
                            status = "STOPPED"
                    
                    if action_type == "STOP":
                        msg = f"■ <b>算力釋放完畢 ({node_name})</b>\n目標容器 [<b>{display_name}</b>] 已成功退回掛起狀態。"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ {node_name} 強制休眠指令歸檔 [ OK ]")
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type in ["START", "RESTART", "KEEPALIVE"] and status in ["STOPPED", "STARTING", "RUNNING"]:
                        if status == "STOPPED":
                            logger.info(f" > SYS_BOOT_ {node_name} 申請分配底層計算資源 (等待插入栓深度連接)... [WAIT]")
                            if not set_status(False, "RUNNING"):
                                logger.error(f"[!!FATAL!!] {node_name} 資源分配超時，啟動異常 [FAIL]")
                                return False
                                
                        logger.info(f" > UI_PENET_ {node_name} 注入無頭探測器探針... [WAIT]")
                        page.goto(f"{region_url}/index.html")
                        time.sleep(8)
                        
                        ws_frame = page.frame_locator("iframe#ws-manager")
                        ws_link = ws_frame.locator(f"a[href*='{ws_uuid}']").first
                        ws_link.wait_for(state="visible", timeout=20000)
                        ws_link.click(force=True)
                        logger.info(f" > IDE_LOAD_ {node_name} 等待核心 IDE 構件裝載... [WAIT]")
                        time.sleep(30)
                        
                        logger.info(f" > UI_CLEAN_ {node_name} 執行模態框靜默消除策略... [WAIT]")
                        for _ in range(3):
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                                
                        screenshot_path = f"{work_dir}/capture_{acc_id}_{ws_uuid}.png"
                        page.screenshot(path=screenshot_path)
                        if action_type != "KEEPALIVE":
                            send_tg_photo(screenshot_path, f"■ <b>系統喚醒完成 ({node_name})</b>\n通知：目標容器 [<b>{display_name}</b>] 算力單元已上線！")
                        logger.info(f" < TASK_END_ {node_name} [{action_type}] 調度流程執行成功 [ OK ]")
                        
                        account['probe_paused'] = False
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                except Exception as inner_e:
                    logger.error(f"[!!FATAL!!] {node_name} 運行時發生內核級崩潰 [FAIL]")
                    try:
                        error_shot = f"{work_dir}/error_crash_{acc_id}_{action_type}.png"
                        page.screenshot(path=error_shot)
                        send_tg_photo(error_shot, f"▲ <b>內核級異常警報 ({node_name})</b>\n調度指令: {action_type}\n棧追蹤: <code>{str(inner_e)}</code>")
                    except Exception as pic_e:
                        logger.error(f"<SYS_ERR_> 棧追蹤快照導出失敗: {pic_e} [FAIL]")
                    return False
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[!!FATAL!!] 沙盒環境拉起失敗，環境異常: {str(e)} [FAIL]")
            return False

# ==========================================
# 5. 全局排隊調度中心與隧道探針
# ==========================================
def enqueue_task(action, target_accounts, source):
    task_queue.put({
        "action": action,
        "accounts": target_accounts,
        "source": source
    })
    if source == "MANUAL":
        acc_str = f"{get_node_name(target_accounts[0]['id'])}" if len(target_accounts) == 1 else f"全局 {len(target_accounts)} 個節點"
        msg = f"► <b>調度任務入隊</b>\n目標: <b>{acc_str}</b>\n指令: <b>{action}</b>..."
        logger.info(f"<SCHEDULR> 手動調度隊列 [{action}] 已覆寫進內存 [ OK ]")
        send_tg_msg(msg)

def global_task_worker():
    while True:
        task = task_queue.get()
        system_busy_event.set()
        
        action = task['action']
        accounts = task['accounts']
        source = task['source']
        
        try:
            for acc in accounts:
                SAPController.execute_lifecycle_action(action, acc)
                if len(accounts) > 1:
                    time.sleep(3)
        except Exception as e:
            logger.error(f"<SYS_ERR_> 調度流水線突發阻塞: {e} [FAIL]")
        finally:
            system_busy_event.clear()
            if source == "MANUAL":
                finish_msg = "■ <b>終端報告</b>\n隊列任務已清空，全局硬件鎖已釋放。"
                logger.info("<SCHEDULR> 調度隊列執行完畢，互斥鎖已解除 [ OK ]")
                send_tg_msg(finish_msg)
            task_queue.task_done()

threading.Thread(target=global_task_worker, daemon=True).start()

def async_task_runner(action, account):
    enqueue_task(action, [account], "CRON")

def bot_action_runner(action, target_id=None):
    target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
    if not target_accounts:
        msg = f"▲ 映射表未匹配到標識 [<b>{target_id}</b>] 的參數塊！"
        logger.error(f"<SCHEDULR> 標識 {target_id} 索引缺失，越權被拒絕 [FAIL]")
        send_tg_msg(msg)
        return
    enqueue_task(action, target_accounts, "MANUAL")

def tunnel_health_check(account):
    url = account.get('tunnel_url')
    if not url: return
    if account.get('probe_paused'): return
    if system_busy_event.is_set(): return
        
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        status_code = res.status_code
    except Exception:
        status_code = 503
        
    node_name = get_node_name(account['id'])
    
    if 400 <= status_code < 500:
        logger.info(f" > NET_PING_ {node_name} 邊緣隧道心跳穩定 [❤ {status_code}] ... [ OK ]")
        if account['fail_count'] > 0 or account['auto_restart_count'] > 0:
            logger.info(f" < NET_RECV_ {node_name} 數據包重組成功，A.T.力場修復 [ OK ]")
            send_tg_msg(f"■ <b>鏈路連接恢復 ({node_name})</b>\n邊緣隧道心跳穩定 ﮩ٨ـﮩﮩ٨ـ♡ﮩ٨ـﮩﮩ٨ـ\n絕密代碼: <tg-spoiler>{account['email']}</tg-spoiler>")
        account['fail_count'] = 0
        account['auto_restart_count'] = 0
        
    elif 500 <= status_code < 600:
        account['fail_count'] += 1
        logger.warning(f" > NET_PING_ {node_name} 邊緣隧道發生丟包 [❤ {status_code}] ({account['fail_count']}/5)... [WARN]")
        
        if account['fail_count'] >= 5:
            if account['auto_restart_count'] >= 3:
                logger.error(f"[!!FATAL!!] {node_name} 連續 3 次硬重啟均超時，探針已掛起 [FAIL]")
                send_tg_msg(f"▲ <b>⚠️ 探針同步率下降至 0% ({node_name})</b>\nA.T.力場（隧道鏈路）已崩潰！該節點已被系統強制掛起。\n絕密代碼: <tg-spoiler>{account['email']}</tg-spoiler>")
                account['probe_paused'] = True
                account['fail_count'] = 0
                return
                
            account['auto_restart_count'] += 1
            account['fail_count'] = 0
            logger.error(f"<SYS_CRIT> {node_name} 丟包率越界，啟暴走模式，拉起強制重置序列 ({account['auto_restart_count']}/3)...")
            send_tg_msg(f"▲ <b>🚨 網絡劣化告警 ({node_name})</b>\n連續 5 次心跳失敗，啟動暴走模式(Berserk)，強制拉起系統重置序列 ({account['auto_restart_count']}/3)...")
            enqueue_task("RESTART", [account], "PROBE")

def clean_probe_logs():
    try:
        filtered_logs = [log for log in list(log_queue) if "NET_PING_" not in log]
        log_queue.clear()
        log_queue.extend(filtered_logs)
        logger.info("<MEM_SWEEP> 常規網絡嗅探冗餘日志已從內存堆棧剝離 [ OK ]")
    except Exception as e:
        logger.error(f"<SYS_ERR_> 垃圾回收機制陷入死鎖: {str(e)} [FAIL]")

# ==========================================
# 6. Telegram Bot ChatOps
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        help_text = (
            "► <b>NERV_MAINFRAME</b>\n\n"
            "--------- 主機集群控制終端 ---------\n"
            "❖ /status   ( 節點運行狀態追蹤 )\n"
            "❖ /stop     ( 強制釋放計算資源 )\n"
            "❖ /start    ( 喚醒掛起算力容器 )\n"
            "❖ /restart  ( 硬重置數據流鏈路 )\n\n"
            "<i>高維參數: 指令+編號 (例: /start 1)</i>"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not check_tg_auth(message): return
        parts = message.text.split()
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        
        if target_id and not target_accounts:
            bot.reply_to(message, f"▲ 映射表未能匹配到編號 <b>{target_id}</b> 的節點配置。", parse_mode="HTML")
            return

        bot.reply_to(message, f"⧗ 正在輪詢集群節點狀態...", parse_mode="HTML")
        
        def _check():
            sys_status = "■ 繁忙 (核心隊列阻塞中)" if system_busy_event.is_set() else "■ 空閒 (全局調度鎖釋放)"
            report = f"► <b>系統全局調度狀態</b>: {sys_status}\n\n"
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                node_name = get_node_name(acc['id'])
                report += f"👤 <b>節點: {node_name}</b> (<tg-spoiler>{acc['email']}</tg-spoiler>)\n"
                report += f"■ 容器物理態: <b>{status}</b>\n\n"
            bot.send_message(TG_CHAT_ID, report, parse_mode="HTML")
            
        threading.Thread(target=_check).start()

    @bot.message_handler(commands=['start', 'stop', 'restart'])
    def handle_actions(message):
        if not check_tg_auth(message): return
        parts = message.text.strip().split()
        command = parts[0].replace("/", "").upper()
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        bot_action_runner(command, target_id)

# ==========================================
# 7. Flask Web 守護服務
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SYS_ONLINE</title>
    <style>
        /* 復古賽博朋克 色彩與字體 */
        @import url('https://fonts.googleapis.com/css2?family=DotGothic16&family=VT323&display=swap');
        
        :root[data-theme="dark"] { 
            --bg-body: #0d1117; --bg-window: #010409; --bg-header: #161b22;
            --text-norm: #34d399; /* 熒光綠 */
            --text-muted: #4b5563; --border-col: #30363d;
            --input-bg: #000000; --toast-bg: #1f2937; --toast-text: #34d399;
            --cmd-bg: transparent; --cmd-col: #58a6ff; --cmd-border: #58a6ff; --cmd-hover: #1f6feb;
            --log-info: #34d399; --log-warn: #fbbf24; --log-err: #f87171;
            --shadow-window: 0 0 40px rgba(52, 211, 153, 0.1), 0 0 0 1px #30363d;
            --bloom: 0 0 3px rgba(52, 211, 153, 0.4); 
        }
        :root[data-theme="light"] { 
            --bg-body: #e5e7eb; --bg-window: #f6f8fa; --bg-header: #e1e4e8;
            --text-norm: #065f46; /* 暗黑綠 */
            --text-muted: #6e7781; --border-col: #d0d7de;
            --input-bg: #ffffff; --toast-bg: #24292f; --toast-text: #ffffff;
            --cmd-bg: transparent; --cmd-col: #0969da; --cmd-border: #0969da; --cmd-hover: #033d8b;
            --log-info: #065f46; --log-warn: #b45309; --log-err: #cf222e;
            --shadow-window: 0 15px 30px rgba(0,0,0,0.1), 0 0 0 1px #d0d7de;
            --bloom: none;
        }
        
        body { background: var(--bg-body); color: var(--text-norm); font-family: 'DotGothic16', 'VT323', monospace; margin: 0; height: 100vh; box-sizing: border-box; overflow: hidden; transition: background 0.3s ease; text-shadow: var(--bloom); font-size: 16px;}
        
        /* CRT Scanlines */
        body::after {
            content: " "; display: block; position: absolute; top: 0; left: 0; bottom: 0; right: 0;
            background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.1) 50%), linear-gradient(90deg, rgba(255, 0, 0, 0.03), rgba(0, 255, 0, 0.01), rgba(0, 0, 255, 0.03));
            z-index: 2; background-size: 100% 3px, 3px 100%; pointer-events: none;
        }
        
        #login-view, #app-view { 
            position: absolute; top: 0; left: 0; width: 100%; height: 100%; 
            display: flex; flex-direction: column; align-items: center; justify-content: center; 
            transition: opacity 0.4s ease, transform 0.4s cubic-bezier(0.16, 1, 0.3, 1); 
            box-sizing: border-box; padding: 4vh 5vw; z-index: 10;
        }
        .hidden { opacity: 0; pointer-events: none; z-index: 1; transform: scale(0.96); }
        .active { opacity: 1; pointer-events: auto; z-index: 10; transform: scale(1); }
        
        .mac-window { background: var(--bg-window); border-radius: 8px; box-shadow: var(--shadow-window); overflow: hidden; border: 1px solid var(--border-col); display: flex; flex-direction: column; width: 100%; position: relative; z-index: 20;}
        
        .mac-header { background: var(--bg-header); height: 26px; display: flex; justify-content: space-between; align-items: center; padding: 0 12px; border-bottom: 1px solid var(--border-col); user-select: none;}
        .mac-btns { display: flex; gap: 8px; width: 60px; align-items: center;}
        .mac-btn { width: 11px; height: 11px; border-radius: 50%; cursor: pointer; transition: filter 0.2s;}
        .mac-btn:hover { filter: brightness(0.8); }
        .btn-close { background: #ff5f56; } 
        .btn-min { background: #ffbd2e; }   
        .btn-max { background: #27c93f; cursor: default;} 
        
        .breathing { animation: blink-btn 2s infinite; }
        @keyframes blink-btn { 0%, 100% { box-shadow: 0 0 8px #27c93f; opacity: 1; } 50% { box-shadow: none; opacity: 0.5; } }
        
        /* 修改后：换成复古像素字体，放大字号，增加字间距 */
        .mac-title { font-size: 18px; font-weight: bold; color: var(--text-muted); letter-spacing: 2px; text-align: center; flex: 1; font-family: 'VT323', monospace;}
        .mac-spacer { width: 60px; } 

        #login-view .mac-window { width: 380px; height: auto; }
        .login-content { padding: 40px; text-align: center; }
        .login-content h2 { margin: 0 0 25px; font-size: 24px; color: var(--text-norm); font-weight: normal; letter-spacing: 2px;}
        .login-content input { width: 100%; padding: 12px; margin-bottom: 25px; background: var(--input-bg); border: 1px solid var(--border-col); border-radius: 4px; color: var(--text-norm); font-family: inherit; font-size: 18px; text-align: center; outline: none; box-sizing: border-box; text-shadow: var(--bloom);}
        .login-content input:focus { border-color: var(--text-norm); }
        .login-content button { width: 100%; padding: 12px; background: transparent; color: var(--text-norm); border: 1px solid var(--text-norm); border-radius: 4px; font-family: inherit; font-size: 18px; cursor: pointer; transition: 0.2s; text-shadow: var(--bloom);}
        .login-content button:hover { background: var(--text-norm); color: var(--bg-window); }
        .login-content button:disabled { opacity: 0.5; cursor: not-allowed; }

        #app-view .mac-window { flex: 1; max-width: 1400px; }
        
        /* 改成这样：把 1.3 改大 */
        #terminal-wrapper { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 5px 20px 0 20px; line-height: 1.4; word-wrap: break-word; }
        
        #boot-sequence { flex-shrink: 0; padding-right: 10px; }
        #live-logs { flex: 1; overflow-y: scroll; overflow-x: hidden; display: flex; flex-direction: column; }
       
        /* 改成这样：增加上下外边距 */
        .log-line { display: flex; justify-content: space-between; align-items: flex-start; margin: 2px 0; width: 100%; }
        
        /* 修改點：把 pre-wrap 精準加到了這裡 */
        .log-content { flex: 1; word-break: break-all; white-space: pre-wrap; }
        .log-badge { flex-shrink: 0; margin-left: 15px; font-family: 'VT323', monospace; font-size: 17px;}

        .INFO { color: var(--log-info); } .WARNING { color: var(--log-warn); } 
        
        /* Glitch Animation for ERROR */
        @keyframes glitch-anim {
            0% { transform: translate(0); text-shadow: none; }
            20% { transform: translate(-2px, 1px); text-shadow: 2px 0 rgba(255,0,0,0.8), -2px 0 rgba(0,0,255,0.8); }
            40% { transform: translate(-1px, -1px); text-shadow: none; }
            60% { transform: translate(2px, 1px); text-shadow: -2px 0 rgba(255,0,0,0.8), 2px 0 rgba(0,0,255,0.8); }
            80% { transform: translate(1px, -1px); text-shadow: none; }
            100% { transform: translate(0); text-shadow: none; }
        }
        .ERROR .log-content { animation: glitch-anim 0.3s ease-in-out; color: var(--log-err); font-weight: bold; }
        .ERROR .log-badge { color: var(--log-err); }
        
        /* 心電圖 (ECG) Pulse 熒光特效 */
        @keyframes heartbeat {
            0% { transform: scale(1); text-shadow: 0 0 5px var(--text-norm); }
            15% { transform: scale(1.3); text-shadow: 0 0 15px var(--text-norm); }
            30% { transform: scale(1); text-shadow: 0 0 5px var(--text-norm); }
            45% { transform: scale(1.15); text-shadow: 0 0 10px var(--text-norm); }
            60% { transform: scale(1); text-shadow: 0 0 5px var(--text-norm); }
            100% { transform: scale(1); text-shadow: 0 0 5px var(--text-norm); }
        }
        .heartbeat-anim { display: inline-block; animation: heartbeat 1.2s infinite; color: var(--text-norm); font-weight: bold; }
        .heartbeat-err { display: inline-block; animation: heartbeat 0.8s infinite; color: var(--log-err); text-shadow: 0 0 10px var(--log-err); font-weight: bold; }

        .inv-ok { background: var(--log-info); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-fail { background: var(--log-err); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-wait { background: var(--log-warn); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-warn { background: var(--log-warn); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        
        .cmd-clickable { color: var(--cmd-col); padding: 0 4px; cursor: pointer; border: 1px solid var(--cmd-border); margin: 0 2px; transition: 0.1s;}
        .cmd-clickable:hover { background: var(--cmd-col); color: var(--bg-window); text-shadow: none;}
        
        /* 修改點：縮減了 SYSTEM_READY 上下間距 */
        .sys-divider { display: flex; align-items: center; width: 100%; margin: 8px 0; color: var(--cmd-col); text-shadow: var(--bloom); opacity: 0.8;}
        .sys-divider .line { flex: 1; height: 1px; background-color: var(--cmd-col); box-shadow: var(--bloom); }
        .sys-divider .badge { padding: 0 15px; font-weight: bold; font-family: 'VT323', monospace; font-size: 18px; letter-spacing: 2px;}

        #typewriter-line { display: flex; align-items: center; min-height: 1.5em; width: 100%; padding-right: 10px;}
        #typewriter-text { white-space: pre-wrap; word-break: break-all; flex: 1;}
        .cursor { display: inline-block; width: 8px; height: 1em; background-color: var(--text-norm); margin-left: 2px; animation: cursor-blink 1s step-end infinite; box-shadow: var(--bloom);}
        @keyframes cursor-blink { 50% { opacity: 0; } }
        
        #input-area { background: var(--input-bg); padding: 15px 20px; display: flex; align-items: center; border-top: 1px solid var(--border-col); }
        #cmd-prefix { color: var(--cmd-col); margin-right: 12px; font-weight: bold; font-family: 'VT323', monospace; font-size: 18px;}
        #cmdInput { flex: 1; background: transparent; border: none; color: var(--text-norm); font-family: inherit; font-size: 17px; outline: none; text-shadow: var(--bloom);}
        #cmdInput::placeholder { color: var(--text-muted); text-shadow: none;}
        
        ::-webkit-scrollbar { width: 10px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: var(--border-col); border-radius: 0; } ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
        
        #toast { position: fixed; bottom: 80px; right: 5vw; background: var(--toast-bg); color: var(--toast-text); padding: 8px 16px; border: 1px solid var(--text-norm); opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000; box-shadow: var(--shadow-window); font-family: inherit; font-size: 16px;}
        #toast.show { opacity: 1; }
    </style>
</head>
<body>

    <div id="login-view" class="active">
        <div class="mac-window">
            <div class="mac-header">
                <div class="mac-btns">
                    <div class="mac-btn btn-close"></div>
                    <div class="mac-btn btn-min"></div>
                    <div class="mac-btn btn-max"></div>
                </div>
                <div class="mac-title">AUTH_GATEWAY</div>
                <div class="mac-spacer"></div>
            </div>
            <div class="login-content">
                <h2>SYS_CONSOLE</h2>
                <input type="password" id="loginPass" placeholder="INPUT ROOT TOKEN..." autocomplete="off" onkeypress="if(event.key==='Enter') doLogin()">
                <button id="loginBtn" onclick="doLogin()">[ ENTER ]</button>
            </div>
        </div>
    </div>

    <div id="app-view" class="hidden">
        <div class="mac-window">
            <div class="mac-header">
                <div class="mac-btns">
                    <div class="mac-btn btn-close" onclick="doLogout()" title="切斷連接"></div>
                    <div class="mac-btn btn-min" onclick="toggleTheme()" title="濾鏡切換"></div>
                    <div class="mac-btn btn-max breathing" title="系統內核運轉中"></div>
                </div>
                <div class="mac-title">SAP_BAS_KEEPALIVE_:</div>
                <div class="mac-spacer"></div>
            </div>
            
            <div id="terminal-wrapper">
                <div id="boot-sequence">
                    <div id="hex-dump-container"></div>
                    <div id="boot-log-container"></div>
                </div>
                <div id="live-logs"></div>
                <div id="typewriter-line"><span id="typewriter-text"></span><span class="cursor"></span></div>
            </div>
            
            <div id="input-area">
                <span id="cmd-prefix">ROOT@NERV_MAINFRAME_:</span>
                <input type="text" id="cmdInput" autocomplete="off" spellcheck="false" placeholder="AWAITING COMMAND (e.g., /sap, /start 1) ...">
            </div>
        </div>
    </div>

    <div id="toast">[OK] COMMAND COPIED</div>

    <script>
        const liveLogsDiv = document.getElementById('live-logs');
        const cmdInput = document.getElementById('cmdInput');
        const toast = document.getElementById('toast');
        const loginView = document.getElementById('login-view');
        const appView = document.getElementById('app-view');
        
        let autoScroll = true;
        let logInterval = null;

        let sessionToken = null;
        function setToken(val) { sessionToken = val; try { localStorage.setItem('bas_token', val); } catch(e) {} }
        function getToken() { if (sessionToken) return sessionToken; try { return localStorage.getItem('bas_token'); } catch(e) { return null; } }
        function clearToken() { sessionToken = null; try { localStorage.removeItem('bas_token'); } catch(e) {} }

        let bootLogsRendered = false;
        let lastLogCount = 0;
        let typeQueue = [];
        let isTyping = false;
        let hasAlert = false;
        let currentRunId = 0; 

        function initTheme() {
            try {
                let saved = localStorage.getItem('bas_theme');
                if (!saved) {
                    let h = new Date().getHours();
                    saved = (h >= 6 && h < 18) ? 'light' : 'dark';
                }
                document.documentElement.setAttribute('data-theme', saved);
            } catch (e) { document.documentElement.setAttribute('data-theme', 'dark'); }
        }

        function toggleTheme() {
            let current = document.documentElement.getAttribute('data-theme');
            let next = (current === 'light') ? 'dark' : 'light';
            try { localStorage.setItem('bas_theme', next); } catch (e) {}
            document.documentElement.setAttribute('data-theme', next);
        }

        function updateTitle(isError) {
            if (isError && !hasAlert) {
                hasAlert = true;
                document.title = '[🚨] 警告: 系統異常!';
                let link = document.querySelector("link[rel~='icon']");
                if (!link) { link = document.createElement('link'); link.rel = 'icon'; document.head.appendChild(link); }
                link.href = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🚨</text></svg>';
            } else if (!isError && !hasAlert) {
                document.title = 'SYS_ONLINE';
                let link = document.querySelector("link[rel~='icon']");
                if (!link) { link = document.createElement('link'); link.rel = 'icon'; document.head.appendChild(link); }
                link.href = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">📟</text></svg>';
            }
        }

        async function doLogin() {
            const pass = document.getElementById('loginPass').value.trim();
            if (!pass) return;
            
            const btn = document.getElementById('loginBtn');
            if (btn.disabled) return; 
            
            const origText = btn.innerText;
            btn.innerText = '[ VERIFYING... ]';
            btn.disabled = true;
            
            try {
                const res = await fetch('api/verify', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: pass })
                });
                
                if (res.status === 200) {
                    setToken(pass);
                    enterSystem();
                    btn.innerText = origText;
                } else {
                    btn.innerText = '[ CLEARANCE DENIED ]';
                    btn.style.color = 'var(--log-err)';
                    btn.style.borderColor = 'var(--log-err)';
                    setTimeout(() => { btn.innerText = origText; btn.style.color = ''; btn.style.borderColor = ''; }, 2000);
                }
            } catch(e) { 
                btn.innerText = '[ NET_PULSE_ERR ]';
                btn.style.color = 'var(--log-warn)';
                btn.style.borderColor = 'var(--log-warn)';
                setTimeout(() => { btn.innerText = origText; btn.style.color = ''; btn.style.borderColor = ''; }, 2000);
            } finally {
                btn.disabled = false;
            }
        }

        function doLogout() {
            currentRunId++; 
            clearToken();
            clearInterval(logInterval);
            appView.className = 'hidden';
            loginView.className = 'active';
            document.getElementById('loginPass').value = '';
            hasAlert = false;
            updateTitle(false);
        }

        async function playHexDump(runId) {
            const hexLines = [
                "[0x00000000] BOOTSTRAP KERNEL... [ OK ]",
                "[0x001B4F3A] ALLOCATING NEURAL NETWORK RESOURCES... [ OK ]",
                "[0x003C8A11] CHECKING A.T. FIELD INTEGRITY... [ OK ]",
                "[0x008F11B2] INITIALIZING PLUG SUIT INTERFACE... [ OK ]",
                "[0x00A1FF23] ESTABLISHING CONNECTION TO MAINFRAME... [ OK ]"
            ];
            const hexCont = document.getElementById('hex-dump-container');
            for (let line of hexLines) {
                if (runId !== currentRunId) return; 
                hexCont.innerHTML += `<div class="log-line INFO"><div class="log-content">${line}</div></div>`;
                if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;
                await new Promise(r => setTimeout(r, 120));
            }
            if (runId === currentRunId) await new Promise(r => setTimeout(r, 200));
        }

        async function enterSystem() {
            currentRunId++;
            isTyping = false;
            typeQueue = [];
            lastLogCount = 0;
            bootLogsRendered = false;
            hasAlert = false;
            
            loginView.className = 'hidden';
            appView.className = 'active';
            
            document.getElementById('hex-dump-container').innerHTML = '';
            document.getElementById('boot-log-container').innerHTML = '';
            liveLogsDiv.innerHTML = '';
            document.getElementById('typewriter-text').textContent = '';
            
            await playHexDump(currentRunId);
            if (currentRunId !== currentRunId) return; 
            
            fetchLogs();
            logInterval = setInterval(fetchLogs, 1500); 
            cmdInput.focus();
        }

        liveLogsDiv.addEventListener('scroll', () => { 
            autoScroll = liveLogsDiv.scrollHeight - liveLogsDiv.scrollTop <= liveLogsDiv.clientHeight + 10; 
        });

        window.copyToInput = function(cmdText) {
            cmdInput.value = cmdText + ' ';
            cmdInput.focus();
            navigator.clipboard.writeText(cmdText).catch(err => {});
            toast.innerText = `[OK] LOADED: ${cmdText}`;
            toast.style.borderColor = 'var(--text-norm)';
            toast.style.color = 'var(--toast-text)';
            toast.className = 'show';
            setTimeout(() => { toast.className = ''; }, 2000);
        }

        async function fetchLogs() {
            const token = getToken();
            if(!token) return doLogout();
            try {
                const res = await fetch('api/logs', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: token })
                });
                if (res.status === 401) return doLogout();
                const data = await res.json();
                processLogStream(data.logs);
            } catch (e) {}
        }

        function processLogStream(logs) {
            let splitIndex = logs.findIndex(l => l.includes("終端面甲激活完成，全系統就緒"));
            if(splitIndex === -1) splitIndex = -1; 

            if (!bootLogsRendered && splitIndex !== -1) {
                let bootHtml = logs.slice(0, splitIndex + 1).map(formatLogHTML).join('');
                bootHtml += '<div class="sys-divider"><div class="line"></div><div class="badge">[ SYSTEM_READY ]</div><div class="line"></div></div>';
                document.getElementById('boot-log-container').innerHTML = bootHtml;
                bootLogsRendered = true;
                lastLogCount = splitIndex + 1;
            } else if (!bootLogsRendered) {
                lastLogCount = 0; 
            }
            
            if (logs.length < lastLogCount) {
                lastLogCount = splitIndex !== -1 ? splitIndex + 1 : 0;
                liveLogsDiv.innerHTML = ''; 
                return;
            }
            
            let newLogs = logs.slice(lastLogCount);
            if (newLogs.length > 0) {
                for(let line of newLogs) typeQueue.push(line);
                lastLogCount = logs.length;
                runTypewriter();
            }
        }

        function formatLogHTML(log) {
            let cls = 'INFO';
            if (log.includes('[WARN]')) cls = 'WARNING';
            if (log.includes('[FAIL]') || log.includes('[!!FATAL!!]')) {
                cls = 'ERROR';
                updateTitle(true);
            }
            
            let badgeHtml = '';
            let contentHtml = log;
            
            let badgeRegex = /\[\s*(OK|FAIL|WAIT|WARN)\s*\]/;
            let match = log.match(badgeRegex);
            if (match) {
                 let type = match[1];
                 let badgeCls = 'inv-' + type.toLowerCase();
                 let displayType = type === 'OK' ? ' OK ' : type;
                 badgeHtml = `<div class="log-badge"><span class="${badgeCls}">[${displayType}]</span></div>`;
                 contentHtml = log.replace(badgeRegex, '').trim();
            }
            
            if (contentHtml.includes('[❤ ')) {
                contentHtml = contentHtml.replace(/\[❤ (\d+)\]/g, (m, p1) => {
                    let hbClass = parseInt(p1) >= 500 ? 'heartbeat-err' : 'heartbeat-anim';
                    return `<span class="${hbClass}">[❤ ${p1}]</span>`;
                });
            }
            
            contentHtml = contentHtml.replace(/(\/(?:status|stop|start|restart|sap)\\b)/g, 
                    '<span class="cmd-clickable" onclick="copyToInput(&quot;$1&quot;)">$1</span>');
                    
            return `<div class="log-line ${cls}"><div class="log-content">${contentHtml}</div>${badgeHtml}</div>`;
        }

        function runTypewriter() {
            if (isTyping || typeQueue.length === 0) return;
            isTyping = true;
            
            let line = typeQueue.shift();
            let typeSpan = document.getElementById('typewriter-text');
            
            if(typeQueue.length > 3) {
                typeSpan.innerHTML = '';
                liveLogsDiv.insertAdjacentHTML('beforeend', formatLogHTML(line));
                while(typeQueue.length > 0) {
                    liveLogsDiv.insertAdjacentHTML('beforeend', formatLogHTML(typeQueue.shift()));
                }
                if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;
                isTyping = false;
                return;
            }
            
            let index = 0;
            let runId = currentRunId; 
            
            function typeChar() {
                if (runId !== currentRunId) return; 
                
                if(index < line.length) {
                    typeSpan.textContent += line.charAt(index);
                    index++;
                    if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;
                    setTimeout(typeChar, 8); 
                } else {
                    typeSpan.textContent = '';
                    liveLogsDiv.insertAdjacentHTML('beforeend', formatLogHTML(line));
                    if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;
                    isTyping = false;
                    runTypewriter();
                }
            }
            typeChar();
        }

        cmdInput.addEventListener('keypress', async function (e) {
            if (e.key === 'Enter') {
                const cmd = cmdInput.value.trim();
                const token = getToken();
                if (!cmd || !token) return;
                
                const fakeLog = document.createElement('div');
                fakeLog.className = 'log-line INFO';
                fakeLog.innerHTML = `<div class="log-content">[${new Date().toISOString().slice(0,19).replace('T', ' ')}] &gt; COMMAND INPUT: <span class="cmd-clickable">${cmd}</span></div>`;
                fakeLog.style.color = 'var(--cmd-col)';
                liveLogsDiv.appendChild(fakeLog);
                if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;

                cmdInput.value = '';
                
                try {
                    const res = await fetch(`api/command`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ token: token, command: cmd })
                    });
                    if (res.status === 401) doLogout();
                } catch (err) {}
            }
        });

        cmdInput.addEventListener('focus', () => { 
            hasAlert = false; 
            updateTitle(false); 
        });

        initTheme();
        updateTitle(false);
        if (getToken()) enterSystem();
    </script>
</body>
</html>
"""

# ==========================================
# 8. Flask 後端路由
# ==========================================

@app.route('/')
def index():
    response = make_response(HTML_TEMPLATE)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/api/verify', methods=['POST'])
def verify_token():
    data = request.get_json()
    if data and data.get("token") == WEB_TOKEN:
        return jsonify({"status": "OK"}), 200
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/api/logs', methods=['POST'])
def api_logs():
    data = request.get_json()
    if not data or data.get("token") != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"logs": list(log_queue)})

@app.route('/api/command', methods=['POST'])
def web_command():
    data = request.get_json()
    if not data or data.get("token") != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    
    cmd_str = data.get("command", "").strip()
    if not cmd_str.startswith("/"):
        logger.warning(f"<HUD_UI> 攔截非法語法流: {cmd_str} (須以 / 起始) [WARN]")
        return jsonify({"error": "Invalid command format"}), 400
        
    parts = cmd_str.split()
    command = parts[0].replace("/", "").lower()
    target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    
    logger.info(f"<HUD_UI> 權限提權寫入(OVERRIDE): {cmd_str} [ OK ]")
    
    if command in ['start', 'stop', 'restart']:
        bot_action_runner(command.upper(), target_id)
        return jsonify({"status": "Command dispatched to queue"})
        
    elif command == 'status':
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        if target_id and not target_accounts:
            logger.error(f"<HUD_UI> 數據庫未能匹配到編號 {target_id} 的容器節點 [FAIL]")
            return jsonify({"status": "Not found"})
        
        def _check_web():
            sys_status = "■ 紅色警戒 (強制指令阻塞中)" if system_busy_event.is_set() else "■ 綠色安定 (全局鎖已釋放)"
            logger.info(f"<SYS_OP> 集群算力容器狀態追蹤: {sys_status}")
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                node_name = get_node_name(acc['id'])
                logger.info(f"<SYS_OP> 節點 {node_name} ({acc['email']}) -> 容器物理態: {status}")
                
        threading.Thread(target=_check_web).start()
        return jsonify({"status": "Checking status"})
        
    elif command == 'sap':
        logger.info("--------- 主機集群控制終端 ---------")
        logger.info("❖ /status   ( 節點運行狀態追蹤 )")
        logger.info("❖ /stop     ( 強制釋放計算資源 )")
        logger.info("❖ /start    ( 喚醒掛起算力容器 )")
        logger.info("❖ /restart  ( 硬重置數據流鏈路 )")
        return jsonify({"status": "Help displayed"})
    
    else:
        logger.warning(f"<HUD_UI> 濾除未知戰術指令: {cmd_str} [WARN]")
        return jsonify({"error": "Unknown command"}), 400

# ==========================================
# 9. 啓動引導區
# ==========================================
def start_bot_polling():
    logger.info("<SYS_INIT> 外部系統通訊網絡連線補完。 [ OK ]")
    bot.infinity_polling()

if __name__ == '__main__':
    logger.info(f"<SYS_INIT> 核心調度模塊啓動！成功掛載【{get_node_count_str(len(ACCOUNTS))}】個節點參數。 [ OK ]")
    
    if not ACCOUNTS:
        logger.error("[!!FATAL!!] 核心節點參數缺失，系統拋出異常並自我鎖定！ [FAIL]")
        sys.exit(1)
        
    scheduler = BackgroundScheduler()
    for acc in ACCOUNTS:
        node_name = get_node_name(acc['id'])
        scheduler.add_job(lambda a=acc: async_task_runner("KEEPALIVE", a), trigger='cron', minute=acc['joba_min'], id=f"job_keepalive_{acc['id']}")
        scheduler.add_job(lambda a=acc: async_task_runner("RESTART", a), trigger='cron', hour=acc['jobb_hrs'], minute=acc['jobb_min'], id=f"job_restart_{acc['id']}")
        
        reboot_str = format_reboot_times(acc['jobb_hrs'], acc['jobb_min'])
        if acc.get('tunnel_url'):
            scheduler.add_job(lambda a=acc: tunnel_health_check(a), trigger='interval', minutes=1, id=f"job_health_{acc['id']}")
            logger.info(f"<SCHEDULR> {node_name} 守護進程注入【KEEP_ALIVE:{acc['joba_min']}M/H | REBOOT:{reboot_str} | ARGO:ON】 [ OK ]")
        else:
            logger.info(f"<SCHEDULR> {node_name} 守護進程注入【KEEP_ALIVE:{acc['joba_min']}M/H | REBOOT:{reboot_str} | ARGO:OFF】 [ OK ]")

    scheduler.add_job(clean_probe_logs, trigger='interval', hours=1, id='job_clean_logs')

    scheduler.start()

    if bot:
        threading.Thread(target=start_bot_polling, daemon=True).start()

    logger.info("<HUD_UI> 終端面甲激活完成，全系統就緒。 [ OK ]")
    
    for acc in ACCOUNTS:
        if acc.get('tunnel_url'):
            threading.Thread(target=tunnel_health_check, args=(acc,)).start()

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
