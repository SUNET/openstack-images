#!/bin/bash
# Apache2 wrapper that generates OIDC secrets config before starting Apache
# This script replaces the standard apache2 binary in keystone-api.sh

set -e

OIDC_SECRETS_DIR="${OIDC_SECRETS_DIR:-/etc/keystone/oidc}"
OIDC_CONFIG_FILE="${OIDC_CONFIG_FILE:-/tmp/oidc-secrets.conf}"

# Generate OIDC secrets config from mounted secret files
generate_oidc_config() {
    if [ -d "$OIDC_SECRETS_DIR" ]; then
        echo "# Auto-generated OIDC secrets config - $(date)" > "$OIDC_CONFIG_FILE"
        
        if [ -f "$OIDC_SECRETS_DIR/client_secret" ]; then
            CLIENT_SECRET=$(cat "$OIDC_SECRETS_DIR/client_secret" | tr -d '\n')
            echo "OIDCClientSecret \"$CLIENT_SECRET\"" >> "$OIDC_CONFIG_FILE"
            echo "Generated OIDCClientSecret from secret file"
        fi
        
        if [ -f "$OIDC_SECRETS_DIR/crypto_passphrase" ]; then
            CRYPTO_PASSPHRASE=$(cat "$OIDC_SECRETS_DIR/crypto_passphrase" | tr -d '\n')
            echo "OIDCCryptoPassphrase \"$CRYPTO_PASSPHRASE\"" >> "$OIDC_CONFIG_FILE"
            echo "Generated OIDCCryptoPassphrase from secret file"
        fi
        
        chmod 600 "$OIDC_CONFIG_FILE"
    else
        echo "OIDC secrets directory not found at $OIDC_SECRETS_DIR, creating empty config"
        # Create empty file so IncludeOptional doesn't cause issues
        touch "$OIDC_CONFIG_FILE"
    fi
}

# Generate OIDC config
generate_oidc_config

# Execute the real apache2 binary with all arguments
exec /usr/sbin/apache2 "$@"
