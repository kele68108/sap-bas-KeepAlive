import os
import sys
import time
import threading
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, jsonify, render_template_string

from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 核心配置与全局状态变量初始化
# ==========================================
SAP_EMAIL = os.environ.get("SAP_EMAIL")
SAP_PASSWORD = os.environ.get("SAP_PASSWORD")
REGION_URL = os.environ.get("REGION_URL")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
WEB_TOKEN = os.environ.get("WEB_TOKEN", "default_token")
PORT = int(os.environ.get("PORT", 8080))

# --- 新增：自定义调度时间变量 ---
JOBA_MINUTE = os.environ.get("JOBA_MINUTE", "50")
JOBB_HOURS = os.environ.get("JOBB_HOURS", "*/12")
JOBB_MINUTE = os.environ.get("JOBB_MINUTE", "30")

# ！！！核心全局锁：防止定时任务和手动指令互相抢占浏览器引发内存爆炸 ！！！
action_lock = threading.Lock()

# ==========================================
# 2. 极客级内存日志系统 (Log Hijacking)
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
    def get_workspace_info():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{REGION_URL}/index.html")
                
                # 登录超时延长至 60 秒
                page.locator("input[name='j_username'], input[type='email']").fill(SAP_EMAIL)
                if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                     page.locator("button#logOnFormSubmit, button[type='submit']").click()
                     time.sleep(2)
                page.locator("input[name='j_password'], input[type='password']").fill(SAP_PASSWORD)
                page.locator("button#logOnFormSubmit, button[type='submit']").click()
                page.wait_for_url("**/index.html*", timeout=60000)
                
                req_headers = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}
                response = context.request.get(f"{REGION_URL}/ws-manager/api/v1/workspace", headers=req_headers)
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
    def execute_lifecycle_action(action_type):
        logger.info(f"🚀 开始执行核心生命周期任务: [{action_type}]")
        work_dir = "/tmp"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1920, 'height': 1080})
                page = context.new_page()
                api_request = context.request

                try:
                    logger.info("[-] 正在模拟登录过 SSO...")
                    page.goto(f"{REGION_URL}/index.html")
                    page.locator("input[name='j_username'], input[type='email']").fill(SAP_EMAIL)
                    if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                         page.locator("button#logOnFormSubmit, button[type='submit']").click()
                         time.sleep(2)
                    page.locator("input[name='j_password'], input[type='password']").fill(SAP_PASSWORD)
                    page.locator("button#logOnFormSubmit, button[type='submit']").click()
                    
                    # === 修复：等待时间延长至 60 秒 ===
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
                    
                    logger.info("[+] 登录成功，正在获取 Workspace API...")
                    req_headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    ws_api_url = f"{REGION_URL}/ws-manager/api/v1/workspace"
                    workspaces = api_request.get(ws_api_url, headers=req_headers).json()
                    
                    if not workspaces:
                        logger.error("未找到任何工作区")
                        return False
                        
                    ws = workspaces[0]
                    ws_uuid = ws.get("id") or ws.get("config", {}).get("id")
                    username = ws.get("config", {}).get("username", "")
                    display_name = ws.get("config", {}).get("labels", {}).get("ws-manager.devx.sap.com/displayname", ws_uuid)
                    status = ws.get("runtime", {}).get("status")
                    
                    csrf_headers = req_headers.copy()
                    csrf_headers["X-CSRF-Token"] = "Fetch"
                    csrf_token = api_request.get(ws_api_url, headers=csrf_headers).headers.get("x-csrf-token", "")
                    
                    def set_status(target_suspend, target_status):
                        encoded_username = urllib.parse.quote(username)
                        action_url = f"{REGION_URL}/ws-manager/api/v1/workspace/{ws_uuid}?all=false&username={encoded_username}"
                        headers = {"X-CSRF-Token": csrf_token, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
                        payload = {"suspended": target_suspend, "WorkspaceDisplayName": display_name}
                        api_request.put(action_url, headers=headers, data=payload)
                        
                        for _ in range(30):
                            time.sleep(10)
                            curr_ws = next((w for w in api_request.get(ws_api_url, headers=req_headers).json() if w.get("id") == ws_uuid or w.get("config", {}).get("id") == ws_uuid), {})
                            curr_status = curr_ws.get("runtime", {}).get("status", "UNKNOWN")
                            logger.info(f"[-] 状态轮询: 期望={target_status}, 当前={curr_status}")
                            if curr_status == target_status:
                                return True
                        return False

                    if action_type in ["RESTART", "STOP"] and status == "RUNNING":
                        logger.info("[*] 正在执行停止操作...")
                        if set_status(True, "STOPPED"):
                            logger.info("[+] 工作区已停止")
                            status = "STOPPED"
                    
                    if action_type == "STOP":
                        return True
                        
                    if action_type in ["START", "RESTART", "KEEPALIVE"] and status in ["STOPPED", "STARTING", "RUNNING"]:
                        if status == "STOPPED":
                            logger.info("[*] 正在执行启动操作...")
                            if not set_status(False, "RUNNING"):
                                logger.error("[!] 启动超时")
                                return False
                                
                        logger.info("[*] 开始进行 UI 穿透保活 (进入 IDE)...")
                        page.goto(f"{REGION_URL}/index.html")
                        time.sleep(8)
                        
                        ws_frame = page.frame_locator("iframe#ws-manager")
                        ws_link = ws_frame.locator(f"a[href*='{ws_uuid}']").first
                        ws_link.wait_for(state="visible", timeout=20000)
                        ws_link.click(force=True)
                        logger.info("[-] 已突破 iframe，正在加载 IDE (30秒)...")
                        time.sleep(30)
                        
                        logger.info("[-] 执行弹窗清理策略...")
                        for _ in range(3):
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                                
                        screenshot_path = f"{work_dir}/capture_{ws_uuid}.png"
                        page.screenshot(path=screenshot_path)
                        if action_type != "KEEPALIVE":
                            send_tg_photo(screenshot_path, f"🎯 <b>[{action_type}] 任务完成</b>\n工作区 [<b>{display_name}</b>] 隧道已唤醒！")
                        logger.info(f"[+] 🎯 [{action_type}] 任务全流程圆满成功！")
                        return True
                        
                except Exception as inner_e:
                    logger.error(f"[!] 严重异常 (内部): {str(inner_e)}")
                    # === 临终遗照 (Death Cam) 捕捉机制 ===
                    try:
                        error_shot = f"{work_dir}/error_crash_{action_type}.png"
                        page.screenshot(path=error_shot)
                        send_tg_photo(error_shot, f"❌ <b>执行 [{action_type}] 发生异常</b>\n请查看云端实时截图排查问题。\n报错: <code>{str(inner_e)}</code>")
                        logger.info("[-] 已成功截取并发送崩溃现场截图。")
                    except Exception as pic_e:
                        logger.error(f"保存崩溃截图失败: {pic_e}")
                    return False
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[!] 浏览器环境拉起失败: {str(e)}")
            return False

# ==========================================
# 5. 任务调度逻辑
# ==========================================
def async_task_runner(action, tg_reply_msg=None):
    if not action_lock.acquire(blocking=False):
        logger.warning(f"被跳过：系统繁忙，无法执行 {action}。")
        if tg_reply_msg: send_tg_msg("⚠️ 系统繁忙，请稍后再试。")
        return
        
    try:
        if tg_reply_msg: send_tg_msg(f"✅ 收到指令：正在执行 <b>{action}</b>...")
        SAPController.execute_lifecycle_action(action)
    finally:
        action_lock.release()
        logger.info(f"[{action}] 任务结束，释放锁。")

# ==========================================
# 6. Telegram Bot ChatOps
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        bot.reply_to(message, "🤖 <b>SAP BAS 机器人</b>\n🔹 /status 🔹 /stop 🔹 /start 🔹 /restart", parse_mode="HTML")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not check_tg_auth(message): return
        sys_status = "🔴 繁忙 (执行中)" if action_lock.locked() else "🟢 空闲"
        bot.reply_to(message, "正在查询云端，请稍候...", parse_mode="HTML")
        success, ws_id, status = SAPController.get_workspace_info()
        bot.send_message(TG_CHAT_ID, f"📊 后台锁: {sys_status}\n☁️ 云端状态: <b>{status}</b>", parse_mode="HTML")

    @bot.message_handler(commands=['start', 'stop', 'restart'])
    def handle_actions(message):
        if not check_tg_auth(message): return
        command = message.text.replace("/", "").upper()
        threading.Thread(target=async_task_runner, args=(command, True)).start()

# ==========================================
# 7. Flask Web 守护服务 (内嵌 HTML 模板)
# ==========================================
app = Flask(__name__)

# 内嵌 1Panel 极客终端风格 HTML
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAP BAS 终端</title>
    <style>
        body { background-color: #1e1e1e; color: #0f0; font-family: 'Consolas', monospace; margin: 0; padding: 20px; height: 100vh; box-sizing: border-box; display: flex; flex-direction: column; }
        .header { border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px; display: flex; justify-content: space-between; }
        .title { font-size: 1.2rem; font-weight: bold; color: #fff; }
        #terminal { flex: 1; overflow-y: auto; background-color: #000; padding: 15px; border-radius: 5px; box-shadow: inset 0 0 10px rgba(0,0,0,0.8); line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
        .log-line { margin: 0; }
        .INFO { color: #0f0; } .WARNING { color: #fc0; } .ERROR { color: #f33; }
        ::-webkit-scrollbar { width: 8px; } ::-webkit-scrollbar-track { background: #1e1e1e; } ::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }
    </style>
</head>
<body>
    <div class="header">
        <div class="title">🚀 SAP BAS 保活终端</div>
        <div>● 运行中 (刷新: 3s)</div>
    </div>
    <div id="terminal"></div>
    <script>
        const terminal = document.getElementById('terminal');
        let autoScroll = true;
        terminal.addEventListener('scroll', () => { autoScroll = terminal.scrollHeight - terminal.scrollTop <= terminal.clientHeight + 10; });
        async function fetchLogs() {
            try {
                const res = await fetch(`/api/{{ token }}`);
                if (res.status !== 200) { terminal.innerHTML = '<span class="ERROR">[!] 鉴权失败或异常</span>'; return; }
                const data = await res.json();
                terminal.innerHTML = data.logs.map(log => {
                    let cls = 'INFO';
                    if (log.includes('[WARNING]')) cls = 'WARNING';
                    if (log.includes('[ERROR]') || log.includes('失败') || log.includes('异常')) cls = 'ERROR';
                    return `<p class="log-line ${cls}">${log}</p>`;
                }).join('');
                if (autoScroll) terminal.scrollTop = terminal.scrollHeight;
            } catch (e) {}
        }
        fetchLogs(); setInterval(fetchLogs, 3000);
    </script>
</body>
</html>
"""

@app.route('/')
def health_check():
    return jsonify({"status": "OK"}), 200

# === 核心修改：极其隐蔽的独立路由 (域名/你的token) ===
@app.route('/<token>')
def view_logs(token):
    if token != WEB_TOKEN:
        return "404 Not Found", 404
    return render_template_string(HTML_TEMPLATE, token=token)

@app.route('/api/<token>')
def api_logs(token):
    if token != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"logs": list(log_queue)})

# ==========================================
# 8. 启动引导区
# ==========================================
def start_bot_polling():
    logger.info("[*] TG Bot 已上线...")
    bot.infinity_polling()

if __name__ == '__main__':
    logger.info("========================================")
    logger.info("🚀 SAP BAS 自动保活 开始启动...")
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: async_task_runner("KEEPALIVE"), trigger='cron', minute=JOBA_MINUTE, id='job_keepalive')
    scheduler.add_job(lambda: async_task_runner("RESTART"), trigger='cron', hour=JOBB_HOURS, minute=JOBB_MINUTE, id='job_restart')
    scheduler.start()
    logger.info(f"[+] 保活任务 (JobA: 每小时:{JOBA_MINUTE}, JobB: {JOBB_HOURS}:{JOBB_MINUTE})")

    if bot:
        threading.Thread(target=start_bot_polling, daemon=True).start()

    logger.info(f"[+] Web 日志启动")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
