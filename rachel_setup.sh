#!/bin/bash
# ─────────────────────────────────────────────
# Rachel Setup Script — ClientCo Property Management
#
# SAVE TO: ~/Desktop/Rachel/rachel_setup.sh
#
# FIRST TIME ONLY — store passwords in Mac Keychain:
#   security add-generic-password -a "rachel_appfolio" -s "rachel_appfolio" -w "PASSWORD"
#   security add-generic-password -a "rachel_anthropic" -s "rachel_anthropic" -w "API_KEY"
#
# After that, just run:
#   source ~/Desktop/Rachel/rachel_setup.sh
# ─────────────────────────────────────────────

export RACHEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export RACHEL_EXPORTS_DIR="$RACHEL_DIR/exports"
mkdir -p "$RACHEL_EXPORTS_DIR"

echo "Setting up Rachel environment..."

# ── Fixed values ──
export APPFOLIO_URL="https://clientco.appfolio.com"
export APPFOLIO_EMAIL="rachel@clientco.example"
export RACHEL_DB_PATH="/tmp/rachel_data.db"
export MANAGER_EMAIL="manager@clientco.example"
export RACHEL_FROM_EMAIL="rachel@clientco.example"
export GOOGLE_SERVICE_ACCOUNT_JSON="$RACHEL_DIR/rachel-client-co-272e82c3aad6.json"

# Release-candidate safety defaults. Operational and consequential-write
# switches must be enabled explicitly for a separately approved QA or launch.
export RACHEL_ENABLED="false"
export RACHEL_TEST_MODE="true"
export RACHEL_OWNER_QA_MODE="false"
export RACHEL_APPFOLIO_WRITES_ENABLED="false"
export RACHEL_APPFOLIO_MESSAGING_ENABLED="false"
export RACHEL_APPFOLIO_QA_ENABLED="false"
export RACHEL_APPFOLIO_COMMUNICATION_SEND_QA_ENABLED="false"
export RACHEL_SMS_ENABLED="false"
export RACHEL_LEASE_EMAIL_DRY_RUN="true"
export RACHEL_PROPERTY_FACT_SHEET_WRITES_ENABLED="false"

# ── Read sensitive values from Mac Keychain ──
# (stored once with: security add-generic-password ...)
export APPFOLIO_PASSWORD="$(security find-generic-password -a "rachel_appfolio" -s "rachel_appfolio" -w 2>/dev/null)"
export ANTHROPIC_API_KEY="$(security find-generic-password -a "rachel_anthropic" -s "rachel_anthropic" -w 2>/dev/null)"

# ── Check if keys loaded successfully ──
if [ -z "$APPFOLIO_PASSWORD" ]; then
    echo "⚠️  AppFolio password not in Keychain — enter manually:"
    read -s APPFOLIO_PASSWORD && export APPFOLIO_PASSWORD
else
    echo "✅ AppFolio password loaded from Keychain"
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "⚠️  Anthropic key not in Keychain — enter manually:"
    read -s ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
else
    echo "✅ Anthropic key loaded from Keychain"
fi


cd "$RACHEL_DIR"

echo ""
echo "─────────────────────────────────────────────"
echo "✅ Rachel environment ready!"
echo "─────────────────────────────────────────────"
echo ""
echo "Commands:"
echo "  python3 rachel_scraper_v2.py                   — monthly scrape"
echo "  python3 rachel_lease_tracker_v2.1.py           — email Manager alerts"
echo "  python3 rachel_lease_tracker_v2.1.py --digest  — Monday digest"
echo ""
echo "Rachel role: Assistant Property Manager"
echo "Exports:     $RACHEL_EXPORTS_DIR"
echo "Database:    $RACHEL_DB_PATH"
echo ""

# Gmail for auto-2FA
export RACHEL_GMAIL_USER="rachel.clientco@example.com"
export RACHEL_GMAIL_PASSWORD="$(security find-generic-password -a "rachel_gmail" -s "rachel_gmail" -w 2>/dev/null)"
if [ -z "$RACHEL_GMAIL_PASSWORD" ]; then
    echo "⚠️  Gmail app password not in Keychain"
else
    echo "✅ Gmail app password loaded from Keychain"
fi

# Rachel's own inbox for reading Manager replies
export RACHEL_EMAIL_USER="rachel@clientco.example"
export RACHEL_EMAIL_PASSWORD="$(security find-generic-password -a "rachel_email" -s "rachel_email" -w 2>/dev/null)"
if [ -z "$RACHEL_EMAIL_PASSWORD" ]; then
    echo "⚠️  Rachel email password not in Keychain"
else
    echo "✅ Rachel email password loaded from Keychain"
fi
