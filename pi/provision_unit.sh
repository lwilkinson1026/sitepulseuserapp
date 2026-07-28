#!/usr/bin/env bash
#
# Normalize a SitePulse Pi onto the standard layout, or provision a new one.
#
#   sudo ./provision_unit.sh [--migrate-from DIR] [--unit-id UNIT-00N] [--dry-run]
#
# Idempotent: safe to re-run. Converges a Pi onto
#
#   /opt/sitepulse/releases/<name>/   code
#   /opt/sitepulse/current -> ...     symlink the systemd units point at
#   /etc/sitepulse/unit.env           the only per-unit file
#   /etc/sitepulse/service-account.json
#   /var/log/sitepulse/*.log
#
# and a dedicated unprivileged `sitepulse` service user.
#
# WHY: UNIT-001 runs as lwilkinson out of /home/lwilkinson, UNIT-002 runs as
# sitepulse2 out of /home/sitepulse2, and the systemd units hardcode each. One
# artifact cannot serve two machines shaped differently, so every deploy has
# been hand-adapted — which is how the two drifted apart in the first place.
#
# SAFETY: this script does not stop, start, or restart any service. It only
# stages files. Running services keep running under their old definitions
# until you explicitly restart them, so this is safe to run on a live unit
# with the engine going. The cutover is a separate, deliberate step.

set -euo pipefail

CODE_DIR=/opt/sitepulse
RELEASE_NAME=manual
RELEASE_DIR="$CODE_DIR/releases/$RELEASE_NAME"
CONF_DIR=/etc/sitepulse
LOG_DIR=/var/log/sitepulse
SVC_USER=sitepulse
SVC_GROUPS="gpio i2c spi video dialout"

MIGRATE_FROM=""
UNIT_ID=""
DRY_RUN=0

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --migrate-from) MIGRATE_FROM="$2"; shift 2 ;;
    --unit-id)      UNIT_ID="$2";      shift 2 ;;
    --dry-run)      DRY_RUN=1;         shift ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

