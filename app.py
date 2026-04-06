import os
import sys
import time
import threading
import queue
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, jsonify, request

from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 核心配置与多账号动态加载
# ==========================================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
WEB_TOKEN = os.environ.get("WEB_TOKEN", "default_token")
PORT = int(os.environ.get("PORT", 8080))

# URL 自动格式化函数
def format_url(url_str):
    if not url_str: return None
    url_str = url_str.strip()
    if url_str.startswith("https://"): url_str = url_str[8:]
    elif url_str.startswith("http://"): url_str = url_str[7:]
    return f"https://{url_str}"

# 动态加载所有配置了 SAP_EMAIL_X 的账号
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

# ==========================================
# 2. 极客级内存日志系统
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
# 3. 核心通讯组件
# ==========================================
bot = telebot.TeleBot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

def check_tg_auth(message):
    return str(message.chat.id) == TG_CHAT_ID

def send_tg_msg(text):
    if bot and TG_CHAT_ID:
        try:
            bot.send_message(TG_CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"TG 通知发送失败: {str(e)}")

def send_tg_photo(photo_path, caption=""):
    if bot and TG_CHAT_ID and os.path.exists(photo_path):
        try:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(TG_CHAT_ID, photo, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"TG 图片发送失败: {str(e)}")

# ==========================================
# 4. 业务逻辑层 (Playwright 操作 BAS)
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
        region_url = account['region_url']
        email = account['email']
        password = account['password']
        
        logger.info(f"🚀 开始执行任务: [{action_type}] (账号 {acc_id})")
        work_dir = "/tmp"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1920, 'height': 1080})
                page = context.new_page()
                api_request = context.request

                try:
                    logger.info(f"[-] 账号 {acc_id} 正在模拟登录...")
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
                    
                    logger.info(f"[+] 账号 {acc_id} 登录成功，正在调用API...")
                    req_headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    ws_api_url = f"{region_url}/ws-manager/api/v1/workspace"
                    workspaces = api_request.get(ws_api_url, headers=req_headers).json()
                    
                    if not workspaces:
                        logger.error(f"账号 {acc_id} 未找到任何工作区")
                        return False
                        
                    ws = workspaces[0]
                    ws_uuid = ws.get("id") or ws.get("config", {}).get("id")
                    username = ws.get("config", {}).get("username", "")
                    display_name = ws.get("config", {}).get("labels", {}).get("ws-manager.devx.sap.com/displayname", ws_uuid)
                    status = ws.get("runtime", {}).get("status")
                    
                    if action_type == "STOP" and status == "STOPPED":
                        msg = f"ℹ️ <b>操作跳过 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 当前已经是 <b>STOPPED</b> 状态，无需重复停止。"
                        send_tg_msg(msg)
                        logger.info(f"[-] 账号 {acc_id} 状态已是 STOPPED，无需重复停止。")
                        
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type == "START" and status == "RUNNING":
                        msg = f"ℹ️ <b>操作跳过 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 当前已经是 <b>RUNNING</b> 状态，无需重复启动。\n💡 <i>提示：若ARGO隧道不通，请使用 /restart 进行深度重置。</i>"
                        send_tg_msg(msg)
                        logger.info(f"[-] 账号 {acc_id} 状态已是 RUNNING，无需重复启动。")
                        
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
                            logger.info(f"[-] 账号 {acc_id} 状态轮询: 期望={target_status}, 当前={curr_status}")
                            if curr_status == target_status:
                                return True
                        return False

                    if action_type in ["RESTART", "STOP"] and status == "RUNNING":
                        logger.info(f"[*] 账号 {acc_id} 正在执行停止操作...")
                        if set_status(True, "STOPPED"):
                            logger.info(f"[+] 账号 {acc_id} 工作区已停止")
                            status = "STOPPED"
                    
                    if action_type == "STOP":
                        msg = f"🔴 <b>SAP BAS {action_type} 任务完成 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 已停止服务！"
                        send_tg_msg(msg)
                        logger.info(f"[+] 账号 {acc_id} STOP 任务结束，已挂起探针，发送TG通知。")
                        
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type in ["START", "RESTART", "KEEPALIVE"] and status in ["STOPPED", "STARTING", "RUNNING"]:
                        if status == "STOPPED":
                            logger.info(f"[*] 账号 {acc_id} 正在执行启动操作...")
                            if not set_status(False, "RUNNING"):
                                logger.error(f"[!] 账号 {acc_id} 启动超时")
                                return False
                                
                        logger.info(f"[*] 账号 {acc_id} 开始进行 UI 穿透...")
                        page.goto(f"{region_url}/index.html")
                        time.sleep(8)
                        
                        ws_frame = page.frame_locator("iframe#ws-manager")
                        ws_link = ws_frame.locator(f"a[href*='{ws_uuid}']").first
                        ws_link.wait_for(state="visible", timeout=20000)
                        ws_link.click(force=True)
                        logger.info(f"[-] 账号 {acc_id} 正在加载 IDE (等待30秒)...")
                        time.sleep(30)
                        
                        logger.info(f"[-] 账号 {acc_id} 执行弹窗清理策略...")
                        for _ in range(3):
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                                
                        screenshot_path = f"{work_dir}/capture_{acc_id}_{ws_uuid}.png"
                        page.screenshot(path=screenshot_path)
                        if action_type != "KEEPALIVE":
                            send_tg_photo(screenshot_path, f"🎯 <b>SAP BAS {action_type} 任务完成 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 已唤醒服务！")
                        logger.info(f"[+] 🎯 账号 {acc_id} [{action_type}] 任务成功。")
                        
                        account['probe_paused'] = False
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                except Exception as inner_e:
                    logger.error(f"[!] 账号 {acc_id} 严重异常: {str(inner_e)}")
                    try:
                        error_shot = f"{work_dir}/error_crash_{acc_id}_{action_type}.png"
                        page.screenshot(path=error_shot)
                        send_tg_photo(error_shot, f"❌ <b>执行 [{action_type}] 发生异常 (账号 {acc_id})</b>\n请查看BAS实时截图排查问题。\n报错: <code>{str(inner_e)}</code>")
                    except Exception as pic_e:
                        logger.error(f"保存崩溃截图失败: {pic_e}")
                    return False
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[!] 浏览器环境拉起失败: {str(e)}")
            return False

