// Live engine status bar.
//
// Renders a thin sticky banner at the top of the app whenever the engine
// is in any state other than `idle` — covers cranking, running, charging,
// stopping, and all failed_* states. Hidden when the engine is at rest so
// it doesn't add chrome during normal use.
//
// Layout:
//   ┌────────────────────────────────────────────────────────────────┐
//   │ ●  CHARGING · 2,180 RPM · 420 W                          VIEW │
//   └────────────────────────────────────────────────────────────────┘
//
// The leading dot pulses on live states (running, charging) using the
// same 1.6 s breath cycle the rest of the UI uses for "live" indicators.
// Transitional states (starting, cranking, stopping) get an amber static
// dot. Failed states get a red static dot and the bar stays visible until
// the user dismisses (tap) — gives them time to see the failure mode.
//
// Tap opens a sheet with full telemetry. For now we just call onPress
// (parent owns the modal); component is presentational.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Animated,
  Pressable,
  StyleSheet,
  Text,
  View,
  ViewStyle,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useUnitDoc } from '../hooks/useUnitDoc';
import { EngineState, EngineMachineState, TelemetrySnapshot } from '../firebase/types';
import { colors, fonts, motion, spacing } from '../theme';

// Match the convention used in screens — read the dev unit from env,
// fall back to UNIT-001 for bench testing. Will swap to a paired-unit
// context once the pair flow lands.
const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

// Pole pairs for the starter-generator motor — used to convert the eRPM
// field from VESC STATUS_1 into mechanical RPM that's meaningful to a
// human. 28 magnets ÷ 2 = 14 pole pairs (confirmed empirically).
const MOTOR_POLE_PAIRS = 14;

// States that should hide the bar entirely.
const HIDDEN_STATES = new Set<EngineMachineState>(['idle']);

// Severity classification drives the dot color and pulse animation.
type Severity = 'transitional' | 'live' | 'failed';

function classify(state: EngineMachineState | undefined): Severity | null {
  if (!state || HIDDEN_STATES.has(state)) return null;
  if (state.startsWith('failed_')) return 'failed';
  if (state === 'running' || state === 'charging') return 'live';
  return 'transitional';
}

// Compact human label for each state. Keep these short — they share one
// line with the live metric.
function labelFor(state: EngineMachineState): string {
  switch (state) {
    case 'starting':              return 'STARTING';
    case 'cranking':              return 'CRANKING';
    case 'running':               return 'ENGINE RUNNING';
    case 'charging':              return 'CHARGING';
    case 'stopping':              return 'STOPPING';
    case 'failed_no_load':        return 'ENGINE: NO LOAD';
    case 'failed_did_not_catch':  return 'ENGINE: DID NOT CATCH';
    case 'failed_timeout':        return 'ENGINE: TIMEOUT';
    case 'failed_overtemp':       return 'ENGINE: OVERTEMP';
    case 'failed_engine_bogged':  return 'ENGINE BOGGED';
    case 'failed_low_voltage':    return 'ENGINE: LOW VOLTAGE';
    case 'failed_error':          return 'ENGINE: ERROR';
    default:                      return String(state).toUpperCase();
  }
}

// Live metric to append after the state label. Returns null when there's
// nothing useful to show for this state (most failed_* states; idle).
function metricFor(
  state: EngineMachineState,
  snap: TelemetrySnapshot | null,
): string | null {
  const eRpm = snap?.motor_rpm;
  const volts = snap?.motor_volts;
  const ampsIn = snap?.motor_amps_in;

  // Mechanical RPM for display
  const mechRpm =
    typeof eRpm === 'number' && Number.isFinite(eRpm)
      ? Math.round(Math.abs(eRpm) / MOTOR_POLE_PAIRS)
      : null;

  if (state === 'running') {
    if (mechRpm !== null && mechRpm > 0) {
      return `${mechRpm.toLocaleString()} RPM`;
    }
    return null;
  }

  if (state === 'charging') {
    // Pack-side charge power = pack volts × DC input current (sign:
    // negative motor_amps_in during regen = current flowing into pack).
    const parts: string[] = [];
    if (mechRpm !== null && mechRpm > 0) {
      parts.push(`${mechRpm.toLocaleString()} RPM`);
    }
    if (
      typeof volts === 'number' && typeof ampsIn === 'number'
      && Number.isFinite(volts) && Number.isFinite(ampsIn)
    ) {
      const watts = Math.round(Math.abs(volts * ampsIn));
      if (watts > 0) parts.push(`${watts.toLocaleString()} W`);
    }
    return parts.length ? parts.join(' · ') : null;
  }

  if (state === 'cranking') {
    // Brief — eRPM rises through the cranking window, peak motor current
    // is the more interesting datapoint. But that's only in metadata,
    // not the snapshot. Just leave the cranking text alone for now.
    return null;
  }

  return null;
}

