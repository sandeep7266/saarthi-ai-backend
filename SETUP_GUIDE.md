# Saarthi-AI — Complete Setup Guide

## STEP 1A: Firebase Setup (15 min)

1. Go to https://console.firebase.google.com
2. Click "Add Project" → Name: "saarthi-ai"
3. Enable Google Analytics → Continue

### Firestore Setup
4. Left menu → "Firestore Database" → "Create Database"
5. Select "Production mode" → Choose region: "asia-south1" (Mumbai)
6. Click "Enable"

### Storage Setup  
7. Left menu → "Storage" → "Get Started"
8. Accept rules → Same region: asia-south1

### Service Account Key
9. Project Settings (gear icon) → "Service accounts" tab
10. Click "Generate new private key" → Download JSON
11. RENAME the file to: serviceAccountKey.json
12. PLACE it in the saarthi-ai/ backend root folder
    ⚠️ NEVER commit this file to Git. It's in .gitignore.

### Get Storage Bucket name
13. Storage → Settings → Copy the bucket name (format: saarthi-ai.appspot.com)
14. Paste it in your .env file as FIREBASE_STORAGE_BUCKET

---

## STEP 1B: Meta WhatsApp Cloud API (1-2 weeks for approval)

### Create Meta Business Account
1. Go to https://business.facebook.com
2. Create a Business Account with your real business details
3. Go to https://developers.facebook.com → "My Apps" → "Create App"
4. Select "Business" type → Fill details

### Add WhatsApp Product
5. In your app dashboard → "Add Products" → "WhatsApp" → Setup
6. You'll get a TEST phone number initially (good for development)

### Get your credentials
7. WhatsApp → API Setup → Copy:
   - "Phone Number ID" → this is whatsapp_phone_id in Firestore
   - "WhatsApp Business Account ID"
   - "Temporary access token" (generate a permanent one in production)
8. Paste these in your .env:
   META_ACCESS_TOKEN=your_token_here

### Configure Webhook
9. WhatsApp → Configuration → Webhook:
   - Callback URL: https://your-domain.com/api/v1/webhook/whatsapp
   - Verify Token: saarthi_verify_token (must match WHATSAPP_VERIFY_TOKEN in .env)
10. Subscribe to: "messages" field

### Apply for Production Access
11. App Review → Request "whatsapp_business_messaging" permission
12. Submit business verification documents
⏳ This takes 1-2 weeks. Start this FIRST.

---

## STEP 1C: Razorpay Account (2-3 days for KYC)

1. Go to https://dashboard.razorpay.com/signup
2. Sign up with your business email
3. Complete KYC:
   - PAN Card
   - Business registration (GST certificate or udyam registration)
   - Bank account details
4. Once approved, go to Settings → API Keys → "Generate Key"
5. Copy:
   - Key ID → RAZORPAY_KEY_ID in .env
   - Key Secret → RAZORPAY_KEY_SECRET in .env

### Configure Webhook
6. Settings → Webhooks → "Add New Webhook"
7. Webhook URL: https://your-domain.com/api/v1/payments/razorpay-webhook
8. Select events: payment.captured, payment_link.paid
9. Copy the webhook secret → RAZORPAY_WEBHOOK_SECRET in .env

---

## STEP 1D: Google Gemini API Key (5 min)

1. Go to https://aistudio.google.com
2. Click "Get API Key" → "Create API Key"
3. Select your Google Cloud project (or create new)
4. Copy the key → GEMINI_API_KEY in .env

---

## STEP 1E: Railway.app Deployment Account (10 min)

1. Go to https://railway.app → Sign up with GitHub
2. Connect your GitHub account
3. You'll deploy here in Step 7

---

## YOUR .env FILE (fill all values)

```env
# Firebase
FIREBASE_SERVICE_ACCOUNT_PATH=serviceAccountKey.json
FIREBASE_STORAGE_BUCKET=saarthi-ai.appspot.com   # ← change this

# JWT (generate a random 64-char string)
JWT_SECRET=GENERATE_WITH: openssl rand -hex 32
JWT_TTL_HOURS=12

# Razorpay
RAZORPAY_KEY_ID=rzp_live_xxxx
RAZORPAY_KEY_SECRET=xxxx
RAZORPAY_WEBHOOK_SECRET=xxxx

# Meta WhatsApp
META_ACCESS_TOKEN=xxxx
META_API_VERSION=v19.0
WHATSAPP_VERIFY_TOKEN=saarthi_verify_token

# Gemini
GEMINI_API_KEY=xxxx

# App
APP_BASE_URL=https://your-domain.com
ALLOWED_ORIGINS=https://your-domain.com
CRON_SECRET=GENERATE_WITH: openssl rand -hex 32
```

## CHECKLIST — Before telling me "accounts ready":
- [ ] serviceAccountKey.json downloaded and placed in backend folder
- [ ] Firebase Storage bucket name noted
- [ ] Razorpay Key ID + Secret + Webhook Secret obtained
- [ ] Gemini API key obtained  
- [ ] Meta access token obtained (test token is fine to start)
- [ ] .env file filled with all real values
