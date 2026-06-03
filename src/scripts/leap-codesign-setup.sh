#!/usr/bin/env bash
# Set up the self-signed code-signing certificate Leap Monitor is signed with,
# in a DEDICATED keychain (not the login keychain), and make sure that keychain
# is unlocked and on the user's keychain search list.
#
# Why a dedicated keychain + set-key-partition-list (this is the whole point):
#   Any keychain in ~/Library/Keychains enforces a per-key "partition list" that
#   `security import -T` does NOT populate, so codesign pops a "codesign wants to
#   access key" dialog when it uses the key - and since we sign --deep (~230
#   nested Mach-O), that's ~230 prompts unless the user clicks "Always Allow"
#   (the confusing bug we're fixing).  The documented fix is
#   `security set-key-partition-list`, which adds codesign to the key's partition
#   list so it signs SILENTLY.  On the LOGIN keychain that call needs the user's
#   LOGIN password (phishing-shaped on managed Macs, nags everyone).  So we use a
#   DEDICATED keychain whose password WE generate - then we run
#   set-key-partition-list with THAT password: no user/login password, no "Always
#   Allow".  codesign finds the identity because we add the keychain to the search
#   list, and we sign by the cert's unique SHA-1 (below) so it's unambiguous.
#   (NB: a keychain OUTSIDE ~/Library/Keychains happens not to be partition-gated
#   and signs silently even without set-key-partition-list - but that's
#   undocumented macOS behavior we deliberately do NOT rely on.)
#
# Why sign by SHA-1, not by name:
#   Existing users already have a "Leap Self-Signed" cert in their LOGIN keychain
#   from the previous scheme.  Once the dedicated keychain (also "Leap
#   Self-Signed") is on the search list, `codesign --sign "Leap Self-Signed"`
#   sees two same-named certs and fails with "ambiguous" - even with --keychain
#   (verified: --keychain does NOT break the name tie).  Signing by the dedicated
#   cert's SHA-1 hash is unambiguous, so we DON'T need to delete the old login
#   cert (deleting it could itself prompt) - it just becomes inert.  BUILD_MONITOR_APP
#   looks the SHA-1 up with `security find-certificate -c ... -Z <dedicated-kc>`.
#
# TCC angle (unchanged from before): the cert is stable across rebuilds, so the
# designated requirement embedded in the signature is byte-identical every
# install/update, and macOS preserves the Accessibility grant (TCC keys on the
# designated requirement, not the cdhash).  The dedicated cert has a NEW SHA-1
# vs the old login-keychain one, so existing users re-grant Accessibility ONCE
# via the in-app banner after this ships; new users grant once as usual.
#
# Idempotent + self-healing: if the cert already exists in the dedicated keychain
# we DON'T regenerate - we just re-unlock it and make sure it's on the search
# list, then exit.  Safe to run before every build (it's a prereq of every
# monitor build).
#
# Usage:
#   leap-codesign-setup.sh            # ensure/generate (default)
#   leap-codesign-setup.sh --remove   # teardown (uninstall): delete the keychain

unset PYTHONHOME PYTHONPATH VIRTUAL_ENV

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

CERT_NAME="Leap Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/leap-codesign.keychain-db"
PASS_FILE="$HOME/Library/Keychains/.leap-codesign.pass"
LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

# ── helpers ─────────────────────────────────────────────────────────

# Canonicalize a path: resolve symlinks in the directory part (e.g.
# /var -> /private/var, or a symlinked/network $HOME) and keep the basename.
# macOS stores a canonicalized path in the search list, which can differ
# textually from what we passed, so membership checks must compare resolved
# paths or they silently miss.
_resolve() {
    local p="$1" d
    d=$(cd "$(dirname "$p")" 2>/dev/null && pwd -P) || { printf '%s' "$p"; return; }
    printf '%s/%s' "$d" "$(basename "$p")"
}