# ==========================================
# 5. 全局排队调度中心与隧道探针
# ==========================================

def enqueue_task(action, target_accounts, source):
    task_queue.put({
        "action": action,
        "accounts": target_accounts,
        "source": source
    })
    if source == "MANUAL":
        acc_str = f"账号 {target_accounts[0]['id']}" if len(target_accounts) == 1 else f"全部 {len(target_accounts)} 个账号"
        msg = f"✅ 已加入排队系统：即将为 <b>{acc_str}</b> 依次执行 <b>{action}</b>..."
        logger.info(msg.replace("<b>", "").replace("</b>", ""))
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
            logger.error(f"Worker 执行流水线异常: {e}")
        finally:
            system_busy_event.clear()
            if source == "MANUAL":
                finish_msg = "🎉 <b>后台任务已全部执行完毕，系统资源已释放，可以下发新的指令。</b>"
                logger.info(finish_msg.replace("<b>", "").replace("</b>", ""))
                send_tg_msg(finish_msg)
            task_queue.task_done()

threading.Thread(target=global_task_worker, daemon=True).start()

def async_task_runner(action, account):
    enqueue_task(action, [account], "CRON")

def bot_action_runner(action, target_id=None):
    target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
    if not target_accounts:
        msg = f"❌ 未找到 ID 为 <b>{target_id}</b> 的账号配置！"
        logger.error(msg.replace("<b>", "").replace("</b>", ""))
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
        
    acc_id = account['id']
    
    if 400 <= status_code < 500:
        logger.info(f"[-] 隧道探针: 账号 {acc_id} (状态码: {status_code}) -> 🟢 隧道在线")
        if account['fail_count'] > 0 or account['auto_restart_count'] > 0:
            logger.info(f"[+] 隧道恢复：账号 {acc_id} 警报解除，重置所有计数器。")
            send_tg_msg(f"✅ <b>隧道已恢复在线 (账号 {acc_id})</b>\n探针连通测试成功，重置错误计数器。")
        account['fail_count'] = 0
        account['auto_restart_count'] = 0
        
    elif 500 <= status_code < 600:
        account['fail_count'] += 1
        logger.warning(f"[!] 隧道探针: 账号 {acc_id} (状态码: {status_code}) -> 🔴 隧道离线 ({account['fail_count']}/5)")
        
        if account['fail_count'] >= 5:
            if account['auto_restart_count'] >= 3:
                logger.error(f"🚨 [放弃重置] 账号 {acc_id} 连续 3 次重启后隧道依然离线，已暂停自动重启。")
                send_tg_msg(f"🚨 <b>隧道严重故障 (账号 {acc_id})</b>\n已连续自动重启 3 次，隧道依然处于离线状态。探针及自动重启功能已被挂起，请手动登录 SAP BTP 网页端排查原因！")
                account['probe_paused'] = True
                account['fail_count'] = 0
                return
                
            account['auto_restart_count'] += 1
            account['fail_count'] = 0
            
            logger.error(f"🚨 [紧急重置] 账号 {acc_id} 隧道连续 5 次心跳失败，触发自动重启 ({account['auto_restart_count']}/3)...")
            send_tg_msg(f"🚨 <b>隧道掉线警报 (账号 {acc_id})</b>\n隧道连续 5 次心跳失败，触发自动重启 ({account['auto_restart_count']}/3)...")
            
            enqueue_task("RESTART", [account], "PROBE")

