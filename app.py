import os
import sys
import time
import threading
import queue
import urllib.parse
import logging
from collections import deque
import requests
from flask import Flask, jsonify, render_template_string, request

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

# ==========================================
# 2. 极简复古日志系统
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
            logger.error(f"<SYS_ERR_> 远端推送接口异常: {str(e)} [FAIL]")

def send_tg_photo(photo_path, caption=""):
    if bot and TG_CHAT_ID and os.path.exists(photo_path):
        try:
            with open(photo_path, 'rb') as photo:
                bot.send_photo(TG_CHAT_ID, photo, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"<SYS_ERR_> 图像流回传阻断: {str(e)} [FAIL]")

# ==========================================
# 4. 业务逻辑层
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
        
        logger.info(f"<EXEC_JOB> 进程提权，执行核心序列: [{action_type}] (节点 {acc_id})")
        work_dir = "/tmp"
        
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1920, 'height': 1080})
                page = context.new_page()
                api_request = context.request

                try:
                    logger.info(f" > AUTH_REQ_ 节点 {acc_id} 请求建立安全隧道会话... [WAIT]")
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
                    
                    logger.info(f" < AUTH_ACK_ 节点 {acc_id} 鉴权通过，已接管远端 API 总线... [ OK ]")
                    req_headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
                    ws_api_url = f"{region_url}/ws-manager/api/v1/workspace"
                    workspaces = api_request.get(ws_api_url, headers=req_headers).json()
                    
                    if not workspaces:
                        logger.error(f"[!!FATAL!!] 节点 {acc_id} 挂载区未侦测到有效容器实体 [FAIL]")
                        return False
                        
                    ws = workspaces[0]
                    ws_uuid = ws.get("id") or ws.get("config", {}).get("id")
                    username = ws.get("config", {}).get("username", "")
                    display_name = ws.get("config", {}).get("labels", {}).get("ws-manager.devx.sap.com/displayname", ws_uuid)
                    status = ws.get("runtime", {}).get("status")
                    
                    if action_type == "STOP" and status == "STOPPED":
                        msg = f"► <b>指令调度合并 (节点 {acc_id})</b>\n目标容器 [<b>{display_name}</b>] 已处于 <b>挂起态 (STOPPED)</b>，动作跳过。"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ 节点 {acc_id} 算力已挂起，停止指令合并 [ OK ]")
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type == "START" and status == "RUNNING":
                        msg = f"► <b>指令调度合并 (节点 {acc_id})</b>\n目标容器 [<b>{display_name}</b>] 已处于 <b>运行态 (RUNNING)</b>，动作跳过。\n💡 <i>若边缘隧道阻断请使用 /restart 进行硬重置。</i>"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ 节点 {acc_id} 状态已激活，唤醒指令合并 [ OK ]")
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
                            logger.info(f" > SYS_POLL_ 节点 {acc_id} 状态轮询: 期望={target_status}, 当前={curr_status} [WAIT]")
                            if curr_status == target_status:
                                return True
                        return False

                    if action_type in ["RESTART", "STOP"] and status == "RUNNING":
                        logger.info(f" > SYS_HALT_ 节点 {acc_id} 下发挂起信令，释放算力资源... [WAIT]")
                        if set_status(True, "STOPPED"):
                            logger.info(f" < TASK_END_ 节点 {acc_id} 资源释放完成，已安全挂起 [ OK ]")
                            status = "STOPPED"
                    
                    if action_type == "STOP":
                        msg = f"■ <b>算力释放完毕 (节点 {acc_id})</b>\n目标容器 [<b>{display_name}</b>] 已成功退回挂起状态。"
                        send_tg_msg(msg)
                        logger.info(f" < TASK_END_ 节点 {acc_id} 强制休眠指令归档 [ OK ]")
                        account['probe_paused'] = True
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                    if action_type in ["START", "RESTART", "KEEPALIVE"] and status in ["STOPPED", "STARTING", "RUNNING"]:
                        if status == "STOPPED":
                            logger.info(f" > SYS_BOOT_ 节点 {acc_id} 申请分配底层计算资源... [WAIT]")
                            if not set_status(False, "RUNNING"):
                                logger.error(f"[!!FATAL!!] 节点 {acc_id} 资源分配超时，启动异常 [FAIL]")
                                return False
                                
                        logger.info(f" > UI_PENET_ 节点 {acc_id} 注入无头探测器探针... [WAIT]")
                        page.goto(f"{region_url}/index.html")
                        time.sleep(8)
                        
                        ws_frame = page.frame_locator("iframe#ws-manager")
                        ws_link = ws_frame.locator(f"a[href*='{ws_uuid}']").first
                        ws_link.wait_for(state="visible", timeout=20000)
                        ws_link.click(force=True)
                        logger.info(f" > IDE_LOAD_ 节点 {acc_id} 等待核心 IDE 构件装载... [WAIT]")
                        time.sleep(30)
                        
                        logger.info(f" > UI_CLEAN_ 节点 {acc_id} 执行模态框静默消除策略... [WAIT]")
                        for _ in range(3):
                            page.keyboard.press("Escape")
                            time.sleep(0.5)
                                
                        screenshot_path = f"{work_dir}/capture_{acc_id}_{ws_uuid}.png"
                        page.screenshot(path=screenshot_path)
                        if action_type != "KEEPALIVE":
                            send_tg_photo(screenshot_path, f"■ <b>系统唤醒完成 (节点 {acc_id})</b>\n通知：目标容器 [<b>{display_name}</b>] 算力单元已上线！")
                        logger.info(f" < TASK_END_ 节点 {acc_id} [{action_type}] 调度流程执行成功 [ OK ]")
                        
                        account['probe_paused'] = False
                        account['fail_count'] = 0
                        account['auto_restart_count'] = 0
                        return True
                        
                except Exception as inner_e:
                    logger.error(f"[!!FATAL!!] 节点 {acc_id} 运行时发生内核级崩溃 [FAIL]")
                    try:
                        error_shot = f"{work_dir}/error_crash_{acc_id}_{action_type}.png"
                        page.screenshot(path=error_shot)
                        send_tg_photo(error_shot, f"▲ <b>内核级异常警报 (节点 {acc_id})</b>\n调度指令: {action_type}\n栈追踪: <code>{str(inner_e)}</code>")
                    except Exception as pic_e:
                        logger.error(f"<SYS_ERR_> 栈追踪快照导出失败: {pic_e} [FAIL]")
                    return False
                finally:
                    browser.close()
        except Exception as e:
            logger.error(f"[!!FATAL!!] 沙盒环境拉起失败，环境异常: {str(e)} [FAIL]")
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
        acc_str = f"节点 {target_accounts[0]['id']}" if len(target_accounts) == 1 else f"全局 {len(target_accounts)} 个节点"
        msg = f"► <b>调度任务入队</b>\n目标: <b>{acc_str}</b>\n指令: <b>{action}</b>..."
        logger.info(f"<SCHEDULR> 手动调度队列 [{action}] 已覆写进内存 [ OK ]")
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
            logger.error(f"<SYS_ERR_> 调度流水线突发阻塞: {e} [FAIL]")
        finally:
            system_busy_event.clear()
            if source == "MANUAL":
                finish_msg = "■ <b>终端报告</b>\n队列任务已清空，全局硬件锁已释放。"
                logger.info("<SCHEDULR> 调度队列执行完毕，互斥锁已解除 [ OK ]")
                send_tg_msg(finish_msg)
            task_queue.task_done()