# Is $1 currently on the user keychain search list?  Compare RESOLVED paths,
# and strip the leading whitespace + surrounding quotes that `list-keychains`
# prints (but not spaces inside the path).
_in_search_list() {
    local target line
    target=$(_resolve "$1")
    while IFS= read -r line; do
        line=$(printf '%s' "$line" | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')
        [ -n "$line" ] || continue
        [ "$(_resolve "$line")" = "$target" ] && return 0
    done < <(security list-keychains -d user)
    return 1
}

# Append $1 to the user keychain search list without dropping anything that's
# already there.  `security` has no "append" primitive - you must re-set the
# whole list - so this is the one operation that could, if it parsed the
# existing list wrong, drop the login keychain (and break the user's saved
# passwords).  Hence: capture defensively, guarantee the login keychain is in
# the set we write, then VERIFY the end state and roll back if it's wrong.
ensure_in_search_list() {
    local kc="$1"
    if _in_search_list "$kc"; then
        return 0
    fi

    local -a cur=()
    local line
    while IFS= read -r line; do
        line=$(printf '%s' "$line" | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')
        [ -n "$line" ] && cur+=("$line")
    done < <(security list-keychains -d user)

    if [ "${#cur[@]}" -eq 0 ]; then
        echo -e "${RED}✗ Could not read the keychain search list; refusing to modify it.${NC}" >&2
        return 1
    fi

    # Defensively guarantee the login keychain is in the list we're about to
    # write, even if our parse somehow missed it.  Compare resolved paths.
    local has_login=0 k login_r
    login_r=$(_resolve "$LOGIN_KEYCHAIN")
    for k in "${cur[@]}"; do
        [ "$(_resolve "$k")" = "$login_r" ] && has_login=1
    done
    if [ "$has_login" -eq 0 ] && [ -f "$LOGIN_KEYCHAIN" ]; then
        cur=("$LOGIN_KEYCHAIN" "${cur[@]}")
    fi

    security list-keychains -d user -s "${cur[@]}" "$kc" 2>/dev/null || true

    # Verify the end state: our keychain must be present AND the login keychain
    # must have survived.  Anything else → roll back to the captured list.
    if _in_search_list "$kc" && { [ ! -f "$LOGIN_KEYCHAIN" ] || _in_search_list "$LOGIN_KEYCHAIN"; }; then
        return 0
    fi

    echo -e "${RED}✗ Keychain search-list update did not take cleanly - rolling back.${NC}" >&2
    security list-keychains -d user -s "${cur[@]}" 2>/dev/null || true
    if [ -f "$LOGIN_KEYCHAIN" ] && ! _in_search_list "$LOGIN_KEYCHAIN"; then
        security list-keychains -d user -s "$LOGIN_KEYCHAIN" "${cur[@]}" 2>/dev/null || true
    fi
    return 1
}

# Unlock the dedicated keychain with the stored password and disable its
# auto-lock timeout (so it stays unlocked through a multi-minute py2app build).
# Returns non-zero if the password file is missing or the unlock fails.
unlock_keychain() {
    [ -f "$PASS_FILE" ] || return 1
    local pw
    pw=$(cat "$PASS_FILE") || return 1
    security set-keychain-settings "$KEYCHAIN" 2>/dev/null || true   # no-timeout
    security unlock-keychain -p "$pw" "$KEYCHAIN"
}

# Add codesign + Apple tools to the dedicated key's partition list, authorized
# with OUR generated keychain password ($1) - NOT the user's login password.
# This is what makes codesign sign silently from a keychain in ~/Library/Keychains
# (verified: without it codesign prompts there even for a custom keychain).
# Idempotent - safe to re-run.  Returns the security command's status.
set_partition_list() {
    security set-key-partition-list \
        -S apple-tool:,apple:,codesign: -s \
        -k "$1" "$KEYCHAIN" >/dev/null 2>&1
}

# ── teardown mode (uninstall) ───────────────────────────────────────
# `security delete-keychain` removes the file AND the search-list entry
# (verified), leaving the login keychain untouched.
if [ "${1:-}" = "--remove" ]; then
    security delete-keychain "$KEYCHAIN" 2>/dev/null || true
    rm -f "$PASS_FILE"
    echo -e "${GREEN}✓ Removed Leap code-signing keychain${NC}"
    exit 0
fi

# ── fast path: cert already in the dedicated keychain ───────────────
# Don't regenerate (that would change the SHA-1 and force a needless re-grant).
# Just re-unlock and make sure the keychain is still on the search list.
if [ -f "$KEYCHAIN" ] && [ -f "$PASS_FILE" ] \
    && security find-certificate -c "$CERT_NAME" "$KEYCHAIN" >/dev/null 2>&1; then
    if unlock_keychain >/dev/null 2>&1; then
        set_partition_list "$(cat "$PASS_FILE")" || true   # re-assert (idempotent)
        ensure_in_search_list "$KEYCHAIN" || exit 1
        CERT_SHA1=$(security find-certificate -c "$CERT_NAME" -Z "$KEYCHAIN" 2>/dev/null \
            | awk '/SHA-1 hash:/{print $NF}')
        echo -e "${GREEN}✓ '$CERT_NAME' cert already in dedicated keychain (SHA1 $CERT_SHA1) - skipping generation.${NC}"
        exit 0
    fi
    echo -e "${YELLOW}⚠ Existing Leap code-signing keychain could not be unlocked - regenerating.${NC}" >&2
fi

# ── generation path ─────────────────────────────────────────────────
echo "→ Generating Leap Self-Signed code-signing certificate (dedicated keychain)..."

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# Private key.
openssl genrsa -out leap.key 2048 2>/dev/null

# Self-signed cert with the codeSigning EKU.  No subjectKeyIdentifier - macOS
# computes its own key-pair hash from the public key bits, and an explicit SKI
# confuses cert/key pairing on import.
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

# Bundle into PKCS12.  macOS Security can only read PKCS12 using the legacy
# PBE-SHA1-3DES algorithm, not OpenSSL 3's new defaults; OpenSSL 3 needs an
# explicit `-legacy`, LibreSSL (Apple stock) has no such flag (legacy is the
# default), so we probe for it.  Empty passwords break MAC verification on
# import, so use a short per-run password.
PKCS12_LEGACY=""
if openssl pkcs12 -help 2>&1 | grep -q -- "-legacy"; then
    PKCS12_LEGACY="-legacy"
fi
P12_PW="leap-setup-$$"
openssl pkcs12 -export \
    -inkey leap.key -in leap.crt \
    -out leap.p12 \
    -name "$CERT_NAME" \
    -passout "pass:$P12_PW" \
    $PKCS12_LEGACY 2>/dev/null

# Fresh dedicated keychain.  Delete any leftover first so a half-built keychain
# can't wedge us (delete-keychain also clears its search-list entry).  The
# random password is persisted 0600 so future builds can unlock it without a
# prompt - this keychain holds ONLY a per-machine self-signed code-signing cert
# whose sole power is signing "Leap Self-Signed" for local TCC; it protects
# nothing else, so a local 0600 password file is an acceptable, CI-standard
# design (the password gates nothing an attacker with file access lacks).
KC_PASS=$(openssl rand -hex 24)
security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
security create-keychain -p "$KC_PASS" "$KEYCHAIN"
( umask 077; printf '%s' "$KC_PASS" > "$PASS_FILE" )
chmod 600 "$PASS_FILE"
security set-keychain-settings "$KEYCHAIN"        # no auto-lock timeout
security unlock-keychain -p "$KC_PASS" "$KEYCHAIN"

# Import.  -T adds codesign + security to the key's legacy ACL.  That alone is
# NOT enough on a ~/Library/Keychains keychain (the partition list still gates),
# so set-key-partition-list below does the real work; -T is belt-and-suspenders.
security import leap.p12 \
    -k "$KEYCHAIN" \
    -P "$P12_PW" \
    -T /usr/bin/codesign \
    -T /usr/bin/security \
    >/dev/null 2>&1 || true

# Authoritative gate: a failed import (whatever the cause) lands here with a
# clear message rather than a bare set -e exit with stderr swallowed above.
if ! security find-certificate -c "$CERT_NAME" "$KEYCHAIN" >/dev/null 2>&1; then
    echo -e "${RED}✗ Cert generated but not found in the dedicated keychain after import${NC}" >&2
    exit 1
fi

# THE silent-signing fix: add codesign to the key's partition list, authorized
# with OUR keychain password (no user/login password).  Without this, codesign
# prompts "codesign wants to access key" ~230 times during the --deep sign.
if ! set_partition_list "$KC_PASS"; then
    echo -e "${YELLOW}⚠ Could not set the key partition list - codesign may prompt on first sign.${NC}" >&2
fi

ensure_in_search_list "$KEYCHAIN" || exit 1

# Clear any stale Accessibility entries from the previous scheme (cdhash-based,
# or keyed on the old login-keychain cert's SHA-1).  The new cert's SHA-1
# differs, so TCC won't match until the user re-grants once via the in-app
# banner; clearing avoids a ghost entry sitting beside the new one.
tccutil reset Accessibility com.leap.monitor >/dev/null 2>&1 || true

CERT_SHA1=$(security find-certificate -c "$CERT_NAME" -Z "$KEYCHAIN" \
    | awk '/SHA-1 hash:/{print $NF}')

echo -e "${GREEN}✓ Generated 'Leap Self-Signed' code-signing cert${NC}"
echo "  Cert SHA1: $CERT_SHA1"
echo "  Keychain:  $KEYCHAIN"
echo ""
echo -e "${YELLOW}ℹ One-time step on next Leap Monitor launch:${NC} the in-app banner will ask"
echo "  you to grant Accessibility (and Notifications, if not already granted)."
echo "  Future updates will preserve the grant - you'll never see this again."
echo ""
