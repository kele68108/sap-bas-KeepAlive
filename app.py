import os
import sys
import time
import threading
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, jsonify, render_template_string, request # 新增了 request

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

# 动态加载所有配置了 SAP_EMAIL_X 的账号
ACCOUNTS = []
for i in range(1, 11):
    email = os.environ.get(f"SAP_EMAIL_{i}")
    if email:
        ACCOUNTS.append({
            "id": i,
            "email": email,
            "password": os.environ.get(f"SAP_PASSWORD_{i}"),
            "region_url": os.environ.get(f"REGION_URL_{i}"),
            "joba_min": os.environ.get(f"JOBA_MINUTE_{i}", "50"),
            "jobb_hrs": os.environ.get(f"JOBB_HOURS_{i}", "*/12"),
            "jobb_min": os.environ.get(f"JOBB_MINUTE_{i}", "30"),
            "tunnel_url": os.environ.get(f"TUNNEL_URL_{i}"), # 新增：隧道探针URL
            "fail_count": 0                                  # 新增：连续失败计数器
        })

action_lock = threading.Lock()

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
                    
                    # 🛡️ 科学拦截
                    if action_type == "STOP" and status == "STOPPED":
                        msg = f"ℹ️ <b>操作跳过 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 当前已经是 <b>STOPPED</b> 状态，无需重复停止。"
                        send_tg_msg(msg)
                        logger.info(f"[-] 账号 {acc_id} 状态已是 STOPPED，无需重复停止。")
                        return True
                        
                    if action_type == "START" and status == "RUNNING":
                        msg = f"ℹ️ <b>操作跳过 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 当前已经是 <b>RUNNING</b> 状态，无需重复启动。\n💡 <i>提示：若代理隧道不通，请使用 /restart 进行深度重置。</i>"
                        send_tg_msg(msg)
                        logger.info(f"[-] 账号 {acc_id} 状态已是 RUNNING，无需重复启动。")
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
                        msg = f"🔴 <b>[{action_type}] 任务完成 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 已成功停止服务！"
                        send_tg_msg(msg)
                        logger.info(f"[+] 账号 {acc_id} STOP 任务结束，已发送通知。")
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
                            send_tg_photo(screenshot_path, f"🎯 <b>[{action_type}] 任务完成 (账号 {acc_id})</b>\n工作区 [<b>{display_name}</b>] 已唤醒！")
                        logger.info(f"[+] 🎯 账号 {acc_id} [{action_type}] 任务成功。")
                        return True
                        
                except Exception as inner_e:
                    logger.error(f"[!] 账号 {acc_id} 严重异常: {str(inner_e)}")
                    try:
                        error_shot = f"{work_dir}/error_crash_{acc_id}_{action_type}.png"
                        page.screenshot(path=error_shot)
                        send_tg_photo(error_shot, f"❌ <b>执行 [{action_type}] 发生异常 (账号 {acc_id})</b>\n请查看云端实时截图排查问题。\n报错: <code>{str(inner_e)}</code>")
                    except Exception as pic_e:
                        logger.error(f"保存崩溃截图失败: {pic_e}")
                    return False
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[!] 浏览器环境拉起失败: {str(e)}")
            return False

# ==========================================
# 5. 任务并发调度控制逻辑 与 新增隧道探针
# ==========================================
def async_task_runner(action, account):
    """供定时任务使用的单发运行器"""
    if not action_lock.acquire(blocking=False):
        logger.warning(f"被跳过：账号 {account['id']} 尝试 {action}，但系统繁忙。")
        return
    try:
        SAPController.execute_lifecycle_action(action, account)
    finally:
        action_lock.release()

def bot_action_runner(action, target_id=None):
    """供机器人和网页端手动调用的全局串行运行器"""
    if not action_lock.acquire(blocking=False):
        logger.warning("系统繁忙，手动指令排队被拒绝。")
        send_tg_msg("⚠️ 系统当前有任务正在执行，请等待释放锁后再试。")
        return
    try:
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        if not target_accounts:
            msg = f"❌ 未找到 ID 为 <b>{target_id}</b> 的账号配置！"
            logger.error(msg.replace("<b>", "").replace("</b>", ""))
            send_tg_msg(msg)
            return
            
        if target_id:
            msg = f"✅ 收到指令：即将为 <b>账号 {target_id}</b> 执行 <b>{action}</b>..."
        else:
            msg = f"✅ 收到指令：即将为 <b>{len(target_accounts)} 个账号</b> 依次执行 <b>{action}</b>..."
            
        logger.info(msg.replace("<b>", "").replace("</b>", ""))
        send_tg_msg(msg)
            
        for acc in target_accounts:
            SAPController.execute_lifecycle_action(action, acc)
            if len(target_accounts) > 1:
                time.sleep(3)
                
        logger.info(f"🎉 {action} 指令下发执行完毕！")
        send_tg_msg(f"🎉 <b>{action} 指令下发执行完毕！</b>")
    finally:
        action_lock.release()

