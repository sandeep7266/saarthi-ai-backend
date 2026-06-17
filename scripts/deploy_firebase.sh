#!/bin/bash
# scripts/deploy_firebase.sh
# Deploys Firestore rules + indexes + Storage rules to Firebase
# Run once after Firebase project is created

set -e

echo "Saarthi-AI Firebase Deployment"
echo "================================"

# Check firebase CLI installed
if ! command -v firebase &> /dev/null; then
  echo "Installing Firebase CLI..."
  npm install -g firebase-tools
fi

# Check logged in
echo "Logging in to Firebase..."
firebase login --no-localhost

# Deploy Firestore security rules
echo ""
echo "1. Deploying Firestore rules..."
firebase deploy --only firestore:rules

# Deploy Firestore indexes
echo ""
echo "2. Deploying Firestore indexes..."
firebase deploy --only firestore:indexes

# Deploy Storage rules
echo ""
echo "3. Deploying Storage rules..."
firebase deploy --only storage

echo ""
echo "All Firebase resources deployed!"
echo ""
echo "NEXT STEPS:"
echo "  1. Go to Firebase Console -> Firestore -> Indexes"
echo "  2. Wait for all indexes to finish building (takes 2-5 min)"
echo "  3. Deploy your backend to Railway"
