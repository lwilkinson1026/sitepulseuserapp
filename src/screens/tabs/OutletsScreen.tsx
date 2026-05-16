import React, { useMemo } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Eyebrow, FigCaption, Screen } from '../../components';
import { useUnitDoc } from '../../hooks/useUnitDoc';
import { useAuth } from '../../hooks/AuthContext';
import { setLight, setRelay } from '../../firebase/commands';
import type {
  LightConfig,
  LightState,
  RelaysConfig,
} from '../../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase B — 3-channel Waveshare RPi Relay Board (B). Channel 1 is the
// security light; channels 2 and 3 are user-labeled aux outputs.
//
// Writes go through `commands.ts` (no direct Firestore writes), reads come
// from `config/relays` + `config/light` + `current/light`. The Pi side
// (pi/relays.py) mirrors the physical override switch into `current/light`
// so we can disable the in-app toggle when the hardware switch is forcing on.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

type LightMode = 'off' | 'on' | 'auto';
const LIGHT_MODES: LightMode[] = ['off', 'on', 'auto'];
const RELAY_MODES: Array<'off' | 'on'> = ['off', 'on'];

export function OutletsScreen() {
  const { user } = useAuth();

  const lightConfig = useUnitDoc<LightConfig>(DEV_UNIT_ID, 'config', 'light');
  const relaysConfig = useUnitDoc<RelaysConfig>(DEV_UNIT_ID, 'config', 'relays');
  const lightState = useUnitDoc<LightState>(DEV_UNIT_ID, 'current', 'light');

  // Derived values come before any conditional return so hook order stays
  // stable across renders (React's rules-of-hooks).
  const lightChannel = (lightConfig.data?.relayChannel ?? 1) as 1 | 2 | 3;
  const auxChannels = useMemo(
    () => ([1, 2, 3] as Array<1 | 2 | 3>).filter((c) => c !== lightChannel),
    [lightChannel],
  );

  const loading =
    lightConfig.loading || relaysConfig.loading || lightState.loading;

  // Don't render any of the cards before we have a uid — the command issuers
  // need it. If the screen is mounted before auth resolves, show a spinner.
  if (!user || loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>LOADING OUTPUTS</Text>
        </View>
      </Screen>
    );
  }

  const currentLightMode: LightMode = (lightConfig.data?.mode ?? 'off') as LightMode;
  const overrideActive = lightState.data?.physicalOverride ?? false;
  const lightOn = lightState.data?.state ?? false;

  const onLightMode = (mode: LightMode) => {
    if (overrideActive) return; // hardware switch wins; UI is read-only
    void setLight(DEV_UNIT_ID, user.uid, mode);
  };

  const onRelayMode = (channel: 1 | 2 | 3, mode: 'off' | 'on') => {
    void setRelay(DEV_UNIT_ID, user.uid, channel, mode);
  };

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.header}>
          <Eyebrow parts={['02 / Outputs', `${auxChannels.length + 1} channels`]} />
          <Text style={styles.headline}>Security{'\n'}light</Text>
        </View>

        {/* ── Light card ─────────────────────────────────────────────── */}
        <View style={styles.card}>
          <View style={styles.cardHeaderRow}>
            <View style={{ flex: 1 }}>
              <Text style={styles.cardLabel}>
                {relaysConfig.data?.channels[String(lightChannel) as '1' | '2' | '3']?.label
                  ?? 'Security Light'}
              </Text>
              <Text style={styles.cardSubLabel}>
                CH {String(lightChannel).padStart(2, '0')}  ·  {lightOn ? 'ENERGIZED' : 'OFF'}
              </Text>
            </View>
            <View style={[styles.indicator, lightOn ? styles.indicatorOn : null]} />
          </View>

          <View style={styles.segmentGroup}>
            {LIGHT_MODES.map((mode) => {
              const active = currentLightMode === mode;
              return (
                <Pressable
                  key={mode}
                  disabled={overrideActive}
                  onPress={() => onLightMode(mode)}
                  style={[
                    styles.segment,
                    active ? styles.segmentActive : null,
                    overrideActive ? styles.segmentLocked : null,
                  ]}
                >
                  <Text
                    style={[
                      styles.segmentLabel,
                      active ? styles.segmentLabelActive : null,
                    ]}
                  >
                    {mode.toUpperCase()}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          {currentLightMode === 'auto' ? (
            <View style={styles.autoDetail}>
              <Text style={styles.autoDetailRow}>
                AUTO-OFF TIMEOUT  ·  {lightConfig.data?.autoTimeoutSec ?? 90} S
              </Text>
              <Text style={styles.autoDetailRow}>
                AFTER-DARK ONLY  ·  {lightConfig.data?.autoOnlyAfterDark ? 'ON' : 'OFF'}
              </Text>
            </View>
          ) : null}

          {overrideActive ? (
            <View style={styles.overrideBanner}>
              <View style={styles.overrideDot} />
              <Text style={styles.overrideText}>LOCAL SWITCH ACTIVE</Text>
            </View>
          ) : null}
        </View>

        {/* ── Aux outputs ────────────────────────────────────────────── */}
        <View style={styles.sectionHeader}>
          <Eyebrow parts={['Aux outputs', `${auxChannels.length} channels`]} />
        </View>

        {auxChannels.map((channel) => {
          const cfg = relaysConfig.data?.channels[String(channel) as '1' | '2' | '3'];
          const mode = (cfg?.mode ?? 'off') as 'off' | 'on';
          return (
            <View key={channel} style={styles.card}>
              <View style={styles.cardHeaderRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.cardLabel}>
                    {cfg?.label ?? `Channel ${channel}`}
                  </Text>
                  <Text style={styles.cardSubLabel}>
                    CH {String(channel).padStart(2, '0')}  ·  {mode === 'on' ? 'ENERGIZED' : 'OFF'}
                  </Text>
                </View>
                <View
                  style={[
                    styles.indicator,
                    mode === 'on' ? styles.indicatorOn : null,
                  ]}
                />
              </View>

              <View style={styles.segmentGroup}>
                {RELAY_MODES.map((m) => {
                  const active = mode === m;
                  return (
                    <Pressable
                      key={m}
                      onPress={() => onRelayMode(channel, m)}
                      style={[
                        styles.segment,
                        active ? styles.segmentActive : null,
                      ]}
                    >
                      <Text
                        style={[
                          styles.segmentLabel,
                          active ? styles.segmentLabelActive : null,
                        ]}
                      >
                        {m.toUpperCase()}
                      </Text>
                    </Pressable>
                  );
                })}
              </View>
            </View>
          );
        })}

        <FigCaption number={2} label="Outputs" detail={DEV_UNIT_ID} />
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
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  header: {
    paddingBottom: spacing.sm,
  },
  headline: {
    marginTop: spacing.md,
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayMD,
    lineHeight: typeScale.displayMD,
    letterSpacing: tracking.displayTight,
  },
  sectionHeader: {
    paddingTop: spacing.lg,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },

  card: {
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    padding: spacing.lg,
    gap: spacing.lg,
  },
  cardHeaderRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
  },
  cardLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  cardSubLabel: {
    marginTop: 2,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  indicator: {
    width: 12,
    height: 12,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  indicatorOn: {
    backgroundColor: colors.textDisplay,
    borderColor: colors.textDisplay,
  },

  segmentGroup: {
    flexDirection: 'row',
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  segment: {
    flex: 1,
    paddingVertical: spacing.sm,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.background,
  },
  segmentActive: {
    backgroundColor: colors.textDisplay,
  },
  segmentLocked: {
    opacity: 0.4,
  },
  segmentLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  segmentLabelActive: {
    color: colors.background,
  },

  autoDetail: {
    gap: spacing.xxs,
  },
  autoDetailRow: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },

  overrideBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    paddingTop: spacing.sm,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  overrideDot: {
    width: 6,
    height: 6,
    backgroundColor: colors.warning,
  },
  overrideText: {
    color: colors.warning,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