threading.Thread(target=global_task_worker, daemon=True).start()

def async_task_runner(action, account):
    enqueue_task(action, [account], "CRON")

def bot_action_runner(action, target_id=None):
    target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
    if not target_accounts:
        msg = f"▲ 映射表未匹配到标识 [<b>{target_id}</b>] 的参数块！"
        logger.error(f"<SCHEDULR> 标识 {target_id} 索引缺失，越权被拒绝 [FAIL]")
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
        logger.info(f" > NET_PING_ 节点 {acc_id} 边缘隧道稳定 (HTTP:{status_code}) ... [ OK ]")
        if account['fail_count'] > 0 or account['auto_restart_count'] > 0:
            logger.info(f" < NET_RECV_ 节点 {acc_id} 数据包重组成功，告警解除 [ OK ]")
            send_tg_msg(f"■ <b>链路连接恢复 (节点 {acc_id})</b>\n边缘隧道连通性测试通过。")
        account['fail_count'] = 0
        account['auto_restart_count'] = 0
        
    elif 500 <= status_code < 600:
        account['fail_count'] += 1
        logger.warning(f" > NET_PING_ 节点 {acc_id} 边缘隧道发生丢包 ({account['fail_count']}/5)... [WARN]")
        
        if account['fail_count'] >= 5:
            if account['auto_restart_count'] >= 3:
                logger.error(f"[!!FATAL!!] 节点 {acc_id} 连续 3 次硬重启均超时，探针已挂起 [FAIL]")
                send_tg_msg(f"▲ <b>节点离线阻断 (节点 {acc_id})</b>\n连续 3 次硬重置后链路彻底断联，该节点探针已被系统强制挂起。")
                account['probe_paused'] = True
                account['fail_count'] = 0
                return
                
            account['auto_restart_count'] += 1
            account['fail_count'] = 0
            logger.error(f"<SYS_CRIT> 节点 {acc_id} 丢包率越界，强制触发冷启动序列 ({account['auto_restart_count']}/3)...")
            send_tg_msg(f"▲ <b>网络劣化告警 (节点 {acc_id})</b>\n网络探针连续 5 次超时，拉起强制重置序列 ({account['auto_restart_count']}/3)...")
            enqueue_task("RESTART", [account], "PROBE")