def clean_probe_logs():
    try:
        filtered_logs = [log for log in list(log_queue) if "隧道探针" not in log]
        log_queue.clear()
        log_queue.extend(filtered_logs)
        logger.info("[*] 🧹 内存清理: 已自动清空过去 1 小时内的隧道探针常规日志，保持面板极简。")
    except Exception as e:
        logger.error(f"[!] 探针日志清理失败: {str(e)}")

# ==========================================
# 6. Telegram Bot ChatOps
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        help_text = (
            "🤖 <b>SAP BAS KEEPALIVE 机器人</b>\n\n"
            "-------- 可用命令 --------\n\n"
            "🔹 /status   ( 查询 BAS )\n"
            "🔹 /stop     ( 停止 BAS )\n"
            "🔹 /start    ( 启动 BAS )\n"
            "🔹 /restart  ( 重启 BAS )\n\n"
            "💡 <i>提示：加上数字 ID (如 /start 1) 可精准控制单个账号，不加则控制所有账号。</i>"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not check_tg_auth(message): return
        
        parts = message.text.split()
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        
        if target_id and not target_accounts:
            bot.reply_to(message, f"❌ 未找到 ID 为 {target_id} 的账号。", parse_mode="HTML")
            return

        bot.reply_to(message, f"⏳ 正在查询 {len(target_accounts)} 个账号的状态，请稍候...", parse_mode="HTML")
        
        def _check():
            sys_status = "🔴 繁忙 (执行中)" if system_busy_event.is_set() else "🟢 空闲"
            report = f"📊 <b>后台排队/运行状态</b>: {sys_status}\n\n"
            
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                probe_status = "⏸️ 已挂起" if acc['probe_paused'] else f"🔄 运行中 (重置:{acc['auto_restart_count']}/3)"
                report += f"👤 <b>账号 {acc['id']}</b> ({acc['email']})\n"
                report += f"☁️ 容器状态: <b>{status}</b>\n"
                report += f"🛡️ 探针状态: {probe_status}\n\n"
            
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
# 7. Flask Web 守护服务 (SPA单页登录 + 明暗主题)
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAP BAS KEEPALIVE 云中控</title>
    <style>
        /* 极致 CSS 变量：明暗双主题无缝切换 */
        :root[data-theme="dark"] { 
            --main-bg: #050505; --term-bg: #0a0a0a; --head-bg: #1a1a1a; 
            --border-col: #333; --text-norm: #00ff41; --text-title: #ccc; 
            --input-bg: #111; --input-text: #f8f8f2; --btn-hover: #6272a4;
            --shadow-term: 0 10px 30px rgba(0,0,0,0.8), 0 0 0 1px #333;
            --login-card: rgba(20, 20, 20, 0.8);
            --toast-bg: #282a36; --toast-text: #50fa7b;
            --log-info: #00ff41; --log-warn: #ffb86c; --log-err: #ff5555;
            --cmd-bg: #282a36; --cmd-col: #8be9fd; --cmd-bord: #6272a4;
        }
        :root[data-theme="light"] { 
            --main-bg: #e9ecef; --term-bg: #ffffff; --head-bg: #f5f5f7; 
            --border-col: #d1d5db; --text-norm: #115926; --text-title: #333; 
            --input-bg: #f3f4f6; --input-text: #1f2937; --btn-hover: #4b5563;
            --shadow-term: 0 10px 30px rgba(0,0,0,0.1), 0 0 0 1px #e5e7eb;
            --login-card: rgba(255, 255, 255, 0.9);
            --toast-bg: #333; --toast-text: #fff;
            --log-info: #006600; --log-warn: #b37400; --log-err: #d32f2f;
            --cmd-bg: #e5e7eb; --cmd-col: #2563eb; --cmd-bord: #93c5fd;
        }
        
        body { background: var(--main-bg); color: var(--text-norm); font-family: 'Consolas', 'Fira Code', monospace; margin: 0; height: 100vh; box-sizing: border-box; overflow: hidden; transition: background 0.4s ease; display: flex; align-items: center; justify-content: center;}
        
        /* 视图切换逻辑 */
        #login-view, #terminal-view { width: 100%; height: 100%; transition: opacity 0.5s ease; position: absolute; top: 0; left: 0; }
        .hidden { opacity: 0; pointer-events: none; z-index: -1; }
        .active { opacity: 1; pointer-events: auto; z-index: 10; display: flex; flex-direction: column; padding: 2vh 5vw; box-sizing: border-box;}
        
        /* 登录卡片 */
        #login-view { display: flex; align-items: center; justify-content: center; }
        .login-box { background: var(--login-card); backdrop-filter: blur(10px); padding: 40px; border-radius: 12px; box-shadow: var(--shadow-term); text-align: center; border: 1px solid var(--border-col); transition: all 0.4s; }
        .login-box h2 { margin-top: 0; color: var(--text-title); font-family: sans-serif; }
        .login-box input { width: 80%; padding: 12px; margin: 20px 0; border: 1px solid var(--border-col); border-radius: 6px; background: var(--input-bg); color: var(--input-text); outline: none; font-size: 16px; text-align: center;}
        .login-box button { width: 85%; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; transition: 0.2s; }
        .login-box button:hover { background: #1d4ed8; }

        /* 终端容器 */
        .terminal-container { flex: 1; display: flex; flex-direction: column; background: var(--term-bg); border-radius: 10px; box-shadow: var(--shadow-term); overflow: hidden; margin-bottom: 10px; transition: background 0.4s, box-shadow 0.4s; }
        .header { background: var(--head-bg); padding: 12px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-col); transition: background 0.4s; }
        
        /* 顶部动作组 */
        .mac-btns { display: flex; gap: 8px; width: 60px; }
        .mac-btn { width: 12px; height: 12px; border-radius: 50%; }
        .btn-close { background: #ff5f56; } .btn-min { background: #ffbd2e; } .btn-max { background: #27c93f; }
        .title { font-weight: bold; color: var(--text-title); font-size: 0.95rem; letter-spacing: 1px; flex: 1; text-align: center; }
        
        .header-actions { display: flex; align-items: center; gap: 15px; width: 120px; justify-content: flex-end; }
        .status-indicator { font-size: 0.8rem; color: var(--text-norm); display: flex; align-items: center; gap: 6px; }
        .dot { width: 8px; height: 8px; background: #00ff41; border-radius: 50%; box-shadow: 0 0 8px #00ff41; animation: blink 2s infinite; }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        
        /* SVG 图标按钮 */
        .icon-btn { background: none; border: none; color: var(--text-title); cursor: pointer; padding: 0; display: flex; align-items: center; transition: color 0.2s; }
        .icon-btn:hover { color: #2563eb; }
        .icon-btn svg { width: 18px; height: 18px; }

        /* 日志输出区 */
        #terminal { flex: 1; overflow-y: auto; padding: 20px; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; font-size: 14px; }
        .log-line { margin: 2px 0; }
        .INFO { color: var(--log-info); } .WARNING { color: var(--log-warn); } .ERROR { color: var(--log-err); }
        
        /* 可点击的命令按钮 */
        .cmd-clickable { color: var(--cmd-col); background: var(--cmd-bg); padding: 1px 6px; border-radius: 4px; cursor: pointer; transition: all 0.2s ease; border: 1px solid var(--cmd-bord); margin: 0 2px; font-weight: bold;}
        .cmd-clickable:hover { background: var(--btn-hover); color: #fff; border-color: transparent;}
        
        /* 底部输入框 */
        #input-area { background: var(--input-bg); padding: 15px 20px; display: flex; align-items: center; border-radius: 10px; box-shadow: var(--shadow-term); border: 1px solid var(--border-col); transition: background 0.4s; }
        #cmd-prefix { color: #ff79c6; margin-right: 12px; font-weight: bold; font-size: 15px;}
        #cmdInput { flex: 1; background: transparent; border: none; color: var(--input-text); font-family: inherit; font-size: 15px; outline: none; }
        
        ::-webkit-scrollbar { width: 10px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: #888; border-radius: 5px; } ::-webkit-scrollbar-thumb:hover { background: #555; }
        #toast { position: fixed; bottom: 80px; right: 5vw; background: var(--toast-bg); color: var(--toast-text); padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity 0.3s ease; pointer-events: none; z-index: 1000; box-shadow: 0 4px 15px rgba(0,0,0,0.4); font-weight: bold; font-family: sans-serif; }
    </style>
</head>
<body>

    <div id="login-view" class="active">
        <div class="login-box">
            <h2>SAP BAS KEEPALIVE</h2>
            <input type="password" id="loginPass" placeholder="输入管理员密码" onkeypress="if(event.key==='Enter') doLogin()">
            <br>
            <button onclick="doLogin()">进入终端</button>
        </div>
    </div>

    <div id="terminal-view" class="hidden">
        <div class="terminal-container">
            <div class="header">
                <div class="mac-btns">
                    <div class="mac-btn btn-close"></div><div class="mac-btn btn-min"></div><div class="mac-btn btn-max"></div>
                </div>
                <div class="title">SAP BAS KEEPALIVE</div>
                <div class="header-actions">
                    <span class="status-indicator"><div class="dot"></div>实时</span>
                    <button class="icon-btn" onclick="toggleTheme()" title="切换主题">
                        <svg id="theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></svg>
                    </button>
                    <button class="icon-btn" onclick="doLogout()" title="退出系统">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
                            <polyline points="16 17 21 12 16 7"></polyline>
                            <line x1="21" y1="12" x2="9" y2="12"></line>
                        </svg>
                    </button>
                </div>
            </div>
            <div id="terminal"></div>
        </div>
        
        <div id="input-area">
            <span id="cmd-prefix">root@bas:~#</span>
            <input type="text" id="cmdInput" autocomplete="off" spellcheck="false" placeholder="输入指令 或 点击上方蓝字快捷复制">
        </div>
    </div>

    <div id="toast">已静默复制指令 🚀</div>

    <script>
        const terminal = document.getElementById('terminal');
        const cmdInput = document.getElementById('cmdInput');
        const toast = document.getElementById('toast');
        const loginView = document.getElementById('login-view');
        const terminalView = document.getElementById('terminal-view');
        
        let autoScroll = true;
        let logInterval = null;

        // === 主题管理引擎 ===
        const iconMoon = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>';
        const iconSun = '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>';

        function initTheme() {
            let saved = localStorage.getItem('bas_theme');
            if (!saved) {
                let h = new Date().getHours();
                saved = (h >= 6 && h < 18) ? 'light' : 'dark';
            }
            applyTheme(saved);
        }

        function applyTheme(themeName) {
            document.documentElement.setAttribute('data-theme', themeName);
            document.getElementById('theme-icon').innerHTML = (themeName === 'dark') ? iconSun : iconMoon;
        }

        function toggleTheme() {
            let current = document.documentElement.getAttribute('data-theme');
            let next = (current === 'light') ? 'dark' : 'light';
            localStorage.setItem('bas_theme', next);
            applyTheme(next);
        }

        // === 登录管理引擎 ===
        async function doLogin() {
            const pass = document.getElementById('loginPass').value.trim();
            if (!pass) return;
            
            try {
                const res = await fetch('/api/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: pass })
                });
                
                if (res.status === 200) {
                    localStorage.setItem('bas_token', pass);
                    enterSystem();
                } else {
                    alert('访问密钥错误，请重试！');
                }
            } catch(e) { alert('网络异常'); }
        }

        function doLogout() {
            localStorage.removeItem('bas_token');
            clearInterval(logInterval);
            terminalView.className = 'hidden';
            loginView.className = 'active';
            document.getElementById('loginPass').value = '';
        }

        function enterSystem() {
            loginView.className = 'hidden';
            terminalView.className = 'active';
            terminal.innerHTML = '';
            fetchLogs();
            logInterval = setInterval(fetchLogs, 3000);
        }

        // === 终端核心引擎 ===
        terminal.addEventListener('scroll', () => { 
            autoScroll = terminal.scrollHeight - terminal.scrollTop <= terminal.clientHeight + 10; 
        });

        window.copyToInput = function(cmdText) {
            cmdInput.value = cmdText + ' ';
            cmdInput.focus();
            navigator.clipboard.writeText(cmdText).catch(err => {});
            toast.innerText = `已快捷填入指令: ${cmdText} 按回车执行`;
            toast.style.opacity = '1';
            setTimeout(() => { toast.style.opacity = '0'; }, 2000);
        }

        async function fetchLogs() {
            const token = localStorage.getItem('bas_token');
            if(!token) return doLogout();

            try {
                const res = await fetch('/api/logs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: token })
                });
                
                if (res.status === 401) return doLogout();
                
                const data = await res.json();
                terminal.innerHTML = data.logs.map(log => {
                    let cls = 'INFO';
                    if (log.includes('[WARNING]')) cls = 'WARNING';
                    if (log.includes('[ERROR]') || log.includes('失败') || log.includes('异常') || log.includes('🚨')) cls = 'ERROR';
                    
                    let formattedLog = log.replace(/(\\/(?:status|stop|start|restart|sap)\\b)/g, 
                        '<span class="cmd-clickable" onclick="copyToInput(\\'$1\\')">$1</span>');
                        
                    return `<p class="log-line ${cls}">${formattedLog}</p>`;
                }).join('');
                
                if (autoScroll) terminal.scrollTop = terminal.scrollHeight;
            } catch (e) {}
        }

        cmdInput.addEventListener('keypress', async function (e) {
            if (e.key === 'Enter') {
                const cmd = cmdInput.value.trim();
                const token = localStorage.getItem('bas_token');
                if (!cmd || !token) return;
                
                const fakeLog = document.createElement('p');
                fakeLog.className = 'log-line INFO';
                fakeLog.innerText = `[${new Date().toISOString().slice(0,19).replace('T', ' ')}] root@bas:~# ${cmd}`;
                terminal.appendChild(fakeLog);
                if (autoScroll) terminal.scrollTop = terminal.scrollHeight;

                cmdInput.value = '';
                
                try {
                    const res = await fetch(`/api/command`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ token: token, command: cmd })
                    });
                    if (res.status === 401) doLogout();
                } catch (err) {}
            }
        });

        // 初始化
        initTheme();
        if (localStorage.getItem('bas_token')) {
            enterSystem();
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return HTML_TEMPLATE

# [新增路由] 用于登录验证
@app.route('/api/verify', methods=['POST'])
def verify_token():
    data = request.get_json()
    if data and data.get("token") == WEB_TOKEN:
        return jsonify({"status": "OK"}), 200
    return jsonify({"error": "Unauthorized"}), 401

# [修改路由] 使用 POST 验证 token，避免 URL 泄露
@app.route('/api/logs', methods=['POST'])
def api_logs():
    data = request.get_json()
    if not data or data.get("token") != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"logs": list(log_queue)})

