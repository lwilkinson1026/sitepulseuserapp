# SitePulse systemd units

All three units are **fleet-identical** — the same bytes on every Pi. Anything
that differs between units lives in `/etc/sitepulse/unit.env`.

That property is the whole point. Before this, UNIT-001 ran as `lwilkinson`
out of `/home/lwilkinson` and UNIT-002 ran as `sitepulse2` out of
`/home/sitepulse2`, with the paths and usernames baked into the unit files.
Two machines shaped differently meant every deploy was hand-adapted, and
`sitepulse-listener.service` was authored directly on each Pi and tracked
nowhere in git — which is exactly how they drifted.

## Layout

| Path | Owner | What |
|---|---|---|
| `/opt/sitepulse/releases/<name>/` | `sitepulse` | code |
| `/opt/sitepulse/current` | symlink | what the units execute |
| `/etc/sitepulse/unit.env` | `root:sitepulse` `0640` | **the only per-unit file** |
| `/etc/sitepulse/service-account.json` | `root:sitepulse` `0640` | Firebase admin key |
| `/var/log/sitepulse/*.log` | `sitepulse` | logs, rotated daily ×14 |

`current` is a symlink rather than a plain directory so the follow-up OTA
agent can stage a release beside the live one and flip atomically — and flip
back. Nothing today depends on that; it costs one `ln -s` now and saves
rewriting every unit file later.

Services run as a dedicated unprivileged `sitepulse` user in `gpio`, `i2c`,
`spi`, `video`, `dialout` — and **not** in `sudo`, which both previous
per-human service users were.

`sitepulse-can.service` still runs as root: bringing a netdev up needs
`CAP_NET_ADMIN`.

## Migrating an existing Pi

```bash
# from the repo root on the Mac
scp -r pi <user>@<pi>:/tmp/sitepulse-src
ssh -t <user>@<pi> 'sudo /tmp/sitepulse-src/provision_unit.sh --dry-run'   # look first
ssh -t <user>@<pi> 'sudo /tmp/sitepulse-src/provision_unit.sh'
```

It autodetects the old install under `/home/*/sitepulse`, copies the code and
service account across, derives `SITEPULSE_UNIT_ID` from the currently
installed unit file, and carries over any `SITEPULSE_VESC_UNIT_ID` /
`SITEPULSE_CLOUDFLARE_*` it finds. It is idempotent.

**It does not stop, start, or restart anything.** Staging is safe on a live
unit with the engine running; the cutover is a separate deliberate step, and
should happen while the engine is off because the listener owns the crank:

```bash
sudo systemctl restart sitepulse-publisher sitepulse-listener
```

The old `/home/<user>/sitepulse` tree is left untouched as the rollback path.

## Deploying code after migration

```bash
scp pi/*.py <user>@<pi>:/opt/sitepulse/current/
ssh -t <user>@<pi> 'sudo systemctl restart sitepulse-publisher'
```

`scp` follows the `current` symlink, so this lands in `releases/manual/`. The
provisioning script adds the invoking admin user to the `sitepulse` group and
sets the code dirs setgid, so this works without `sudo` (log out and back in
once for the new group to take effect).

Which service to restart for which file:

| Service | Restart when you change |
|---|---|
| `sitepulse-listener` | `command_listener.py`, `engine*.py`, `servos.py`, `relays.py`, `streamer.py`, `sentry.py`, `scheduler.py` |
| `sitepulse-publisher` | `firebase_publisher.py`, `vesc_listener.py`, `predator_decoder.py`, `predator_i2c_sniffer.py` |
| `sitepulse-can` | the unit file itself (then `daemon-reload`) |

## Two things this surfaced

Writing the templates turned up drift that was invisible while the units were
maintained by hand:

1. **`SITEPULSE_VESC_UNIT_ID` was never set in the repo's publisher unit.**
   The Python default is `0`; every VESC we have flashed is `100`. UNIT-002's
   installed copy sets it, the repo copy does not. Either UNIT-001's installed
   file diverges from the repo, or its VESC telemetry has been silently
   filtered out — frames arrive, none match, the listener reports nothing.
   Worth checking the moment UNIT-001 is reachable.

2. **`SITEPULSE_CLOUDFLARE_RTMP_TARGET` / `_HLS_URL` are set in neither unit
   file.** `streamer.py` defaults both to `""` and refuses to stream. So
   streaming under systemd cannot currently work on UNIT-002, and on UNIT-001
   it must be getting its config from somewhere outside these files.

Both are now first-class fields in `unit.env.example` so they can't go missing
quietly again.
