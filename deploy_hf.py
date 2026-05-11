#!/usr/bin/env python3
"""Deploy to Hugging Face Spaces"""
import os
import sys

try:
    from huggingface_hub import HfApi, create_repo
except ImportError:
    print("Installing huggingface_hub...")
    os.system(f"{sys.executable} -m pip install --user huggingface_hub")
    from huggingface_hub import HfApi, create_repo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPACE_NAME = "trip-reminder"

token = input("\n请粘贴你的 Hugging Face Access Token: ").strip()
if not token:
    print("Token 不能为空")
    sys.exit(1)

api = HfApi(token=token)
user = api.whoami()
username = user["name"]
repo_id = f"{username}/{SPACE_NAME}"

print(f"\n==> 用户: {username}")
print(f"==> 创建 Space: {repo_id} ...")

try:
    create_repo(repo_id, repo_type="space", space_sdk="docker", token=token, exist_ok=True)
except Exception as e:
    print(f"创建 Space 失败: {e}")
    sys.exit(1)

files_to_upload = [
    ("app.py", "app.py"),
    ("config.py", "config.py"),
    ("wechat_bot.py", "wechat_bot.py"),
    ("requirements.txt", "requirements.txt"),
    ("supervisord.conf", "supervisord.conf"),
    ("templates/index.html", "templates/index.html"),
    ("templates/login.html", "templates/login.html"),
    ("templates/admin.html", "templates/admin.html"),
    ("templates/admin_daily.html", "templates/admin_daily.html"),
    ("templates/student.html", "templates/student.html"),
]

readme_content = """---
title: Trip Reminder
emoji: "\U0001F4CB"
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---
"""

dockerfile_src = os.path.join(BASE_DIR, "Dockerfile.hf")

print("==> 上传文件...")

api.upload_file(
    path_or_fileobj=readme_content.encode(),
    path_in_repo="README.md",
    repo_id=repo_id,
    repo_type="space",
    token=token,
)

api.upload_file(
    path_or_fileobj=dockerfile_src,
    path_in_repo="Dockerfile",
    repo_id=repo_id,
    repo_type="space",
    token=token,
)

for local_name, repo_name in files_to_upload:
    local_path = os.path.join(BASE_DIR, local_name)
    if os.path.exists(local_path):
        api.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=repo_name,
            repo_id=repo_id,
            repo_type="space",
            token=token,
        )
        print(f"   ✓ {repo_name}")

space_url = f"https://huggingface.co/spaces/{repo_id}"
app_url = f"https://{username}-{SPACE_NAME}.hf.space"

print(f"""
==========================================
  部署成功！
==========================================

  Space 页面: {space_url}
  应用地址:   {app_url}

  等待约 3-5 分钟构建完成后：
  1. 扫码登录: {app_url}/bot/login?token=trip-bot-2026
  2. 管理后台: {app_url}/admin
  3. 学员页面: {app_url}/student  (分享给别人的链接)

  设置保活 (防休眠):
  1. 打开 https://uptimerobot.com 注册免费账号
  2. 添加 HTTP 监控, URL 填: {app_url}/student
  3. 间隔选 5 分钟
""")
