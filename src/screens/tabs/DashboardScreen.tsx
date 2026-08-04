import React, { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { CornerBrackets, Eyebrow, FigCaption, FuelAlertBanner, Screen, SecondaryCTA } from '../../components';
import { useUnitTelemetry } from '../../hooks/useUnitTelemetry';
import { useUnitDoc } from '../../hooks/useUnitDoc';
import { useAuth } from '../../hooks/AuthContext';
import type { EngineConfig, EngineState } from '../../firebase/types';
import {
  chargeEngine,
  crankEngine,
  startEngine,
  stopEngine,
  toggleAc,
  tuneCharge,
  wakeLcd,
} from '../../firebase/commands';
import { confirm } from '../../lib/confirm';
import { vescVoltsToSoc, DEFAULT_CELL_COUNT } from '../../lib/voltageSoc';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase-1 dashboard. Subscribes to units/{unitId}/current/snapshot in
// Firestore. The Pi overwrites that doc every 2–5 s; we render whatever
// arrives and reflect staleness via the LIVE / STALE / OFFLINE eyebrow.
//
// Unit ID is hardcoded for now — replaced with the owner's claimed unit
// once the pair flow lands. Override via EXPO_PUBLIC_DEV_UNIT_ID for testing.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

// Engine controls (Start / Stop / Charge / Crank) live behind this flag.
// They expose tuning + bench-test surface that doesn't belong on a regular
// user's main screen — set `EXPO_PUBLIC_ADMIN_MODE=true` in .env to surface
// them. Wake LCD and Aux remain visible to all users since those drive the
// Predator's display and AC outlet button. Until a real role system lands
// in Firestore, this is a build-time gate, not authorization — anyone with
// a custom build of the app could flip it. It's strictly UX hygiene.
const ADMIN_MODE = process.env.EXPO_PUBLIC_ADMIN_MODE === 'true';

// How long the dashboard keeps showing the last Predator-LCD SoC reading
// after `battery_soc` goes null in the snapshot. The Pi's I²C decoder
// occasionally drops a frame even while the LCD is awake, and without
// stickiness the displayed number flips to the voltage fallback (which
// itself jitters under load) on every dropped frame. 20 s comfortably
// covers normal frame gaps without delaying a real LCD-asleep handoff
// to the voltage estimate by more than a few snapshot ticks.
const LCD_STICKY_MS = 20_000;

// EMA weight applied to the voltage-derived SoC each snapshot. The charge
// loop modulates motor_amps_in between -10 A and 0 A every few seconds,
// so the IR-compensated voltage estimate moves ±10–15 %/sample even though
// the underlying pack state is barely changing. Lower α = heavier smoothing.
// α = 0.15 + a ~2-5 s snapshot cadence ≈ a 30 s effective window — enough
// to absorb the charge-loop ripple without lagging real changes for long.
const VESC_SOC_EMA_ALPHA = 0.15;

// Charge-tune stepper bounds. Hard ceiling on the Pi is 50 A; we cap the
// app side a hair below so the user never lands exactly on the clamp and
// gets a silently-rejected request. Any step that would land above
// CHARGE_TUNE_WARN_AMPS prompts confirm() before sending.
const CHARGE_TUNE_MIN_AMPS  = 0;
const CHARGE_TUNE_MAX_AMPS  = 45;
const CHARGE_TUNE_WARN_AMPS = 25;

const STALENESS_LABEL: Record<string, string> = {
  fresh: 'LIVE',
  stale: 'STALE',
  offline: 'OFFLINE',
  unknown: '—',
};

function fmt(value: number | undefined | null, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

function TuneButton({
  label,
  onPress,
  disabled,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      onPress={disabled ? undefined : onPress}
      accessibilityRole="button"
      accessibilityLabel={`Charge target ${label} amps`}
      style={({ pressed }) => [
        styles.tuneButton,
        pressed && !disabled ? styles.tuneButtonPressed : null,
        disabled ? styles.tuneButtonDisabled : null,
      ]}
    >
      <Text style={styles.tuneButtonLabel}>{label}</Text>
    </Pressable>
  );
}

export function DashboardScreen() {
  const { loading, snapshot, staleness, error } = useUnitTelemetry(DEV_UNIT_ID);
  // Subscribe to the Pi-published engine runtime state so we can flip the
  // charging-input tile on only when the regen loop is actively running.
  // Same doc the ScheduleScreen reads — the Pi merges scheduler and runtime
  // fields together, so this read may contain both shapes.
  const engineState = useUnitDoc<EngineState>(DEV_UNIT_ID, 'current', 'engine');
  // Needed for the voltage→SoC fallback: the curve is per-cell, and this
  // unit's pack geometry decides what a given pack voltage means. 49 V is
  // a full 14S pack or a half-charged 15S one.
  const engineConfig = useUnitDoc<EngineConfig>(DEV_UNIT_ID, 'config', 'engine');
  const { user } = useAuth();
  // 'pending' covers the brief window between tap and Pi ack. We don't
  // round-trip the ack into here yet — just debounce by time to prevent
  // double-taps from queueing two presses back-to-back.
  const [wakeBusy, setWakeBusy] = useState(false);
  const [acBusy, setAcBusy] = useState(false);

  // Hooks for the SoC display logic MUST be declared up here, before any
  // early return — React's rules-of-hooks. The actual derivation (which
  // requires a non-null snapshot) lives below the early returns and just
  // reads the ref / state set up here.
  const lcdMemRef = useRef<{ value: number; seenAt: number } | null>(null);
  const [smoothedVescSoc, setSmoothedVescSoc] = useState<number | null>(null);
  useEffect(() => {
    if (!snapshot) return;
    const sample = vescVoltsToSoc(
      snapshot.motor_volts,
      engineConfig.data?.cellCount ?? DEFAULT_CELL_COUNT,
      snapshot.motor_amps_in,
    );
    if (sample === null) return;
    setSmoothedVescSoc((prev) =>
      prev === null
        ? sample
        : Math.round(
            VESC_SOC_EMA_ALPHA * sample + (1 - VESC_SOC_EMA_ALPHA) * prev,
          ),
    );
    // cellCount is a dependency: config/engine usually resolves AFTER the
    // first snapshot, and without it here the estimate would stay pinned to
    // the DEFAULT_CELL_COUNT fallback for the life of the screen.
  }, [snapshot, engineConfig.data?.cellCount]);

  const onWakePress = async () => {
    if (!user || wakeBusy) return;
    setWakeBusy(true);
    try {
      await wakeLcd(DEV_UNIT_ID, user.uid);
    } catch (e) {
      // Surface via console; no in-screen error UI for a one-off bench
      // action. If this becomes user-facing, lift into a toast.
      console.warn('[dashboard] wakeLcd failed', e);
    } finally {
      // Leave the button disabled briefly so the servo has time to
      // physically complete its press-release cycle before the user can
      // queue another.
      setTimeout(() => setWakeBusy(false), 1200);
    }
  };

  const onAcPress = async () => {
    if (!user || acBusy) return;
    setAcBusy(true);
    try {
      await toggleAc(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] toggleAc failed', e);
    } finally {
      setTimeout(() => setAcBusy(false), 1200);
    }
  };

  // Both engine actions get a confirm dialog + a generous post-tap lock-out
  // that covers the worst-case sequence duration so a double-tap can't race
  // a still-in-flight attempt on the Pi.
  const [crankBusy, setCrankBusy] = useState(false);
  const [startBusy, setStartBusy] = useState(false);

  const onCrankPress = async () => {
    if (!user || crankBusy) return;
    const ok = await confirm({
      title: 'Crank Engine?',
      message:
        'This is the low-level crank — it only spins the starter motor. ' +
        'It does NOT set the choke or enable the spark relay. Use "Start Engine" ' +
        'for a normal start; use this only for bench testing.',
      confirmLabel: 'Crank',
      destructive: true,
    });
    if (!ok) return;
    setCrankBusy(true);
    try {
      await crankEngine(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] crankEngine failed', e);
    } finally {
      setTimeout(() => setCrankBusy(false), 6000);
    }
  };

  const [chargeBusy, setChargeBusy] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);

  const onChargePress = async () => {
    if (!user || chargeBusy) return;
    const ok = await confirm({
      title: 'Begin Charging?',
      message:
        'Engine must already be running. Will apply a regen load (default 10A) ' +
        'and monitor pack voltage; stops automatically when voltage hits the ' +
        'configured threshold, or you can tap Stop Engine at any time.',
      confirmLabel: 'Charge',
    });
    if (!ok) return;
    setChargeBusy(true);
    try {
      await chargeEngine(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] chargeEngine failed', e);
    } finally {
      // Charge ack is fast (it just spawns the bg thread). 1.5s is enough;
      // the actual charging continues in the background.
      setTimeout(() => setChargeBusy(false), 1500);
    }
  };

  const onStopPress = async () => {
    if (!user || stopBusy) return;
    const ok = await confirm({
      title: 'Stop Engine?',
      message:
        'Aborts any active charge loop, opens the spark relay, and waits for ' +
        'the engine to coast to a stop.',
      confirmLabel: 'Stop',
      destructive: true,
    });
    if (!ok) return;
    setStopBusy(true);
    try {
      await stopEngine(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] stopEngine failed', e);
    } finally {
      // Stop sequence includes up to 15s RPM-wait — lock out longer.
      setTimeout(() => setStopBusy(false), 18000);
    }
  };

  // Charge tune stepper. Disabled briefly after each press so the round-trip
  // can land before the next tap. Confirmation required when crossing the
  // warn threshold upward — both as a guard against fat-finger taps and to
  // make the operator pause before loading the engine hard.
  const [tuneBusy, setTuneBusy] = useState(false);
  const onTunePress = async (delta: number) => {
    if (!user || tuneBusy) return;
    const current = Math.round(engineState.data?.currentAmpsCommanded ?? 0);
    let next = current + delta;
    if (next < CHARGE_TUNE_MIN_AMPS) next = CHARGE_TUNE_MIN_AMPS;
    if (next > CHARGE_TUNE_MAX_AMPS) next = CHARGE_TUNE_MAX_AMPS;
    if (next === current) return;

    if (delta > 0 && next > CHARGE_TUNE_WARN_AMPS && current <= CHARGE_TUNE_WARN_AMPS) {
      const ok = await confirm({
        title: `Raise to ${next} A?`,
        message:
          `You're about to take the regen load above ${CHARGE_TUNE_WARN_AMPS} A. ` +
          `The engine may bog down or stall under sustained high load. ` +
          `The VESC will still slew at 10 A/s, but listen for the engine ` +
          `before adding more.`,
        confirmLabel: `Set ${next} A`,
        destructive: true,
      });
      if (!ok) return;
    }

    setTuneBusy(true);
    try {
      await tuneCharge(DEV_UNIT_ID, user.uid, next);
    } catch (e) {
      console.warn('[dashboard] tuneCharge failed', e);
    } finally {
      setTimeout(() => setTuneBusy(false), 300);
    }
  };

  const onStartPress = async () => {
    if (!user || startBusy) return;
    const ok = await confirm({
      title: 'Start Engine?',
      message:
        'This will set the choke, energize the spark relay, and crank the engine. ' +
        'On success the choke opens automatically; on failure the choke and spark ' +
        'are reset. Make sure the area around the engine is clear.',
      confirmLabel: 'Start',
      destructive: true,
    });
    if (!ok) return;
    setStartBusy(true);
    try {
      await startEngine(DEV_UNIT_ID, user.uid);
    } catch (e) {
      console.warn('[dashboard] startEngine failed', e);
    } finally {
      // Lock out for ~10s — covers worst-case sequence: choke settle (0.5s)
      // + spark settle (0.2s) + crank (≤4s) + post-catch (2s) + slack for
      // the Pi to publish the final state.
      setTimeout(() => setStartBusy(false), 10000);
    }
  };

  if (loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>SUBSCRIBING  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  if (error) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={[styles.connectLabel, { color: colors.danger }]}>
            FIRESTORE ERROR
          </Text>
          <Text style={styles.connectHint}>{error.message}</Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
          <Text style={styles.connectHint}>UNIT  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  if (!snapshot) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={styles.connectLabel}>NO TELEMETRY YET</Text>
          <Text style={styles.connectHint}>
            Waiting for the controller to publish its first snapshot.
          </Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
          <Text style={styles.connectHint}>UNIT  ·  {DEV_UNIT_ID}</Text>
        </View>
      </Screen>
    );
  }

  // SOC display priority: LCD reading first (accurate when fresh); fall
  // back to EMA-smoothed VESC-voltage-derived estimate so we always have
  // *something* to show when the Predator LCD is asleep. Hooks for both
  // paths are declared at the top of the component; this block just reads
  // the ref / state and updates the LCD memory.
  //
  // Sticky LCD memory: keep showing the last non-null LCD value for
  // LCD_STICKY_MS after battery_soc goes null in the snapshot. Without
  // this, a single dropped I²C frame would swap the display from the
  // LCD path to the voltage path (and back next snapshot), making the
  // hero number look like it's cycling between several values.
  const live = snapshot.battery_soc;
  if (typeof live === 'number' && Number.isFinite(live)) {
    lcdMemRef.current = { value: live, seenAt: Date.now() };
  }
  const lcdMem = lcdMemRef.current;
  const lcdFresh =
    lcdMem !== null && Date.now() - lcdMem.seenAt < LCD_STICKY_MS;
  const lcdSoc = lcdFresh ? lcdMem!.value : null;

  const usingVescFallback = lcdSoc === null && smoothedVescSoc !== null;
  const socStr =
    lcdSoc !== null
      ? fmt(lcdSoc, 0)
      : usingVescFallback
        ? String(smoothedVescSoc)
        : '—';
  const stalenessKind =
    staleness === 'fresh' ? 'fresh' : staleness === 'stale' ? 'stale' : 'offline';

  // Live charging readout. Only rendered while the Pi reports state ===
  // 'charging' AND the VESC telemetry is fresh — otherwise we'd be showing
  // a frozen number from the last frame before the bus stalled.
  // Sign convention: during regen, motor_amps_in flows OUT of the
  // controller into the pack, which VESC reports as negative. Take abs
  // for display; the user thinks of it as "amps going into the battery".
  const isCharging = engineState.data?.state === 'charging';
  const motorFresh =
    snapshot.motor_amps_in !== undefined &&
    snapshot.motor_volts !== undefined &&
    !snapshot.motor_stale;
  const showChargeTile = isCharging && motorFresh;

  // Distinct from `isCharging` above, which means "the engine is running a
  // regen charge cycle through the VESC". This one is the Predator on shore
  // power, read straight off the LCD. Both can be false, either can be true.
  const wallCharging = snapshot.charging === true;
  // The LCD reuses one HH:MM field for both estimates, so only one is ever
  // populated; pick whichever the current layout is reporting.
  const etaMinutes = wallCharging
    ? snapshot.time_to_full_minutes
    : snapshot.time_to_empty_minutes;
  const etaStr =
    etaMinutes === null || etaMinutes === undefined
      ? '—'
      : `${Math.floor(etaMinutes / 60)}:${String(etaMinutes % 60).padStart(2, '0')}`;
  const chargeAmps = showChargeTile
    ? Math.abs(snapshot.motor_amps_in as number)
    : null;
  const chargeWatts = showChargeTile
    ? Math.abs(
        (snapshot.motor_amps_in as number) * (snapshot.motor_volts as number),
      )
    : null;

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <Eyebrow
          parts={[DEV_UNIT_ID, STALENESS_LABEL[staleness] ?? '—']}
          live
          staleness={stalenessKind}
        />

        <FuelAlertBanner unitId={DEV_UNIT_ID} />

        <CornerBrackets style={styles.hero}>
          <View style={styles.heroInner}>
            <Text style={styles.heroNumeric}>{socStr}</Text>
            <Text style={styles.heroUnit}>%</Text>
          </View>
          <Text style={styles.heroSub}>
            STATE OF CHARGE{usingVescFallback ? '  (APPROX)' : ''}  ·{' '}
            {wallCharging
              ? `CHARGING  ·  ${etaStr} TO FULL`
              : `${snapshot.output_mode.toUpperCase()}  ·  ${fmt(snapshot.output_watts, 0)} W`}
          </Text>
        </CornerBrackets>

        <View style={styles.modeRow}>
          <Text style={styles.modeBadge}>{snapshot.system_mode.replace(/_/g, ' ').toUpperCase()}</Text>
          <Text style={styles.modeStamp}>
            {snapshot.last_update
              ? `UPDATED ${Math.max(0, Math.round((Date.now() - snapshot.last_update.toMillis()) / 1000))} s AGO`
              : 'NO TIMESTAMP'}
          </Text>
        </View>

        <View style={styles.metricGroup}>
          <Eyebrow parts={['Output power']} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>{fmt(snapshot.output_watts, 0)}</Text>
            <Text style={styles.metricUnit}>W</Text>
          </View>
          <Text style={styles.metricSub}>
            {snapshot.dc_active ? 'DC ●' : 'DC ○'}  ·  {snapshot.ac_active ? 'AC ●' : 'AC ○'}
          </Text>
        </View>

        {showChargeTile && (
          <View style={styles.metricGroup}>
            <Eyebrow parts={['Charging input', 'LIVE']} />
            <View style={styles.metricRow}>
              <Text style={styles.metricBig}>{fmt(chargeAmps, 1)}</Text>
              <Text style={styles.metricUnit}>A</Text>
            </View>
            <Text style={styles.metricSub}>
              {fmt(chargeWatts, 0)} W into pack  ·  {fmt(snapshot.motor_volts, 1)} V
              {snapshot.motor_fet_temp_c !== undefined
                ? `  ·  FET ${fmt(snapshot.motor_fet_temp_c, 0)} °C`
                : ''}
            </Text>

            {ADMIN_MODE && (
              <View style={styles.tuneBlock}>
                <Text style={styles.tuneLabel}>
                  TARGET  ·  {Math.round(engineState.data?.currentAmpsCommanded ?? 0)} A
                </Text>
                <View style={styles.tuneRow}>
                  <TuneButton label="-5" onPress={() => onTunePress(-5)} disabled={tuneBusy || !user} />
                  <TuneButton label="-1" onPress={() => onTunePress(-1)} disabled={tuneBusy || !user} />
                  <TuneButton label="+1" onPress={() => onTunePress(+1)} disabled={tuneBusy || !user} />
                  <TuneButton label="+5" onPress={() => onTunePress(+5)} disabled={tuneBusy || !user} />
                </View>
                <Text style={styles.tuneHint}>
                  Slewed on the VESC at 10 A/s · max {CHARGE_TUNE_MAX_AMPS} A
                </Text>
              </View>
            )}
          </View>
        )}

        <View style={styles.metricGroup}>
          <Eyebrow parts={[wallCharging ? 'Time to full' : 'Time to empty']} />
          <View style={styles.metricRow}>
            <Text style={styles.metricBig}>{etaStr}</Text>
            <Text style={styles.metricUnit}>h:mm</Text>
          </View>
          <Text style={styles.metricSub}>
            {snapshot.lcd_frame_rate_hz
              ? `LCD ${snapshot.lcd_frame_rate_hz.toFixed(1)} Hz`
              : 'LCD —'}
          </Text>
        </View>

        <View style={styles.actionGroup}>
          <SecondaryCTA
            label={wakeBusy ? 'Waking…' : 'Wake LCD'}
            onPress={onWakePress}
            disabled={wakeBusy || !user}
          />
          <SecondaryCTA
            label={acBusy ? 'Pressing…' : 'Aux'}
            onPress={onAcPress}
            disabled={acBusy || !user}
          />
        </View>

        {ADMIN_MODE ? (
          <View style={styles.adminGroup}>
            <Eyebrow parts={['Admin · engine controls']} />
            <SecondaryCTA
              label={startBusy ? 'Starting…' : 'Start Engine'}
              onPress={onStartPress}
              disabled={startBusy || !user}
            />
            <SecondaryCTA
              label={chargeBusy ? 'Engaging…' : 'Charge'}
              onPress={onChargePress}
              disabled={chargeBusy || !user}
            />
            <SecondaryCTA
              label={stopBusy ? 'Stopping…' : 'Stop Engine'}
              onPress={onStopPress}
              disabled={stopBusy || !user}
            />
            <SecondaryCTA
              label={crankBusy ? 'Cranking…' : 'Crank Engine'}
              onPress={onCrankPress}
              disabled={crankBusy || !user}
            />
          </View>
        ) : null}

        <FigCaption number={1} label="Dashboard" detail={DEV_UNIT_ID} />
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  scroll: {
    paddingTop: spacing.md,
    paddingBottom: spacing.xxl,
    gap: spacing.xl,
  },
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.md,
  },
  connectLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  connectHint: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    textAlign: 'center',
    paddingHorizontal: spacing.lg,
  },
  hero: {
    paddingVertical: spacing.xl,
    paddingHorizontal: spacing.md,
    marginHorizontal: spacing.xs,
  },
  heroInner: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    justifyContent: 'center',
    gap: spacing.xs,
  },
  heroNumeric: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: 140,
    lineHeight: 140,
    letterSpacing: -4,
  },
  heroUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    marginBottom: spacing.md,
  },
  heroSub: {
    marginTop: spacing.md,
    textAlign: 'center',
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  modeRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
  },
  modeBadge: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
    paddingVertical: spacing.xxs,
    paddingHorizontal: spacing.xs,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  modeStamp: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  metricGroup: {
    gap: spacing.sm,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.lg,
  },
  metricRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: spacing.xs,
  },
  metricBig: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayLG,
    lineHeight: typeScale.displayLG,
    letterSpacing: tracking.displayTight,
  },
  metricUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    marginBottom: spacing.xs,
  },
  metricSub: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  actionGroup: {
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.lg,
    gap: spacing.sm,
  },
  adminGroup: {
    // Visually separate the admin-only engine controls so it's clear they
    // aren't part of the regular user surface. Same hairline divider + gap
    // pattern as the action group above.
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.lg,
    gap: spacing.sm,
  },
  tuneBlock: {
    marginTop: spacing.sm,
    gap: spacing.xs,
  },
  tuneLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  tuneRow: {
    flexDirection: 'row',
    gap: spacing.xs,
  },
  tuneButton: {
    flex: 1,
    borderWidth: hairline,
    borderColor: colors.textDisplay,
    paddingVertical: spacing.sm,
    alignItems: 'center',
    justifyContent: 'center',
  },
  tuneButtonPressed: {
    opacity: 0.65,
  },
  tuneButtonDisabled: {
    opacity: 0.35,
  },
  tuneButtonLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  tuneHint: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
