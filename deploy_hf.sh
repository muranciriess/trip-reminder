#!/bin/bash
set -e

SPACE_NAME="${1:-trip-reminder}"
HF_USER=$(huggingface-cli whoami 2>/dev/null | head -1)

if [ -z "$HF_USER" ]; then
    echo "请先登录 Hugging Face："
    echo "  huggingface-cli login"
    echo ""
    echo "登录后重新运行："
    echo "  bash deploy_hf.sh"
    exit 1
fi

REPO_ID="$HF_USER/$SPACE_NAME"
DEPLOY_DIR=$(mktemp -d)

echo "==> 准备部署文件..."
cp app.py config.py wechat_bot.py requirements.txt supervisord.conf "$DEPLOY_DIR/"
cp -r templates "$DEPLOY_DIR/"
cp Dockerfile.hf "$DEPLOY_DIR/Dockerfile"

cat > "$DEPLOY_DIR/README.md" << 'FRONTMATTER'
---
title: Trip Reminder
emoji: "\U0001F4CB"
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---
FRONTMATTER

echo "==> 创建 HF Space: $REPO_ID ..."
huggingface-cli repo create "$SPACE_NAME" --type space -y 2>/dev/null || true

echo "==> 上传文件..."
cd "$DEPLOY_DIR"
git init
git checkout -b main
git add -A
git commit -m "deploy trip reminder"
git remote add origin "https://huggingface.co/spaces/$REPO_ID"
git push -f origin main

echo ""
echo "=========================================="
echo "  部署成功！"
echo "=========================================="
echo ""
echo "  Space 地址: https://huggingface.co/spaces/$REPO_ID"
echo "  应用地址:   https://$HF_USER-$SPACE_NAME.hf.space"
echo ""
echo "  等待约 3-5 分钟构建完成后："
echo "  1. 扫码登录: https://$HF_USER-$SPACE_NAME.hf.space/bot/login?token=trip-bot-2026"
echo "  2. 管理后台: https://$HF_USER-$SPACE_NAME.hf.space/admin"
echo "  3. 学员页面: https://$HF_USER-$SPACE_NAME.hf.space/student"
echo ""

rm -rf "$DEPLOY_DIR"