def clean_probe_logs():
    try:
        filtered_logs = [log for log in list(log_queue) if "NET_PING_" not in log]
        log_queue.clear()
        log_queue.extend(filtered_logs)
        logger.info("<MEM_SWEEP> 常规网络嗅探冗余日志已从内存堆栈剥离 [ OK ]")
    except Exception as e:
        logger.error(f"<SYS_ERR_> 垃圾回收机制陷入死锁: {str(e)} [FAIL]")

# ==========================================
# 6. Telegram Bot ChatOps
# ==========================================
if bot:
    @bot.message_handler(commands=['sap'])
    def handle_help(message):
        if not check_tg_auth(message): return
        help_text = (
            "► <b>MAINFRAME CONSOLE</b>\n\n"
            "--------- 主机集群控制终端 ---------\n"
            "❖ /status   ( 节点运行状态追踪 )\n"
            "❖ /stop     ( 强制释放计算资源 )\n"
            "❖ /start    ( 唤醒挂起算力容器 )\n"
            "❖ /restart  ( 硬重置数据流链路 )\n\n"
            "<i>高维参数: 指令+编号 (例: /start 1)</i>"
        )
        bot.reply_to(message, help_text, parse_mode="HTML")

    @bot.message_handler(commands=['status'])
    def handle_status(message):
        if not check_tg_auth(message): return
        parts = message.text.split()
        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        
        if target_id and not target_accounts:
            bot.reply_to(message, f"▲ 映射表未能匹配到编号 <b>{target_id}</b> 的节点配置。", parse_mode="HTML")
            return

        bot.reply_to(message, f"⧗ 正在轮询集群节点状态...", parse_mode="HTML")
        
        def _check():
            sys_status = "■ 繁忙 (核心队列阻塞中)" if system_busy_event.is_set() else "■ 空闲 (全局调度锁释放)"
            report = f"► <b>系统全局调度状态</b>: {sys_status}\n\n"
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                report += f"👤 <b>节点编号 {acc['id']}</b> ({acc['email']})\n"
                report += f"■ 容器物理态: <b>{status}</b>\n\n"
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
# 7. Flask Web 守护服务
# ==========================================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SYS_CONSOLE</title>
    <style>
        /* 复古赛博朋克 色彩与字体 */
        @import url('https://fonts.googleapis.com/css2?family=DotGothic16&family=VT323&display=swap');
        
        :root[data-theme="dark"] { 
            --bg-body: #0d1117; --bg-window: #010409; --bg-header: #161b22;
            --text-norm: #34d399; /* 荧光绿 */
            --text-muted: #4b5563; --border-col: #30363d;
            --input-bg: #000000; --toast-bg: #1f2937; --toast-text: #34d399;
            --cmd-bg: transparent; --cmd-col: #58a6ff; --cmd-border: #58a6ff; --cmd-hover: #1f6feb;
            --log-info: #34d399; --log-warn: #fbbf24; --log-err: #f87171;
            --shadow-window: 0 0 40px rgba(52, 211, 153, 0.1), 0 0 0 1px #30363d;
            --bloom: 0 0 3px rgba(52, 211, 153, 0.4); 
        }
        :root[data-theme="light"] { 
            --bg-body: #e5e7eb; --bg-window: #f6f8fa; --bg-header: #e1e4e8;
            --text-norm: #065f46; /* 暗黑绿 */
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
        
        .mac-title { font-size: 14px; font-weight: bold; color: var(--text-muted); letter-spacing: 1px; text-align: center; flex: 1; font-family: -apple-system, sans-serif;}
        .mac-spacer { width: 60px; } 

        #login-view .mac-window { width: 380px; height: auto; }
        .login-content { padding: 40px; text-align: center; }
        .login-content h2 { margin: 0 0 25px; font-size: 24px; color: var(--text-norm); font-weight: normal; letter-spacing: 2px;}
        .login-content input { width: 100%; padding: 12px; margin-bottom: 25px; background: var(--input-bg); border: 1px solid var(--border-col); border-radius: 4px; color: var(--text-norm); font-family: inherit; font-size: 18px; text-align: center; outline: none; box-sizing: border-box; text-shadow: var(--bloom);}
        .login-content input:focus { border-color: var(--text-norm); }
        .login-content button { width: 100%; padding: 12px; background: transparent; color: var(--text-norm); border: 1px solid var(--text-norm); border-radius: 4px; font-family: inherit; font-size: 18px; cursor: pointer; transition: 0.2s; text-shadow: var(--bloom);}
        .login-content button:hover { background: var(--text-norm); color: var(--bg-window); }

        #app-view .mac-window { flex: 1; max-width: 1400px; }
        
        #terminal-wrapper { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 20px 20px 0 20px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }
        
        /* 强迫症福音：给固定头部增加右侧 10px 的 padding，完美抵消下方滚动条的宽度误差 */
        #boot-sequence { flex-shrink: 0; padding-right: 10px; }
        #live-logs { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
        
        /* Flexbox Log Line */
        .log-line { display: flex; justify-content: space-between; align-items: flex-start; margin: 2px 0; width: 100%; }
        .log-content { flex: 1; word-break: break-all; }
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
        
        .inv-ok { background: var(--log-info); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-fail { background: var(--log-err); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-wait { background: var(--log-warn); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        .inv-warn { background: var(--log-warn); color: var(--bg-window); padding: 0 4px; text-shadow: none; font-weight: bold;}
        
        .cmd-clickable { color: var(--cmd-col); padding: 0 4px; cursor: pointer; border: 1px solid var(--cmd-border); margin: 0 2px; transition: 0.1s;}
        .cmd-clickable:hover { background: var(--cmd-col); color: var(--bg-window); text-shadow: none;}
        
        /* System Ready Divider */
        .sys-divider { display: flex; align-items: center; width: 100%; margin: 15px 0 10px 0; color: var(--cmd-col); text-shadow: var(--bloom); opacity: 0.8;}
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
                <div class="mac-title">MAINFRAME_AUTH</div>
                <div class="mac-spacer"></div>
            </div>
            <div class="login-content">
                <h2>SYS_CONSOLE</h2>
                <input type="password" id="loginPass" placeholder="INPUT ROOT TOKEN..." autocomplete="off" onkeypress="if(event.key==='Enter') doLogin()">
                <button id="loginBtn" onclick="doLogin()">[ OVERRIDE ]</button>
            </div>
        </div>
    </div>

    <div id="app-view" class="hidden">
        <div class="mac-window">
            <div class="mac-header">
                <div class="mac-btns">
                    <div class="mac-btn btn-close" onclick="doLogout()" title="切断连接"></div>
                    <div class="mac-btn btn-min" onclick="toggleTheme()" title="滤镜切换"></div>
                    <div class="mac-btn btn-max breathing" title="系统内核运转中"></div>
                </div>
                <div class="mac-title">root@mainframe:~</div>
                <div class="mac-spacer"></div>
            </div>
            
            <div id="terminal-wrapper">
                <div id="boot-sequence"></div>
                <div id="live-logs"></div>
                <div id="typewriter-line"><span id="typewriter-text"></span><span class="cursor"></span></div>
            </div>
            
            <div id="input-area">
                <span id="cmd-prefix">root@mainframe:~#</span>
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

        let bootLogsRendered = false;
        let lastLogCount = 0;
        let typeQueue = [];
        let isTyping = false;

        function initTheme() {
            let saved = localStorage.getItem('bas_theme');
            if (!saved) {
                let h = new Date().getHours();
                saved = (h >= 6 && h < 18) ? 'light' : 'dark';
            }
            document.documentElement.setAttribute('data-theme', saved);
        }

        function toggleTheme() {
            let current = document.documentElement.getAttribute('data-theme');
            let next = (current === 'light') ? 'dark' : 'light';
            localStorage.setItem('bas_theme', next);
            document.documentElement.setAttribute('data-theme', next);
        }

        async function doLogin() {
            const pass = document.getElementById('loginPass').value.trim();
            if (!pass) return;
            
            const btn = document.getElementById('loginBtn');
            const origText = btn.innerText;
            btn.innerText = '[ VERIFYING... ]';
            
            try {
                const res = await fetch('/api/verify', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: pass })
                });
                
                if (res.status === 200) {
                    localStorage.setItem('bas_token', pass);
                    enterSystem();
                    btn.innerText = origText;
                } else {
                    btn.innerText = '[ CLEARANCE DENIED ]';
                    btn.style.color = 'var(--log-err)';
                    btn.style.borderColor = 'var(--log-err)';
                    setTimeout(() => {
                        btn.innerText = origText;
                        btn.style.color = '';
                        btn.style.borderColor = '';
                    }, 2000);
                }
            } catch(e) { 
                btn.innerText = '[ NET_PULSE_ERR ]';
                btn.style.color = 'var(--log-warn)';
                btn.style.borderColor = 'var(--log-warn)';
                setTimeout(() => { 
                    btn.innerText = origText; 
                    btn.style.color = '';
                    btn.style.borderColor = '';
                }, 2000);
            }
        }

        function doLogout() {
            localStorage.removeItem('bas_token');
            clearInterval(logInterval);
            bootLogsRendered = false;
            lastLogCount = 0;
            typeQueue = [];
            appView.className = 'hidden';
            loginView.className = 'active';
            document.getElementById('loginPass').value = '';
        }

        function enterSystem() {
            loginView.className = 'hidden';
            appView.className = 'active';
            document.getElementById('boot-sequence').innerHTML = '';
            liveLogsDiv.innerHTML = '';
            document.getElementById('typewriter-text').textContent = '';
            
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
            const token = localStorage.getItem('bas_token');
            if(!token) return doLogout();
            try {
                const res = await fetch('/api/logs', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ token: token })
                });
                if (res.status === 401) return doLogout();
                const data = await res.json();
                processLogStream(data.logs);
            } catch (e) {}
        }

        function processLogStream(logs) {
            let splitIndex = logs.findIndex(l => l.includes("终端面甲激活完成，全系统就绪"));
            if(splitIndex === -1) splitIndex = -1; 

            if (!bootLogsRendered && splitIndex !== -1) {
                let bootHtml = logs.slice(0, splitIndex + 1).map(formatLogHTML).join('');
                // 添加 System Ready 徽章分割线 (Hardcore IT Style)
                bootHtml += '<div class="sys-divider"><div class="line"></div><div class="badge">[ SYSTEM_READY ]</div><div class="line"></div></div>';
                document.getElementById('boot-sequence').innerHTML = bootHtml;
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
            if (log.includes('[FAIL]') || log.includes('[!!FATAL!!]')) cls = 'ERROR';
            
            let badgeHtml = '';
            let contentHtml = log;
            
            // 提取末尾的状态徽章用于 Flex 弹性对齐
            let badgeRegex = /\\[\\s*(OK|FAIL|WAIT|WARN)\\s*\\]/;
            let match = log.match(badgeRegex);
            if (match) {
                 let type = match[1];
                 let badgeCls = 'inv-' + type.toLowerCase();
                 let displayType = type === 'OK' ? ' OK ' : type;
                 badgeHtml = `<div class="log-badge"><span class="${badgeCls}">[${displayType}]</span></div>`;
                 contentHtml = log.replace(badgeRegex, '').trim();
            }
            
            contentHtml = contentHtml.replace(/(\\/(?:status|stop|start|restart|sap)\\b)/g, 
                    '<span class="cmd-clickable" onclick="copyToInput(\\'$1\\')">$1</span>');
                    
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
            function typeChar() {
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
                const token = localStorage.getItem('bas_token');
                if (!cmd || !token) return;
                
                const fakeLog = document.createElement('div');
                fakeLog.className = 'log-line INFO';
                fakeLog.innerHTML = `<div class="log-content">[${new Date().toISOString().slice(0,19).replace('T', ' ')}] &gt; COMMAND INPUT: <span class="cmd-clickable">${cmd}</span></div>`;
                fakeLog.style.color = 'var(--cmd-col)';
                liveLogsDiv.appendChild(fakeLog);
                if (autoScroll) liveLogsDiv.scrollTop = liveLogsDiv.scrollHeight;

                cmdInput.value = '';
                
                try {
                    const res = await fetch(`/api/command`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ token: token, command: cmd })
                    });
                    if (res.status === 401) doLogout();
                } catch (err) {}
            }
        });

        initTheme();
        if (localStorage.getItem('bas_token')) enterSystem();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return HTML_TEMPLATE

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
        logger.warning(f"<HUD_UI> 拦截非法语法流: {cmd_str} (须以 / 起始) [WARN]")
        return jsonify({"error": "Invalid command format"}), 400
        
    parts = cmd_str.split()
    command = parts[0].replace("/", "").lower()
    target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    
    logger.info(f"<HUD_UI> 权限提权写入(OVERRIDE): {cmd_str} [ OK ]")
    
    if command in ['start', 'stop', 'restart']:
        bot_action_runner(command.upper(), target_id)
        return jsonify({"status": "Command dispatched to queue"})
        
    elif command == 'status':
        target_accounts = [acc for acc in ACCOUNTS if acc['id'] == target_id] if target_id else ACCOUNTS
        if target_id and not target_accounts:
            logger.error(f"<HUD_UI> 数据库未能匹配到编号 {target_id} 的容器节点 [FAIL]")
            return jsonify({"status": "Not found"})
        
        def _check_web():
            sys_status = "■ 红色警戒 (强制指令阻塞中)" if system_busy_event.is_set() else "■ 绿色安定 (全局锁已释放)"
            logger.info(f"<SYS_OP> 集群算力容器状态追踪: {sys_status}")
            for acc in target_accounts:
                success, ws_id, status = SAPController.get_workspace_info(acc)
                logger.info(f"<SYS_OP> 节点 {acc['id']} ({acc['email']}) -> 容器物理态: {status}")
                
        threading.Thread(target=_check_web).start()
        return jsonify({"status": "Checking status"})
        
    elif command == 'sap':
        logger.info("--------- 主机集群控制终端 ---------")
        logger.info("❖ /status   ( 节点运行状态追踪 )")
        logger.info("❖ /stop     ( 强制冻结算力容器 )")
        logger.info("❖ /start    ( 唤醒挂起算力容器 )")
        logger.info("❖ /restart  ( 硬重置数据流链路 )")
        return jsonify({"status": "Help displayed"})
    
    else:
        logger.warning(f"<HUD_UI> 滤除未知战术指令: {cmd_str} [WARN]")
        return jsonify({"error": "Unknown command"}), 400