def tunnel_health_check(account):
    """[新增] 隧道心跳监测探针"""
    url = account.get('tunnel_url')
    if not url: return
    
    # 巧妙避开撞车：如果系统正在跑重启，隧道本来就是断的，直接 return 跳过本次检测
    if action_lock.locked():
        return
        
    try:
        # 设置 timeout 防卡死，模拟浏览器头防止被盾直接拒绝
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        status_code = res.status_code
    except Exception as e:
        status_code = 503 # 任何连接超时、域名解析失败，统一按离线 503 处理
        
    acc_id = account['id']
    
    # 逻辑分析：4xx 证明隧道畅通，重置 fail_count
    if 400 <= status_code < 500:
        if account['fail_count'] > 0:
            logger.info(f"[+] 隧道恢复：账号 {acc_id} 探针打通 (状态码: {status_code})。警报解除。")
        account['fail_count'] = 0
        
    # 逻辑分析：5xx 证明物理掉线，累计 5 次杀无赦
    elif 500 <= status_code < 600:
        account['fail_count'] += 1
        logger.warning(f"[!] 探针警告：账号 {acc_id} 隧道离线 (状态码: {status_code}) - 累计失败 {account['fail_count']}/5 次")
        
        if account['fail_count'] >= 5:
            logger.error(f"🚨 [紧急避险] 账号 {acc_id} 隧道连续 5 次断线，触发自动重启洗髓！")
            account['fail_count'] = 0 # 重置计数器，防止重启风暴
            send_tg_msg(f"🚨 <b>隧道掉线警报 (账号 {acc_id})</b>\n检测到 HTTP 50x 或无法连接，连续 5 次心跳失败，正在触发紧急重置！")
            # 召唤全局任务线程去跑（因为里面有锁机制，非常安全）
            threading.Thread(target=bot_action_runner, args=("RESTART", acc_id)).start()