interface EngineStatusBarProps {
  onPress?: () => void;
  // Allow callers to override the unit; defaults to DEV_UNIT_ID.
  unitId?: string;
  style?: ViewStyle;
}

export function EngineStatusBar({
  onPress,
  unitId = DEV_UNIT_ID,
  style,
}: EngineStatusBarProps) {
  const engineDoc = useUnitDoc<EngineState>(unitId, 'current', 'engine');
  const snapDoc = useUnitDoc<TelemetrySnapshot>(unitId, 'current', 'snapshot');

  const state = engineDoc.data?.state;
  const severity = classify(state);

  // Track whether the user has dismissed a failed state. Reset whenever
  // the underlying state changes (so a fresh failure shows the bar again).
  const [dismissedState, setDismissedState] = useState<EngineMachineState | null>(null);
  useEffect(() => {
    if (state && dismissedState && state !== dismissedState) {
      setDismissedState(null);
    }
  }, [state, dismissedState]);
  const isDismissed = severity === 'failed' && dismissedState === state;

  // Pulse animation for live states only.
  const pulse = useRef(new Animated.Value(0.4)).current;
  useEffect(() => {
    if (severity !== 'live') {
      pulse.setValue(severity === 'transitional' ? 0.7 : 0.9);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(pulse, {
          toValue: 1,
          duration: motion.livePulse / 2,
          useNativeDriver: true,
        }),
        Animated.timing(pulse, {
          toValue: 0.4,
          duration: motion.livePulse / 2,
          useNativeDriver: true,
        }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [severity, pulse]);

  // Compose label + metric.
  const metric = useMemo(() => {
    if (!state) return null;
    return metricFor(state, snapDoc.data);
  }, [state, snapDoc.data]);

  // Don't render anything if there's nothing to show.
  if (!severity || !state || isDismissed) return null;

  const dotColor = (() => {
    switch (severity) {
      case 'live':         return colors.live;
      case 'transitional': return colors.warning;
      case 'failed':       return colors.danger;
    }
  })();

  const handlePress = () => {
    if (severity === 'failed') {
      // Tap on a failed banner dismisses it. The user can re-trigger by
      // explicitly viewing engine details from the dashboard.
      setDismissedState(state);
    }
    onPress?.();
  };

  return (
    <SafeAreaView edges={['top']} style={[styles.safeBackground, style]}>
      <Pressable
        onPress={handlePress}
        style={({ pressed }) => [
          styles.bar,
          pressed ? styles.barPressed : null,
        ]}
        // Accessibility: announce what's happening in a single phrase.
        accessibilityRole="button"
        accessibilityLabel={[
          'Engine status:',
          labelFor(state),
          metric ?? '',
          severity === 'failed' ? '. Tap to dismiss' : '. Tap for details',
        ].join(' ')}
      >
        <Animated.View
          style={[
            styles.dot,
            { backgroundColor: dotColor, opacity: pulse },
          ]}
        />
        <Text style={styles.label} numberOfLines={1}>
          {labelFor(state)}
          {metric ? (
            <Text style={styles.metric}>{`  ·  ${metric}`}</Text>
          ) : null}
        </Text>
        <Text style={styles.affordance}>
          {severity === 'failed' ? 'DISMISS' : 'VIEW'}
        </Text>
      </Pressable>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeBackground: {
    backgroundColor: colors.surface,
  },
  bar: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: spacing.lg,
    paddingVertical: 10,
    borderBottomWidth: 1,
    borderBottomColor: colors.borderHairline,
    gap: 10,
  },
  barPressed: {
    backgroundColor: colors.surfaceElevated,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  label: {
    flex: 1,
    fontFamily: fonts.mono,
    fontSize: 12,
    letterSpacing: 1,
    color: colors.textDisplay,
  },
  metric: {
    fontFamily: fonts.mono,
    fontSize: 12,
    letterSpacing: 1,
    color: colors.textBody,
  },
  affordance: {
    fontFamily: fonts.mono,
    fontSize: 10,
    letterSpacing: 1.5,
    color: colors.textMuted,
  },
});
