# Saarthi-AI — Complete Deployment Guide

## Prerequisites (Do these first)
- [ ] Firebase project created (see SETUP_GUIDE.md Step 1A)
- [ ] Razorpay account approved with live keys (Step 1C)
- [ ] Meta WhatsApp API approved (Step 1B)
- [ ] Gemini API key obtained (Step 1D)
- [ ] Railway account created (Step 1E)
- [ ] GitHub repository created and code pushed

---

## Step 1: Firebase Deploy (rules + indexes)

```bash
# Install Firebase CLI
npm install -g firebase-tools

# Login
firebase login

# Select your project
firebase use --add

# Deploy rules and indexes
bash scripts/deploy_firebase.sh
```

Wait 2–5 minutes for indexes to build in Firebase Console.

---

## Step 2: Railway Backend Deploy

### 2A. Encode serviceAccountKey.json
```bash
# On Mac/Linux
base64 serviceAccountKey.json | tr -d '\n'
# Copy the output — you'll need it in step 2C
```

### 2B. Push code to GitHub
```bash
git init
git add .
git commit -m "Initial Saarthi-AI deployment"
git remote add origin https://github.com/YOUR_USERNAME/saarthi-ai-backend.git
git push -u origin main
```

### 2C. Deploy to Railway (interactive)
```bash
bash scripts/railway_deploy.sh
```
Or manually:
```bash
npm install -g @railway/cli
railway login
railway init
railway variables set JWT_SECRET="$(openssl rand -hex 32)"
railway variables set FIREBASE_SERVICE_ACCOUNT_BASE64="PASTE_BASE64_HERE"
# ... set all other vars from .env.example
railway up
```

### 2D. Get your Railway URL
```bash
railway domain
# Example output: saarthi-ai-backend-production.up.railway.app
```

### 2E. Test health check
```bash
curl https://YOUR_RAILWAY_URL.railway.app/health
# Expected: {"status":"healthy","firebase":"connected","scheduler":"running"}
```

---

## Step 3: Configure Meta Webhook

1. Go to developers.facebook.com → Your App → WhatsApp → Configuration
2. Webhook URL: `https://YOUR_RAILWAY_URL/api/v1/webhook/whatsapp`
3. Verify Token: (same as `WHATSAPP_VERIFY_TOKEN` in your .env)
4. Click Verify — should succeed immediately
5. Subscribe to field: `messages`

---

## Step 4: Setup Cron Job

```bash
# Set your values
export SAARTHI_API_URL="https://YOUR_RAILWAY_URL.railway.app"
export CRON_SECRET="your_cron_secret"

# Install crontab
bash scripts/setup_cron.sh

# Test it works
curl -X POST "$SAARTHI_API_URL/api/v1/cron/run-daily-sync" \
  -H "X-Cron-Secret: $CRON_SECRET"
```

---

## Step 5: GitHub Actions CI/CD

Add these secrets in GitHub → Settings → Secrets → Actions:

| Secret | Value | Where to get |
|--------|-------|--------------|
| `RAILWAY_TOKEN` | Railway API token | railway.app → Account → Tokens |

After adding secrets, every push to `main` will auto-deploy.

---

## Step 6: Create First Admin User

```bash
# Use the API to create the first admin for your first client
curl -X POST https://YOUR_RAILWAY_URL/api/v1/onboard/create-pending-vendor \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Test Salon",
    "owner_name": "Your Name",
    "owner_phone": "+919876543210",
    "owner_email": "you@email.com",
    "business_type": "salon",
    "city": "Ranchi",
    "address": "Test Address",
    "plan": "basic",
    "billing_cycle": "monthly",
    "whatsapp_phone_id": "YOUR_META_PHONE_NUMBER_ID"
  }'
# Pay the Razorpay link → vendor auto-activates
# Then create admin user via POST /api/v1/auth/create-staff
```

---

## Step 7: Flutter App Build

```bash
cd saarthi-flutter

# Add google-services.json (from Firebase Console)
cp ~/Downloads/google-services.json android/app/

# Build APK
flutter build apk --release \
  --dart-define=API_BASE_URL=https://YOUR_RAILWAY_URL.railway.app

# APK location
ls build/app/outputs/flutter-apk/app-release.apk

# Install on Android device
adb install build/app/outputs/flutter-apk/app-release.apk
```

---

## Deployment Checklist

### Backend
- [ ] Firebase rules deployed
- [ ] Firestore indexes built (green in console)
- [ ] Railway service running (health check = healthy)
- [ ] All env vars set in Railway
- [ ] Meta webhook verified
- [ ] Razorpay webhooks pointing to Railway URL
- [ ] Cron job running (test manually)

### Flutter App
- [ ] `google-services.json` placed in `android/app/`
- [ ] `API_BASE_URL` set to Railway URL
- [ ] APK built and tested on device
- [ ] FCM push notifications working
- [ ] Login with test credentials works
- [ ] Hard-lock appears for expired tenant
- [ ] Grace period banner appears for grace tenant

### First Client Onboarding
- [ ] Vendor registered via landing page
- [ ] Payment completed
- [ ] WhatsApp bot responding on Meta number
- [ ] Admin login works in Flutter app
- [ ] Slots created via Slot Management screen
- [ ] Services added via Services screen
- [ ] Test booking via WhatsApp end-to-end