# ==========================================
# 9. 启动引导区
# ==========================================
def start_bot_polling():
    logger.info("<SYS_INIT> 外部系统通讯网络连线补完。 [ OK ]")
    bot.infinity_polling()

if __name__ == '__main__':
    logger.info("======================================================================")
    logger.info(f"<SYS_INIT> 核心调度模块启动！成功挂载 {len(ACCOUNTS)} 个节点参数。 [ OK ]")
    
    if not ACCOUNTS:
        logger.error("[!!FATAL!!] 核心节点参数缺失，系统抛出异常并自我锁定！ [FAIL]")
        sys.exit(1)
        
    scheduler = BackgroundScheduler()
    for acc in ACCOUNTS:
        scheduler.add_job(lambda a=acc: async_task_runner("KEEPALIVE", a), trigger='cron', minute=acc['joba_min'], id=f"job_keepalive_{acc['id']}")
        scheduler.add_job(lambda a=acc: async_task_runner("RESTART", a), trigger='cron', hour=acc['jobb_hrs'], minute=acc['jobb_min'], id=f"job_restart_{acc['id']}")
        
        if acc.get('tunnel_url'):
            scheduler.add_job(lambda a=acc: tunnel_health_check(a), trigger='interval', minutes=1, id=f"job_health_{acc['id']}")
            logger.info(f"<SCHEDULR> 节点 {acc['id']} 守护进程注入 [ KEEP_ALIVE:每小时{acc['joba_min']}分 | REBOOT:{acc['jobb_hrs']}时{acc['jobb_min']}分 | PROBE:ON ] [ OK ]")
        else:
            logger.info(f"<SCHEDULR> 节点 {acc['id']} 守护进程注入 [ KEEP_ALIVE:每小时{acc['joba_min']}分 | REBOOT:{acc['jobb_hrs']}时{acc['jobb_min']}分 | PROBE:OFF ] [ OK ]")

    scheduler.add_job(clean_probe_logs, trigger='interval', hours=1, id='job_clean_logs')

    scheduler.start()

    if bot:
        threading.Thread(target=start_bot_polling, daemon=True).start()

    logger.info("<HUD_UI> 终端面甲激活完成，全系统就绪。 [ OK ]")
    
    for acc in ACCOUNTS:
        if acc.get('tunnel_url'):
            threading.Thread(target=tunnel_health_check, args=(acc,)).start()

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