run() {
  if [[ $DRY_RUN -eq 1 ]]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi
}
say() { printf '\n== %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "must run as root (use sudo)" >&2; exit 1; }
[[ -f "$SRC/systemd/sitepulse-publisher.service" ]] || {
  echo "cannot find systemd/ next to this script (looked in $SRC)" >&2; exit 1; }

# ── Locate the existing install ───────────────────────────────────────────
# Autodetect so the common case is a bare `sudo ./provision_unit.sh`.
if [[ -z "$MIGRATE_FROM" ]]; then
  for d in /home/*/sitepulse; do
    [[ -f "$d/firebase_publisher.py" ]] && { MIGRATE_FROM="$d"; break; }
  done
fi
if [[ -n "$MIGRATE_FROM" ]]; then
  echo "migrating from: $MIGRATE_FROM"
else
  echo "no existing install found — provisioning fresh"
fi

# ── Derive the unit ID before anything else ───────────────────────────────
# This has to succeed up front. The new units hard-require /etc/sitepulse/
# unit.env, so daemon-reload without a valid one would leave the services
# unable to start on their next restart.
if [[ -z "$UNIT_ID" ]]; then
  for f in /etc/systemd/system/sitepulse-publisher.service \
           /etc/systemd/system/sitepulse-listener.service; do
    [[ -f "$f" ]] || continue
    UNIT_ID="$(sed -n 's/^Environment=SITEPULSE_UNIT_ID=//p' "$f" | head -1)"
    [[ -n "$UNIT_ID" ]] && break
  done
fi
if [[ -z "$UNIT_ID" ]]; then
  # UNIT-001's unit file never set it and leaned on the Python default.
  UNIT_ID=UNIT-001
  echo "WARNING: could not read SITEPULSE_UNIT_ID from any installed unit file."
  echo "         Assuming $UNIT_ID (the Python default this Pi was relying on)."
  echo "         Re-run with --unit-id if that is wrong. Getting this wrong"
  echo "         points the Pi at another unit's Firestore documents."
fi
echo "unit id: $UNIT_ID"

# ── Service user ──────────────────────────────────────────────────────────
say "service user"
if id -u "$SVC_USER" >/dev/null 2>&1; then
  echo "  $SVC_USER exists"
else
  run useradd --system --home-dir "$CODE_DIR" --no-create-home \
              --shell /usr/sbin/nologin "$SVC_USER"
  echo "  created $SVC_USER"
fi
# Only the hardware groups. Deliberately not sudo — the old per-human service
# users were in sudo, which a daemon has no business holding.
for g in $SVC_GROUPS; do
  if getent group "$g" >/dev/null; then
    run usermod -aG "$g" "$SVC_USER"
  else
    echo "  WARNING: group '$g' does not exist, skipping"
  fi
done
echo "  groups: $SVC_GROUPS"

# ── Directories ───────────────────────────────────────────────────────────
say "directories"
run install -d -m 2775 -o "$SVC_USER" -g "$SVC_USER" "$CODE_DIR" "$CODE_DIR/releases" "$RELEASE_DIR"
run install -d -m 0750 -o root       -g "$SVC_USER" "$CONF_DIR"
run install -d -m 2775 -o "$SVC_USER" -g "$SVC_USER" "$LOG_DIR"
# setgid (2775) on the code dirs so files scp'd in by a human admin keep the
# sitepulse group and stay readable by the service.
echo "  $CODE_DIR  $CONF_DIR  $LOG_DIR"

# Let whoever ran sudo scp straight into /opt/sitepulse/current without sudo.
ADMIN_USER="${SUDO_USER:-}"
if [[ -n "$ADMIN_USER" ]] && [[ "$ADMIN_USER" != "root" ]]; then
  run usermod -aG "$SVC_USER" "$ADMIN_USER"
  echo "  added $ADMIN_USER to the $SVC_USER group (log out/in to take effect)"
fi

# ── Code ──────────────────────────────────────────────────────────────────
say "code"
if [[ -n "$MIGRATE_FROM" && -d "$MIGRATE_FROM" ]]; then
  # Copy rather than move. The old tree stays untouched as a rollback path,
  # and because it is still what the currently-running services execute.
  run bash -c "cp -a '$MIGRATE_FROM'/*.py '$RELEASE_DIR'/ 2>/dev/null || true"
  echo "  copied *.py from $MIGRATE_FROM (original left in place)"
fi
run chown -R "$SVC_USER:$SVC_USER" "$CODE_DIR/releases"
# Relative target so the symlink survives the tree being moved wholesale.
run ln -sfn "releases/$RELEASE_NAME" "$CODE_DIR/current"
echo "  $CODE_DIR/current -> releases/$RELEASE_NAME"

# ── Secret ────────────────────────────────────────────────────────────────
say "service account"
if [[ -f "$CONF_DIR/service-account.json" ]]; then
  echo "  already present, leaving alone"
elif [[ -n "$MIGRATE_FROM" && -f "$MIGRATE_FROM/service-account.json" ]]; then
  run install -m 0640 -o root -g "$SVC_USER" \
      "$MIGRATE_FROM/service-account.json" "$CONF_DIR/service-account.json"
  echo "  copied from $MIGRATE_FROM"
  echo "  NOTE: this is the key flagged as leaked in the project notes."
  echo "        Rotate it before this fleet takes remote-driven updates."
else
  echo "  MISSING — install it yourself:"
  echo "    sudo install -m 0640 -o root -g $SVC_USER sa.json $CONF_DIR/service-account.json"
fi

# ── Per-unit env ──────────────────────────────────────────────────────────
say "per-unit config"
if [[ -f "$CONF_DIR/unit.env" ]]; then
  echo "  $CONF_DIR/unit.env exists, not overwriting"
else
  run install -m 0640 -o root -g "$SVC_USER" \
      "$SRC/systemd/unit.env.example" "$CONF_DIR/unit.env"
  run sed -i "s/^SITEPULSE_UNIT_ID=.*/SITEPULSE_UNIT_ID=$UNIT_ID/" "$CONF_DIR/unit.env"
  # Carry over anything the old unit files set that we would otherwise lose.
  # Extraction is grep -E rather than sed alternation on purpose: `\|` inside
  # a sed BRE is a GNU extension. BSD sed silently matches nothing instead of
  # erroring, so the same line that works here would quietly drop the
  # Cloudflare URLs if this were ever run or tested on macOS.
  for f in /etc/systemd/system/sitepulse-publisher.service \
           /etc/systemd/system/sitepulse-listener.service; do
    [[ -f "$f" ]] || continue
    while IFS= read -r kv; do
      k="${kv%%=*}"
      if grep -q "^${k}=" "$CONF_DIR/unit.env"; then
        run sed -i "s|^${k}=.*|${kv}|" "$CONF_DIR/unit.env"
      else
        run bash -c "printf '%s\n' '$kv' >> '$CONF_DIR/unit.env'"
      fi
      echo "  carried over $k from $(basename "$f")"
    done < <(grep -E '^Environment=(SITEPULSE_VESC_UNIT_ID|SITEPULSE_CLOUDFLARE_[A-Z_]+)=' "$f" \
             | sed 's/^Environment=//')
  done
  echo "  wrote $CONF_DIR/unit.env for $UNIT_ID"
  echo "  REVIEW IT — Cloudflare stream URLs are almost certainly still blank."
fi

# ── systemd units + logrotate ─────────────────────────────────────────────
say "systemd units"
for u in sitepulse-can sitepulse-publisher sitepulse-listener; do
  run install -m 0644 -o root -g root \
      "$SRC/systemd/$u.service" "/etc/systemd/system/$u.service"
  echo "  installed $u.service"
done
run install -m 0644 -o root -g root \
    "$SRC/systemd/sitepulse.logrotate" /etc/logrotate.d/sitepulse
echo "  installed /etc/logrotate.d/sitepulse"

# Only reload once unit.env is definitely on disk. A reload with the new units
# in place but no unit.env would mean the next automatic Restart=on-failure
# brings the service up against a file that does not exist, turning a crash
# into an outage.
if [[ -f "$CONF_DIR/unit.env" || $DRY_RUN -eq 1 ]]; then
  run systemctl daemon-reload
  echo "  daemon-reload done"
else
  echo "  SKIPPED daemon-reload: $CONF_DIR/unit.env is missing."
  echo "  Create it, then run: sudo systemctl daemon-reload"
fi

# ── Next steps ────────────────────────────────────────────────────────────
cat <<EOF

== staged. nothing has been restarted.

Running services are still on their old definitions. To cut over — and pick a
moment when the engine is NOT running, because the listener owns the crank:

  sudo systemctl restart sitepulse-publisher sitepulse-listener
  systemctl is-active sitepulse-can sitepulse-publisher sitepulse-listener
  tail -f $LOG_DIR/publisher.log

Verify before walking away:
  - publisher.log shows a fresh snapshot within ~10s
  - the app shows this unit online with a moving SoC
  - journalctl -u sitepulse-listener -n 50 --no-pager   (no tracebacks)

If it goes wrong, the old tree is untouched at ${MIGRATE_FROM:-<none>} — restore
the previous unit files from git and daemon-reload to get back.

Once you are happy, enable at boot:
  sudo systemctl enable sitepulse-can sitepulse-publisher sitepulse-listener
EOF
