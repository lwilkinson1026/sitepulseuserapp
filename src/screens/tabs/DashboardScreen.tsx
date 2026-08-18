import React, { useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { CornerBrackets, Eyebrow, FigCaption, FuelAlertBanner, Screen, SecondaryCTA } from '../../components';
import { useUnitTelemetry } from '../../hooks/useUnitTelemetry';
import { useUnitDoc } from '../../hooks/useUnitDoc';
import { useAuth } from '../../hooks/AuthContext';
import type { EngineState } from '../../firebase/types';
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
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';
import { useActiveUnit } from '../../hooks/ActiveUnitContext';

// Phase-1 dashboard. Subscribes to units/{unitId}/current/snapshot in
// Firestore. The Pi overwrites that doc every 2–5 s; we render whatever
// arrives and reflect staleness via the LIVE / STALE / OFFLINE eyebrow.
//
// The unit shown comes from useActiveUnit(), which resolves the signed-in
// user's owned unit from Firestore. It is no longer a build-time constant.

// Engine controls (Start / Stop / Charge / Crank) live behind this flag.
// They expose tuning + bench-test surface that doesn't belong on a regular
// user's main screen — set `EXPO_PUBLIC_ADMIN_MODE=true` in .env to surface
// them. Wake LCD and Aux remain visible to all users since those drive the
// Predator's display and AC outlet button. Until a real role system lands
// in Firestore, this is a build-time gate, not authorization — anyone with
// a custom build of the app could flip it. It's strictly UX hygiene.
const ADMIN_MODE = process.env.EXPO_PUBLIC_ADMIN_MODE === 'true';

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
  const {
    unitId,
    unit,
    loading: unitLoading,
    hasNoUnits,
    error: unitError,
  } = useActiveUnit();
  const { loading, snapshot, staleness, error } = useUnitTelemetry(unitId);
  // Subscribe to the Pi-published engine runtime state so we can flip the
  // charging-input tile on only when the regen loop is actively running.
  // Same doc the ScheduleScreen reads — the Pi merges scheduler and runtime
  // fields together, so this read may contain both shapes.
  const engineState = useUnitDoc<EngineState>(unitId, 'current', 'engine');
  const { user } = useAuth();
  // 'pending' covers the brief window between tap and Pi ack. We don't
  // round-trip the ack into here yet — just debounce by time to prevent
  // double-taps from queueing two presses back-to-back.
  const [wakeBusy, setWakeBusy] = useState(false);
  const [acBusy, setAcBusy] = useState(false);

  const onWakePress = async () => {
    if (!user || !unitId || wakeBusy) return;
    setWakeBusy(true);
    try {
      await wakeLcd(unitId, user.uid);
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
    if (!user || !unitId || acBusy) return;
    setAcBusy(true);
    try {
      await toggleAc(unitId, user.uid);
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
    if (!user || !unitId || crankBusy) return;
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
      await crankEngine(unitId, user.uid);
    } catch (e) {
      console.warn('[dashboard] crankEngine failed', e);
    } finally {
      setTimeout(() => setCrankBusy(false), 6000);
    }
  };

  const [chargeBusy, setChargeBusy] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);

  const onChargePress = async () => {
    if (!user || !unitId || chargeBusy) return;
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
      await chargeEngine(unitId, user.uid);
    } catch (e) {
      console.warn('[dashboard] chargeEngine failed', e);
    } finally {
      // Charge ack is fast (it just spawns the bg thread). 1.5s is enough;
      // the actual charging continues in the background.
      setTimeout(() => setChargeBusy(false), 1500);
    }
  };

  const onStopPress = async () => {
    if (!user || !unitId || stopBusy) return;
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
      await stopEngine(unitId, user.uid);
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
    if (!user || !unitId || tuneBusy) return;
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
      await tuneCharge(unitId, user.uid, next);
    } catch (e) {
      console.warn('[dashboard] tuneCharge failed', e);
    } finally {
      setTimeout(() => setTuneBusy(false), 300);
    }
  };

  const onStartPress = async () => {
    if (!user || !unitId || startBusy) return;
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
      await startEngine(unitId, user.uid);
    } catch (e) {
      console.warn('[dashboard] startEngine failed', e);
    } finally {
      // Lock out for ~10s — covers worst-case sequence: choke settle (0.5s)
      // + spark settle (0.2s) + crank (≤4s) + post-catch (2s) + slack for
      // the Pi to publish the final state.
      setTimeout(() => setStartBusy(false), 10000);
    }
  };

  // Unit resolution comes first, and each outcome gets its own state. Without
  // these, a null unitId falls through to "NO TELEMETRY YET" below, which
  // blames the generator for what is really "we don't know which generator
  // yet" or "you don't have one".
  if (unitLoading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>FINDING YOUR GENERATOR</Text>
        </View>
      </Screen>
    );
  }

  if (unitError) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={[styles.connectLabel, { color: colors.danger }]}>
            CANNOT LOOK UP YOUR GENERATOR
          </Text>
          <Text style={styles.connectHint}>{unitError.message}</Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
        </View>
      </Screen>
    );
  }

  if (hasNoUnits || !unitId) {
    return (
      <Screen>
        <View style={styles.center}>
          <Text style={styles.connectLabel}>NO GENERATOR LINKED</Text>
          <Text style={styles.connectHint}>
            This account isn&apos;t linked to a generator yet.
          </Text>
          <Text style={styles.connectHint}>UID  ·  {user?.uid ?? 'NONE'}</Text>
        </View>
      </Screen>
    );
  }

  if (loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>SUBSCRIBING  ·  {unitId}</Text>
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
          <Text style={styles.connectHint}>UNIT  ·  {unitId}</Text>
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
          <Text style={styles.connectHint}>UNIT  ·  {unitId}</Text>
        </View>
      </Screen>
    );
  }

  // State of charge has exactly one source: what the Predator's own LCD
  // reports. No blank is filled in, and no second estimate stands in.
  //
  // This used to be three layers deep — LCD value, then a 20 s sticky memory
  // of the last one, then a voltage-derived estimate — and both extra layers
  // were compensating for the same thing: the Pi's I²C tap dropped frames, so
  // the number visibly cycled between sources. That is fixed at the source now
  // (the publisher votes each reading across a whole sample window), so the
  // scaffolding can go.
  //
  // Dropping the voltage estimate is not only a simplification. Measured on
  // UNIT-002 2026-08-18: the panel read 6 % while the voltage curve claimed
  // 13 % at 46.9 V — a 7-point overestimate on a nearly-empty pack, in the
  // direction that tells someone they have range they do not have. A blank is
  // a better answer than a confident wrong one.
  const live = snapshot.battery_soc;
  const socStr =
    typeof live === 'number' && Number.isFinite(live) ? fmt(live, 0) : '—';
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
  // Controller health. `pi_temp_warn` is precomputed on the Pi so the app and
  // the notifier can't disagree about what counts as hot. Fall back to a local
  // comparison only if the flag is absent but a threshold was published.
  const piHot =
    snapshot.pi_temp_warn === true ||
    (snapshot.pi_temp_warn === undefined &&
      snapshot.pi_temp_c !== undefined &&
      snapshot.pi_temp_c !== null &&
      snapshot.pi_temp_warn_c !== undefined &&
      snapshot.pi_temp_c >= snapshot.pi_temp_warn_c);
  // Live throttling is the urgent signal; the sticky since-boot flags are a
  // quieter "this unit has been in trouble before" note worth surfacing,
  // because they are invisible from any single spot reading.
  const piThrottlingNow =
    snapshot.pi_throttled_now === true ||
    snapshot.pi_soft_temp_limit_now === true ||
    snapshot.pi_freq_capped_now === true;
  const piHealthSub = snapshot.pi_undervoltage_now
    ? 'UNDERVOLTAGE — check power supply'
    : piThrottlingNow
      ? 'THROTTLING NOW'
      : snapshot.pi_undervoltage_since_boot
        ? 'undervoltage seen since boot'
        : snapshot.pi_throttled_since_boot || snapshot.pi_soft_temp_limit_since_boot
          ? 'throttled earlier since boot'
          : `load ${snapshot.pi_load_1min?.toFixed(2) ?? '—'}`;

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
          parts={[unitId ?? '—', STALENESS_LABEL[staleness] ?? '—']}
          live
          staleness={stalenessKind}
        />

        <FuelAlertBanner unitId={unitId} />

        <CornerBrackets style={styles.hero}>
          <View style={styles.heroInner}>
            <Text style={styles.heroNumeric}>{socStr}</Text>
            <Text style={styles.heroUnit}>%</Text>
          </View>
          <Text style={styles.heroSub}>
            STATE OF CHARGE  ·{' '}
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

        {/* ── Controller health ─────────────────────────────────────
            The Pi's own temperature. UNIT-002 spent hours at 85 C and
            throttling with nothing surfacing it; the peripherals all
            reported themselves while the host running them was invisible.
            Rendered only when the Pi publishes it, so an older publisher
            simply omits the group rather than showing an empty one. */}
        {snapshot.pi_temp_c !== undefined && snapshot.pi_temp_c !== null && (
          <View style={styles.metricGroup}>
            <Eyebrow parts={['Controller', piHot ? 'HOT' : 'NOMINAL']} />
            <View style={styles.metricRow}>
              <Text style={[styles.metricBig, piHot ? styles.metricAlert : null]}>
                {snapshot.pi_temp_c.toFixed(1)}
              </Text>
              <Text style={styles.metricUnit}>°C</Text>
            </View>
            <Text style={styles.metricSub}>{piHealthSub}</Text>
          </View>
        )}

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

        <FigCaption number={1} label="Dashboard" detail={unitId ?? undefined} />
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
  metricAlert: {
    color: colors.danger,
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