# [修改路由] 命令下发也改为统一的 POST token 验证
@app.route('/api/command', methods=['POST'])
def web_command():
    data = request.get_json()
    if not data or data.get("token") != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    
    cmd_str = data.get("command", "").strip()
    if not cmd_str.startswith("/"):
        logger.warning(f"[Web终端] 语法错误: {cmd_str} (需以 / 开头)")
        return jsonify({"error": "Invalid command format"}), 400
        
    parts = cmd_str.split()
    command = parts[0].replace("/", "").lower()
    target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    
    logger.info(f"💻 [Web终端] {cmd_str}")
    
    if command in ['start', 'stop', 'restart']:
        bot_action_runner(command.upper(), target_id)
        return jsonify({"status": "Command dispatched to queue"})
        
    elif command == 'status':
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        if target_id and not target_accounts:
            logger.error(f"❌ [Web终端] 未找到 ID 为 {target_id} 的账号。")
            return jsonify({"status": "Not found"})
        
        def _check_web():
            sys_status = "🔴 繁忙 (排队/执行中)" if system_busy_event.is_set() else "🟢 空闲"
            logger.info(f"📊 [查询报告] 后台任务状态: {sys_status}")
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                logger.info(f"👤 账号 {acc['id']} ({acc['email']}) -> ☁️ 状态: {status}")
                
        threading.Thread(target=_check_web).start()
        return jsonify({"status": "Checking status"})
        
    elif command == 'sap':
        logger.info("--------- 可用命令 ---------")
        logger.info("🔹 /status   ( 查询 BAS )")
        logger.info("🔹 /stop     ( 停止 BAS )")
        logger.info("🔹 /start    ( 启动 BAS )")
        logger.info("🔹 /restart  ( 重启 BAS )")
        return jsonify({"status": "Help displayed"})
    
    else:
        logger.warning(f"⚠️ [Web终端] 未知命令: {cmd_str}")
        return jsonify({"error": "Unknown command"}), 400

