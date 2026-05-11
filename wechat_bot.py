import requests
import json
import os

BOT_BASE_URL = os.environ.get('BOT_BASE_URL', 'http://localhost:3001')
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'trip-bot-2026')
BOT_LOGIN_PREFIX = os.environ.get('BOT_LOGIN_PREFIX', '')
_target_group = None


def get_status():
    try:
        resp = requests.get(f"{BOT_BASE_URL}/healthz", params={"token": BOT_TOKEN}, timeout=5)
        healthy = resp.text.strip() == "healthy"
    except Exception:
        healthy = False

    return {
        'logged_in': healthy,
        'target_group': _target_group,
        'login_url': f"{BOT_LOGIN_PREFIX or BOT_BASE_URL}/login?token={BOT_TOKEN}",
    }


def get_groups():
    return []


def set_target_group(group_name):
    global _target_group
    _target_group = group_name


def send_to_group(message):
    if not _target_group:
        return {'status': 'error', 'reason': '未设置目标群'}

    try:
        resp = requests.post(
            f"{BOT_BASE_URL}/webhook/msg/v2",
            params={"token": BOT_TOKEN},
            json={
                "to": _target_group,
                "isRoom": True,
                "data": {"type": "text", "content": message}
            },
            timeout=15
        )
        result = resp.json()
        if result.get('success'):
            return {'status': 'success'}
        else:
            return {'status': 'error', 'reason': result.get('message', '发送失败')}
    except Exception as e:
        return {'status': 'error', 'reason': str(e)}


def send_to_person(name, message):
    try:
        resp = requests.post(
            f"{BOT_BASE_URL}/webhook/msg/v2",
            params={"token": BOT_TOKEN},
            json={
                "to": name,
                "isRoom": False,
                "data": {"type": "text", "content": message}
            },
            timeout=15
        )
        return resp.json()
    except Exception as e:
        return {'success': False, 'message': str(e)}


def start_bot():
    return {'status': 'ok', 'login_url': f"{BOT_BASE_URL}/login?token={BOT_TOKEN}"}


def stop_bot():
    global _target_group
    _target_group = None
    return {'status': 'stopped'}


def register_message_handler(handler):
    pass
