#!/usr/bin/env bash
# Generate a self-signed code-signing certificate for Leap Monitor and
# import it into the user's login keychain.
#
# The Monitor app is then signed with this cert during every build.
# Because the certificate is stable across rebuilds, the designated
# requirement embedded in the bundle's signature is byte-identical on
# every install/update — and macOS TCC preserves the Accessibility
# grant across updates (it keys on the designated requirement, not the
# binary's cdhash).
#
# Idempotent: if the "Leap Self-Signed" cert is already in the user's
# login keychain, this script exits 0 without re-generating.  Safe to
# run multiple times — both via the Makefile target and directly.
#
# Usage: leap-codesign-setup.sh

unset PYTHONHOME PYTHONPATH VIRTUAL_ENV

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

CERT_NAME="Leap Self-Signed"
LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if [ ! -f "$LOGIN_KEYCHAIN" ]; then
    echo -e "${RED}✗ Login keychain not found at $LOGIN_KEYCHAIN${NC}" >&2
    exit 1
fi

# Idempotency guard.  The Makefile's .gen-codesign-cert target already
# gates this script behind a `find-certificate` check, but the script
# could also be invoked directly (e.g., during manual debugging) — and
# without this guard, every direct invocation would add a fresh cert
# alongside the existing one.  `codesign --sign "Leap Self-Signed"`
# then becomes non-deterministic about which cert it picks, and any
# bundles signed with the "wrong" cert won't match the TCC entry that
# was stored using the other cert's SHA1.
if security find-certificate -c "$CERT_NAME" "$LOGIN_KEYCHAIN" >/dev/null 2>&1; then
    EXISTING_SHA1=$(security find-certificate -c "$CERT_NAME" -p "$LOGIN_KEYCHAIN" \
        | openssl x509 -noout -fingerprint -sha1 \
        | sed -e 's/.*Fingerprint=//' -e 's/://g')
    echo -e "${GREEN}✓ '$CERT_NAME' cert already in keychain (SHA1 $EXISTING_SHA1) — skipping generation.${NC}"
    exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"

# Private key
openssl genrsa -out leap.key 2048 2>/dev/null

# Self-signed cert with codeSigning EKU.  No subjectKeyIdentifier —
# macOS Security framework computes its own kpkh from the public key
# bits, and an explicit SKI confuses cert/key pairing on import.
cat > cert.conf <<'EOF'
[req]
distinguished_name = req_dn
x509_extensions = v3_req
prompt = no

[req_dn]
CN = Leap Self-Signed

[v3_req]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

openssl req -new -x509 -key leap.key -out leap.crt -days 3650 -config cert.conf 2>/dev/null

# Bundle into PKCS12.  macOS Security framework can only read PKCS12
# files using the legacy PBE-SHA1-3DES algorithm, NOT OpenSSL 3's new
# defaults (SHA256 HMAC + AES-256).  How we ask for the legacy format
# depends on which openssl is on the user's PATH:
#
#   OpenSSL 3.x (brew, MacPorts)  → needs explicit `-legacy` flag
#   LibreSSL    (Apple's stock)   → no `-legacy` flag exists; the
#                                   legacy algorithm is the default
#
# So we probe the flag's presence rather than hardcoding it.  Empty
# passwords also break MAC verification on import, so use a short
# per-run password.
# Bash 3.2 (macOS default) crashes on empty-array expansion under
# `set -u`, so we use a plain string with word-splitting instead of an
# array — only one flag is ever needed here.
PKCS12_LEGACY=""
if openssl pkcs12 -help 2>&1 | grep -q -- "-legacy"; then
    PKCS12_LEGACY="-legacy"
fi
PW="leap-setup-$$"
openssl pkcs12 -export \
    -inkey leap.key -in leap.crt \
    -out leap.p12 \
    -name "$CERT_NAME" \
    -passout "pass:$PW" \
    $PKCS12_LEGACY 2>/dev/null

# Import into login keychain.  -T flags add codesign + security to the
# private key's ACL, so codesign can use the key without a "allow
# access?" dialog on every signing.
security import leap.p12 \
    -k "$LOGIN_KEYCHAIN" \
    -P "$PW" \
    -T /usr/bin/codesign \
    -T /usr/bin/security \
    >/dev/null 2>&1

# Sanity-check: cert is now visible by name.  (We don't use
# `security find-identity -p codesigning` because that filters by
# policy validation, and self-signed certs are by definition not
# anchored to a trusted root — but codesign accepts them by name
# regardless.)
if ! security find-certificate -c "$CERT_NAME" "$LOGIN_KEYCHAIN" >/dev/null 2>&1; then
    echo -e "${RED}✗ Cert generated but not found in keychain after import${NC}" >&2
    exit 1
fi

# Print the cert's SHA1 — this is what gets embedded in every signed
# bundle's designated requirement.  Stable across reboots and rebuilds
# unless the user deletes the cert from their keychain.
CERT_SHA1=$(security find-certificate -c "$CERT_NAME" -p "$LOGIN_KEYCHAIN" \
    | openssl x509 -noout -fingerprint -sha1 \
    | sed -e 's/.*Fingerprint=//' -e 's/://g')

echo -e "${GREEN}✓ Generated 'Leap Self-Signed' code-signing cert${NC}"
echo "  Cert SHA1: $CERT_SHA1"
echo "  Stored in: $LOGIN_KEYCHAIN"