# ==========================================
# 8. 启动引导区
# ==========================================
def start_bot_polling():
    logger.info(f"✈️ TG Bot 已上线。")
    bot.infinity_polling()

if __name__ == '__main__':
    logger.info("========================================")
    logger.info(f"🚀 SAP BAS 全自动保活 开始运行! 检测到 {len(ACCOUNTS)} 个有效账号。")
    
    if not ACCOUNTS:
        logger.error("[!] 未检测到任何带有 SAP_EMAIL_X 后缀的账号环境变量，程序无法运行！")
        sys.exit(1)
        
    scheduler = BackgroundScheduler()
    for acc in ACCOUNTS:
        scheduler.add_job(lambda a=acc: async_task_runner("KEEPALIVE", a), trigger='cron', minute=acc['joba_min'], id=f"job_keepalive_{acc['id']}")
        scheduler.add_job(lambda a=acc: async_task_runner("RESTART", a), trigger='cron', hour=acc['jobb_hrs'], minute=acc['jobb_min'], id=f"job_restart_{acc['id']}")
        
        if acc.get('tunnel_url'):
            scheduler.add_job(lambda a=acc: tunnel_health_check(a), trigger='interval', minutes=1, id=f"job_health_{acc['id']}")
            logger.info(f"[+] 账号 {acc['id']} 定时器挂载 (保活:每小时{acc['joba_min']}分 | 重启:每天{acc['jobb_hrs']}时{acc['jobb_min']}分 | ARGO探针:已启用)")
        else:
            logger.info(f"[+] 账号 {acc['id']} 定时器挂载 (保活:每小时{acc['joba_min']}分 | 重启:每天{acc['jobb_hrs']}时{acc['jobb_min']}分 | ARGO探针:未启用)")

    scheduler.add_job(clean_probe_logs, trigger='interval', hours=1, id='job_clean_logs')

    scheduler.start()

    if bot:
        threading.Thread(target=start_bot_polling, daemon=True).start()

    logger.info(f"💻 Web 终端面板已就绪！")
    
    for acc in ACCOUNTS:
        if acc.get('tunnel_url'):
            threading.Thread(target=tunnel_health_check, args=(acc,)).start()

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