# ==========================================
# 6. Telegram Bot ChatOps (支持带参数)
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        help_text = (
            "🤖 <b>SAP BAS 监控机器人</b>\n\n"
            "--- 可用命令 ---\n"
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
            sys_status = "🔴 繁忙 (执行中)" if action_lock.locked() else "🟢 空闲"
            report = f"📊 <b>全局后台锁</b>: {sys_status}\n\n"
            
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                report += f"👤 <b>账号 {acc['id']}</b> ({acc['email']})\n"
                report += f"☁️ 状态: <b>{status}</b>\n\n"
            
            bot.send_message(TG_CHAT_ID, report, parse_mode="HTML")
            
        threading.Thread(target=_check).start()

    @bot.message_handler(commands=['start', 'stop', 'restart'])
    def handle_actions(message):
        if not check_tg_auth(message): return
        parts = message.text.strip().split()
        command = parts[0].replace("/", "").upper()
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        
        threading.Thread(target=bot_action_runner, args=(command, target_id)).start()

# ==========================================
# 7. Flask Web 守护服务 (全新交互式终端)
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAP BAS KEEPALIVE 终端</title>
    <style>
        body { background-color: #1e1e1e; color: #0f0; font-family: 'Consolas', monospace; margin: 0; padding: 20px; height: 100vh; box-sizing: border-box; display: flex; flex-direction: column; }
        .header { border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px; display: flex; justify-content: space-between; }
        .title { font-size: 1.2rem; font-weight: bold; color: #fff; }
        #terminal { flex: 1; overflow-y: auto; background-color: #000; padding: 15px; border-radius: 5px; box-shadow: inset 0 0 10px rgba(0,0,0,0.8); line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
        .log-line { margin: 0; }
        .INFO { color: #0f0; } .WARNING { color: #fc0; } .ERROR { color: #f33; }
        ::-webkit-scrollbar { width: 8px; } ::-webkit-scrollbar-track { background: #1e1e1e; } ::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }
        
        /* 新增输入交互区样式 */
        #input-area { display: flex; margin-top: 15px; border-top: 1px solid #333; padding-top: 15px; align-items: center; }
        #cmd-prefix { color: #0f0; margin-right: 10px; font-weight: bold; font-size: 1.1rem; }
        #cmdInput { flex: 1; background: transparent; border: none; color: #fff; font-family: 'Consolas', monospace; font-size: 1.1rem; outline: none; }
        #cmdInput::placeholder { color: #555; }
    </style>
</head>
<body>
    <div class="header">
        <div class="title">🚀 SAP BAS KEEPALIVE</div>
        <div>● 运行中 (刷新: 3s)</div>
    </div>
    <div id="terminal"></div>
    
    <div id="input-area">
        <span id="cmd-prefix">root@bas:~#</span>
        <input type="text" id="cmdInput" autocomplete="off" spellcheck="false" placeholder="输入指令 (如 /status, /start 1, /restart) 并按回车执行">
    </div>

    <script>
        const terminal = document.getElementById('terminal');
        const cmdInput = document.getElementById('cmdInput');
        let autoScroll = true;

        terminal.addEventListener('scroll', () => { 
            autoScroll = terminal.scrollHeight - terminal.scrollTop <= terminal.clientHeight + 10; 
        });

        async function fetchLogs() {
            try {
                const res = await fetch(`/api/{{ token }}`);
                if (res.status !== 200) { terminal.innerHTML = '<span class="ERROR">[!] 鉴权失败或异常</span>'; return; }
                const data = await res.json();
                terminal.innerHTML = data.logs.map(log => {
                    let cls = 'INFO';
                    if (log.includes('[WARNING]')) cls = 'WARNING';
                    if (log.includes('[ERROR]') || log.includes('失败') || log.includes('异常') || log.includes('🚨')) cls = 'ERROR';
                    return `<p class="log-line ${cls}">${log}</p>`;
                }).join('');
                if (autoScroll) terminal.scrollTop = terminal.scrollHeight;
            } catch (e) {}
        }
        
        fetchLogs(); 
        setInterval(fetchLogs, 3000);

        // 新增：监听输入框回车键并提交后端接口
        cmdInput.addEventListener('keypress', async function (e) {
            if (e.key === 'Enter') {
                const cmd = cmdInput.value.trim();
                if (!cmd) return;
                
                // 将指令展示在本地日志，增强打击感
                const fakeLog = document.createElement('p');
                fakeLog.className = 'log-line INFO';
                fakeLog.innerText = `[${new Date().toISOString().slice(0,19).replace('T', ' ')}] root@bas:~# ${cmd}`;
                terminal.appendChild(fakeLog);
                if (autoScroll) terminal.scrollTop = terminal.scrollHeight;

                cmdInput.value = '';
                
                try {
                    const res = await fetch(`/api/command/{{ token }}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ command: cmd })
                    });
                    if (res.status === 401) alert('Unauthorized');
                } catch (err) {
                    console.error("指令发送失败", err);
                }
            }
        });
    </script>
</body>
</html>
"""

@app.route('/')
def health_check():
    return jsonify({"status": "OK"}), 200

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

# [新增] Web 终端接收指令的专属路由
@app.route('/api/command/<token>', methods=['POST'])
def web_command(token):
    if token != WEB_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json()
    cmd_str = data.get("command", "").strip()
    
    if not cmd_str.startswith("/"):
        logger.warning(f"[Web终端] 语法错误: {cmd_str} (需以 / 开头)")
        return jsonify({"error": "Invalid command format"}), 400
        
    parts = cmd_str.split()
    command = parts[0].replace("/", "").lower()
    target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    
    logger.info(f"💻 [Web终端输入] 接收到指令: {cmd_str}")
    
    if command in ['start', 'stop', 'restart']:
        threading.Thread(target=bot_action_runner, args=(command.upper(), target_id)).start()
        return jsonify({"status": "Command dispatched"})
        
    elif command == 'status':
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        if target_id and not target_accounts:
            logger.error(f"❌ [Web终端] 未找到 ID 为 {target_id} 的账号。")
            return jsonify({"status": "Not found"})
        
        def _check_web():
            sys_status = "🔴 繁忙 (执行中)" if action_lock.locked() else "🟢 空闲"
            logger.info(f"📊 [查询报告] 全局后台锁: {sys_status}")
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                logger.info(f"👤 账号 {acc['id']} ({acc['email']}) -> ☁️ 云端真实状态: {status}")
                
        threading.Thread(target=_check_web).start()
        return jsonify({"status": "Checking status"})
    
    else:
        logger.warning(f"⚠️ [Web终端] 未知命令: {cmd_str}")
        return jsonify({"error": "Unknown command"}), 400

# ==========================================
# 8. 启动引导区
# ==========================================
def start_bot_polling():
    logger.info("[*] TG Bot 已上线...")
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
        
        # [新增] 仅为配置了 TUNNEL_URL 的账号挂载探针
        if acc.get('tunnel_url'):
            scheduler.add_job(lambda a=acc: tunnel_health_check(a), trigger='interval', minutes=1, id=f"job_health_{acc['id']}")
            logger.info(f"[+] 账号 {acc['id']} 定时器挂载 (保活:{acc['joba_min']}分 | 重启:{acc['jobb_hrs']}时{acc['jobb_min']}分 | 探针:启用)")
        else:
            logger.info(f"[+] 账号 {acc['id']} 定时器挂载 (保活:{acc['joba_min']}分 | 重启:{acc['jobb_hrs']}时{acc['jobb_min']}分 | 探针:未启用)")

    scheduler.start()

    if bot:
        threading.Thread(target=start_bot_polling, daemon=True).start()

    logger.info(f"[+] Web 终端面板已就绪！")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
