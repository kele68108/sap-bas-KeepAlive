import os
import sys
import time
import threading
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, request, jsonify, render_template

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
WEB_TOKEN = os.environ.get("WEB_TOKEN", "default_token_please_change")
PORT = int(os.environ.get("PORT", 8080))

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

# 输出到内存池 (供 Web 面板使用)
mem_handler = MemoryHandler()
mem_handler.setFormatter(formatter)
logger.addHandler(mem_handler)

# 输出到标准控制台 (供 CF 兜底查看)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# ==========================================
# 3. 核心通讯组件
# ==========================================
bot = telebot.TeleBot(TG_BOT_TOKEN) if TG_BOT_TOKEN else None

def check_tg_auth(message):
    """验证发送消息的人是不是你本人"""
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
    """封装与 SAP 交互的所有无头浏览器及 API 操作"""
    
    @staticmethod
    def get_workspace_info():
        """快速获取工作区状态（仅 API，不弹浏览器，极轻量级）"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto(f"{REGION_URL}/index.html")
                
                # 快速登录
                page.locator("input[name='j_username'], input[type='email']").fill(SAP_EMAIL)
                if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                     page.locator("button#logOnFormSubmit, button[type='submit']").click()
                     time.sleep(2)
                page.locator("input[name='j_password'], input[type='password']").fill(SAP_PASSWORD)
                page.locator("button#logOnFormSubmit, button[type='submit']").click()
                page.wait_for_url("**/index.html*", timeout=30000)
                
                # 请求 API
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
        """执行复杂的生命周期操作：START(唤醒), STOP(停止), RESTART(重启)"""
        logger.info(f"🚀 开始执行核心生命周期任务: [{action_type}]")
        work_dir = "/tmp"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1920, 'height': 1080})
                page = context.new_page()
                api_request = context.request

                logger.info("[-] 正在模拟登录过 SSO...")
                page.goto(f"{REGION_URL}/index.html")
                page.locator("input[name='j_username'], input[type='email']").fill(SAP_EMAIL)
                if page.locator("button#logOnFormSubmit, button[type='submit']").is_visible():
                     page.locator("button#logOnFormSubmit, button[type='submit']").click()
                     time.sleep(2)
                page.locator("input[name='j_password'], input[type='password']").fill(SAP_PASSWORD)
                page.locator("button#logOnFormSubmit, button[type='submit']").click()
                page.wait_for_url("**/index.html*", timeout=30000)
                
                # 处理可能的主页弹窗
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
                
                # 辅助函数：API 启停与轮询
                def set_status(target_suspend, target_status):
                    encoded_username = urllib.parse.quote(username)
                    action_url = f"{REGION_URL}/ws-manager/api/v1/workspace/{ws_uuid}?all=false&username={encoded_username}"
                    headers = {"X-CSRF-Token": csrf_token, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    payload = {"suspended": target_suspend, "WorkspaceDisplayName": display_name}
                    api_request.put(action_url, headers=headers, data=payload)
                    
                    for _ in range(30): # 最多等 5 分钟
                        time.sleep(10)
                        curr_ws = next((w for w in api_request.get(ws_api_url, headers=req_headers).json() if w.get("id") == ws_uuid), {})
                        curr_status = curr_ws.get("runtime", {}).get("status", "UNKNOWN")
                        logger.info(f"[-] 状态轮询: 期望={target_status}, 当前={curr_status}")
                        if curr_status == target_status:
                            return True
                    return False

                # 逻辑分支
                if action_type in ["RESTART", "STOP"] and status == "RUNNING":
                    logger.info("[*] 正在执行停止操作...")
                    if set_status(True, "STOPPED"):
                        logger.info("[+] 工作区已停止")
                        status = "STOPPED"
                
                if action_type == "STOP":
                    browser.close()
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
                    
                    try:
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
                            
                        # 截图并发送
                        screenshot_path = f"{work_dir}/capture_{ws_uuid}.png"
                        page.screenshot(path=screenshot_path)
                        if action_type != "KEEPALIVE": # 静默保活不发图，人为/重启发图
                            send_tg_photo(screenshot_path, f"🎯 <b>[{action_type}] 任务完成</b>\n工作区 [<b>{display_name}</b>] 隧道已深度唤醒并就绪！")
                        logger.info(f"[+] 🎯 [{action_type}] 任务全流程圆满成功！")
                        
                    except Exception as e:
                        logger.error(f"[!] IDE 穿透失败: {e}")
                        browser.close()
                        return False

                browser.close()
                return True
        except Exception as e:
            logger.error(f"[!] 严重异常: {str(e)}")
            return False

# ==========================================
# 5. 任务调度逻辑 (后台线程异步执行，防阻塞)
# ==========================================
def async_task_runner(action, tg_reply_msg=None):
    """获取全局锁并在独立线程运行核心任务"""
    if not action_lock.acquire(blocking=False):
        logger.warning(f"被跳过：尝试执行 {action}，但系统当前繁忙 (锁占用)。")
        if tg_reply_msg: send_tg_msg("⚠️ 系统当前繁忙 (自动化任务进行中)，请 3 分钟后再试。")
        return
        
    try:
        if tg_reply_msg: send_tg_msg(f"✅ 收到指令：正在执行 <b>{action}</b>，预计需几分钟，请耐心等待...")
        SAPController.execute_lifecycle_action(action)
    finally:
        action_lock.release()
        logger.info(f"[{action}] 任务线程结束，释放全局锁。")

# ==========================================
# 6. Telegram Bot ChatOps 路由
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        help_text = (
            "🤖 <b>SAP BAS 监控机器人</b>\n\n"
            "--- 可用命令 ---\n"
            "🔹 /status （查询云端 BAS 实时状态）\n"
            "🔹 /stop （强制停止 BAS 容器）\n"
            "🔹 /start （唤醒并穿透 BAS 隧道）\n"
            "🔹 /restart （完全重置 BAS 生命周期）"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not check_tg_auth(message): return
        sys_status = "🔴 繁忙 (正在执行任务)" if action_lock.locked() else "🟢 空闲"
        bot.reply_to(message, f"正在查询云端，请稍候...", parse_mode="HTML")
        success, ws_id, status = SAPController.get_workspace_info()
        if success:
            bot.send_message(TG_CHAT_ID, f"📊 <b>状态报告</b>\n后台锁状态: {sys_status}\n云端真实状态: <b>{status}</b>", parse_mode="HTML")
        else:
            bot.send_message(TG_CHAT_ID, f"❌ <b>状态查询失败</b>\n{status}", parse_mode="HTML")

    @bot.message_handler(commands=['start', 'stop', 'restart'])
    def handle_actions(message):
        if not check_tg_auth(message): return
        command = message.text.replace("/", "").upper()
        # 启动后台线程跑任务，不阻塞 bot 轮询
        threading.Thread(target=async_task_runner, args=(command, True)).start()

# ==========================================
# 7. Flask Web 守护服务 (CF 测活 & 日志展示)
# ==========================================
app = Flask(__name__)

@app.route('/')
def health_check():
    """根路径留给 CF 和 Uptime Kuma 测活"""
    return jsonify({"status": "OK", "service": "SAP-BAS-Keeper"}), 200

@app.route('/logs')
def view_logs():
    """渲染终端界面"""
    token = request.args.get('token')
    if token != WEB_TOKEN:
        return "401 Unauthorized: Invalid Token", 401
    return render_template('index.html')

@app.route('/api/logs')
def api_logs():
    """API 接口：吐出当前内存中的日志队列"""
    token = request.args.get('token')
    if token != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"logs": list(log_queue)})

# ==========================================
# 8. 启动引导区 (Bootstrapper)
# ==========================================
def start_bot_polling():
    logger.info("[*] TG Bot 长轮询线程已启动...")
    bot.infinity_polling()

if __name__ == '__main__':
    logger.info("========================================")
    logger.info("🚀 终极 SAP 套娃战车系统 开始初始化...")
    
    # 检查核心变量
    if not all([SAP_EMAIL, SAP_PASSWORD, REGION_URL]):
        logger.error("[!] 致命错误：缺失必要的 SAP 环境变量！程序终止。")
        sys.exit(1)

    # 1. 启动 APScheduler (纯后台)
    scheduler = BackgroundScheduler()
    # Job A: 每小时 50 分静默保活
    scheduler.add_job(lambda: async_task_runner("KEEPALIVE"), trigger='cron', minute=50, id='job_keepalive')
    # Job B: 每 12 小时的 30 分钟重启洗髓
    scheduler.add_job(lambda: async_task_runner("RESTART"), trigger='cron', hour='*/12', minute=30, id='job_restart')
    scheduler.start()
    logger.info("[+] 定时任务调度器已挂载 (JobA: XX:50, JobB: */12:30)")

    # 2. 启动 Telegram Bot 轮询 (独立子线程)
    if bot:
        bot_thread = threading.Thread(target=start_bot_polling, daemon=True)
        bot_thread.start()
    else:
        logger.warning("[-] 未配置 TG Token，ChatOps 功能将关闭。")

    # 3. 启动 Flask Web 主干 (占据主线程，响应 CF 的端口要求)
    logger.info(f"[+] Web 服务器已启动，监听端口: {PORT}")
    # 生产环境中推荐用 host="0.0.0.0" 让外部网络可以打进来
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
