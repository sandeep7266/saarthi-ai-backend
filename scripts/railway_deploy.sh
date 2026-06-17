#!/bin/bash
# scripts/railway_deploy.sh
# Step-by-step Railway deployment with env var setup
set -e

echo "Saarthi-AI — Railway Deployment Script"
echo "======================================="
echo ""

# 1. Check Railway CLI
if ! command -v railway &> /dev/null; then
  echo "Installing Railway CLI..."
  npm install -g @railway/cli
fi

# 2. Login
echo "Step 1: Login to Railway"
railway login

# 3. Init project
echo ""
echo "Step 2: Initialize Railway project"
echo "Select 'Empty Project' when prompted"
railway init

# 4. Set environment variables
echo ""
echo "Step 3: Setting environment variables..."
echo "You will be prompted for each value."
echo "(Get these from your .env file)"
echo ""

read -p "FIREBASE_STORAGE_BUCKET: " FB_BUCKET
read -p "JWT_SECRET (64 char random string): " JWT_SECRET
read -p "RAZORPAY_KEY_ID: " RZ_KEY_ID
read -p "RAZORPAY_KEY_SECRET: " RZ_KEY_SECRET
read -p "RAZORPAY_WEBHOOK_SECRET: " RZ_WEBHOOK
read -p "META_ACCESS_TOKEN: " META_TOKEN
read -p "WHATSAPP_VERIFY_TOKEN: " WA_VERIFY
read -p "GEMINI_API_KEY: " GEMINI_KEY
read -p "FCM_SERVER_KEY: " FCM_KEY
read -p "CRON_SECRET (random string): " CRON_SECRET
read -p "APP_BASE_URL (e.g. https://saarthi-ai.in): " APP_URL

railway variables set \
  FIREBASE_SERVICE_ACCOUNT_PATH="/app/serviceAccountKey.json" \
  FIREBASE_STORAGE_BUCKET="$FB_BUCKET" \
  JWT_SECRET="$JWT_SECRET" \
  JWT_TTL_HOURS="12" \
  RAZORPAY_KEY_ID="$RZ_KEY_ID" \
  RAZORPAY_KEY_SECRET="$RZ_KEY_SECRET" \
  RAZORPAY_WEBHOOK_SECRET="$RZ_WEBHOOK" \
  META_ACCESS_TOKEN="$META_TOKEN" \
  META_API_VERSION="v19.0" \
  WHATSAPP_VERIFY_TOKEN="$WA_VERIFY" \
  GEMINI_API_KEY="$GEMINI_KEY" \
  FCM_SERVER_KEY="$FCM_KEY" \
  APP_BASE_URL="$APP_URL" \
  CRON_SECRET="$CRON_SECRET" \
  ALLOWED_ORIGINS="$APP_URL,https://saarthi-ai.in"

echo ""
echo "Step 4: IMPORTANT — Upload serviceAccountKey.json"
echo "Railway does not support file uploads directly."
echo "Two options:"
echo ""
echo "Option A (Recommended): Base64 encode and store as env var:"
echo "  1. Run: base64 serviceAccountKey.json | tr -d '\n'"
echo "  2. Copy the output"
echo "  3. railway variables set FIREBASE_SERVICE_ACCOUNT_BASE64='<paste here>'"
echo "  4. Add this to database.py startup (see SETUP_GUIDE.md)"
echo ""
echo "Option B: Use Railway Volume (paid plan)"
echo ""

read -p "Press Enter when serviceAccountKey.json is handled..."

# 5. Deploy
echo ""
echo "Step 5: Deploying to Railway..."
railway up

echo ""
echo "Step 6: Get your deployment URL"
railway domain

echo ""
echo "Deployment complete!"
echo "Your API is live. Update APP_BASE_URL if the Railway URL differs."
