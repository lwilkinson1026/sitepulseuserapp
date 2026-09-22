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
import { useOptimistic } from '../../hooks/useOptimistic';
import { useAuth } from '../../hooks/AuthContext';
import { setLight, setRelay } from '../../firebase/commands';
import type {
  EnclosureFanConfig,
  EnclosureFanState,
  EngineConfig,
  LightConfig,
  LightState,
  RelaysConfig,
  RelaysState,
} from '../../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';
import { useActiveUnit } from '../../hooks/ActiveUnitContext';

// Phase B — 3-channel Waveshare RPi Relay Board (B). Channel 1 is the
// security light; channels 2 and 3 are user-labeled aux outputs.
//
// Writes go through `commands.ts` (no direct Firestore writes), reads come
// from `config/relays` + `config/light` + `current/light`. The Pi side
// (pi/relays.py) mirrors the physical override switch into `current/light`
// so we can disable the in-app toggle when the hardware switch is forcing on.

type LightMode = 'off' | 'on' | 'auto';
const LIGHT_MODES: LightMode[] = ['off', 'on', 'auto'];
// Aux channels get the full off/on/auto selector, except the spark channel
// (engine-managed), which is plain off/on — see auxModesFor below.
const RELAY_MODES: LightMode[] = ['off', 'on', 'auto'];
const RELAY_MODES_NO_AUTO: LightMode[] = ['off', 'on'];

const ENCLOSURE_REASON_LABEL: Record<EnclosureFanState['reason'], string> = {
  hot: 'PI HOT',
  cool: 'PI COOL',
  engine_running: 'ENGINE RUNNING',
  critical_temp: 'OVER-TEMP OVERRIDE',
  temp_unavailable: 'NO TEMP READING',
  manual_on: 'MANUAL',
  manual_off: 'MANUAL',
};

export function OutletsScreen() {
  const { unitId } = useActiveUnit();
  const { user } = useAuth();

  const lightConfig = useUnitDoc<LightConfig>(unitId, 'config', 'light');
  const relaysConfig = useUnitDoc<RelaysConfig>(unitId, 'config', 'relays');
  const lightState = useUnitDoc<LightState>(unitId, 'current', 'light');
  const relaysState = useUnitDoc<RelaysState>(unitId, 'current', 'relays');
  const engineConfig = useUnitDoc<EngineConfig>(unitId, 'config', 'engine');
  const enclosureFanConfig = useUnitDoc<EnclosureFanConfig>(unitId, 'config', 'enclosureFan');
  const enclosureFanState = useUnitDoc<EnclosureFanState>(unitId, 'current', 'enclosureFan');

  // Derived values come before any conditional return so hook order stays
  // stable across renders (React's rules-of-hooks).
  const lightChannel = (lightConfig.data?.relayChannel ?? 1) as 1 | 2 | 3;
  const sparkChannel = (engineConfig.data?.start?.sparkRelayChannel ?? 2) as 1 | 2 | 3;
  // The fan channel is hardwired to engine-follow on the Pi and not
  // user-controllable, so it's hidden from this screen entirely.
  const fanChannel = (engineConfig.data?.fanRelayChannel ?? 3) as 1 | 2 | 3;
  // A unit can hand a channel to the enclosure-fan thermostat. If that's the
  // light's channel the unit has no light at all (the Pi refuses light.set).
  const enclosureChannel = enclosureFanConfig.data?.relayChannel ?? null;
  const hasLight = enclosureChannel !== lightChannel;
  const auxChannels = useMemo(
    () =>
      ([1, 2, 3] as Array<1 | 2 | 3>).filter(
        (c) => c !== lightChannel && c !== fanChannel && c !== enclosureChannel,
      ),
    [lightChannel, fanChannel, enclosureChannel],
  );
  // Spark channel is driven by the engine sequence, so engine-follow 'auto'
  // would be meaningless there — offer it only on the other aux channel(s).
  const auxModesFor = (channel: 1 | 2 | 3): LightMode[] =>
    channel === sparkChannel ? RELAY_MODES_NO_AUTO : RELAY_MODES;

  // One optimistic value per relay channel. We pre-allocate all three hooks
  // unconditionally (rules-of-hooks: never call hooks in a loop or branch),
  // even though only two are typically aux and one is the light.
  // The enclosure-fan channel's truth is config/enclosureFan.mode, which the
  // Pi defaults to 'auto' (thermostat) when nothing has been set yet.
  const truthFor = (key: '1' | '2' | '3'): LightMode =>
    String(enclosureChannel) === key
      ? enclosureFanConfig.data?.mode ?? 'auto'
      : relaysConfig.data?.channels?.[key]?.mode ?? 'off';
  const truth1 = truthFor('1');
  const truth2 = truthFor('2');
  const truth3 = truthFor('3');
  const [mode1, setMode1] = useOptimistic<LightMode>(truth1);
  const [mode2, setMode2] = useOptimistic<LightMode>(truth2);
  const [mode3, setMode3] = useOptimistic<LightMode>(truth3);
  const channelModes: Record<'1' | '2' | '3', LightMode> = {
    '1': mode1, '2': mode2, '3': mode3,
  };
  const setChannelMode: Record<'1' | '2' | '3', (m: LightMode) => void> = {
    '1': setMode1, '2': setMode2, '3': setMode3,
  };

  const loading =
    lightConfig.loading || relaysConfig.loading || lightState.loading;

  // Don't render any of the cards before we have a uid — the command issuers
  // need it. If the screen is mounted before auth resolves, show a spinner.
  if (!user || !unitId || loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>LOADING OUTPUTS</Text>
        </View>
      </Screen>
    );
  }

  const lightChannelKey = String(lightChannel) as '1' | '2' | '3';
  const currentLightMode: LightMode = channelModes[lightChannelKey];
  const overrideActive = lightState.data?.physicalOverride ?? false;
  const lightOn = lightState.data?.state ?? false;

  const onLightMode = (mode: LightMode) => {
    if (overrideActive) return; // hardware switch wins; UI is read-only
    setChannelMode[lightChannelKey](mode);
    void setLight(unitId, user.uid, mode);
  };

  const onRelayMode = (channel: 1 | 2 | 3, mode: LightMode) => {
    setChannelMode[String(channel) as '1' | '2' | '3'](mode);
    void setRelay(unitId, user.uid, channel, mode);
  };

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.header}>
          <Eyebrow
            parts={[
              '02 / Outputs',
              `${auxChannels.length + (hasLight ? 1 : 0) + (enclosureChannel ? 1 : 0)} channels`,
            ]}
          />
          <Text style={styles.headline}>
            {hasLight ? `Security\nlight` : `Outputs`}
          </Text>
        </View>

        {/* ── Light card ─────────────────────────────────────────────── */}
        {hasLight ? (
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
        ) : null}

        {/* ── Enclosure fan (thermostat-driven) ──────────────────────── */}
        {enclosureChannel ? (() => {
          const key = String(enclosureChannel) as '1' | '2' | '3';
          const mode = channelModes[key];
          const fanOn = enclosureFanState.data?.state
            ?? relaysState.data?.channels?.[key]?.state
            ?? false;
          const reason = enclosureFanState.data?.reason;
          const tempC = enclosureFanState.data?.tempC;
          return (
            <View style={styles.card}>
              <View style={styles.cardHeaderRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.cardLabel}>Enclosure fan</Text>
                  <Text style={styles.cardSubLabel}>
                    CH {key.padStart(2, '0')}  ·  {fanOn ? 'RUNNING' : 'OFF'}
                    {reason ? `  ·  ${ENCLOSURE_REASON_LABEL[reason] ?? reason}` : ''}
                  </Text>
                </View>
                <View style={[styles.indicator, fanOn ? styles.indicatorOn : null]} />
              </View>
              <View style={styles.segmentGroup}>
                {RELAY_MODES.map((m) => {
                  const active = mode === m;
                  return (
                    <Pressable
                      key={m}
                      onPress={() => onRelayMode(enclosureChannel, m)}
                      style={[styles.segment, active ? styles.segmentActive : null]}
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
              {mode === 'auto' ? (
                <View style={styles.autoDetail}>
                  <Text style={styles.autoDetailRow}>
                    ON AT {enclosureFanConfig.data?.onTempC ?? 60} °C  ·  OFF AT{' '}
                    {enclosureFanConfig.data?.offTempC ?? 50} °C
                  </Text>
                  {tempC != null ? (
                    <Text style={styles.autoDetailRow}>
                      PI WAS {tempC} °C AT LAST CHANGE
                    </Text>
                  ) : null}
                </View>
              ) : null}
            </View>
          );
        })() : null}

        {/* ── Aux outputs ────────────────────────────────────────────── */}
        {auxChannels.length > 0 ? (
          <View style={styles.sectionHeader}>
            <Eyebrow parts={['Aux outputs', `${auxChannels.length} channels`]} />
          </View>
        ) : null}

        {auxChannels.map((channel) => {
          const channelKey = String(channel) as '1' | '2' | '3';
          const cfg = relaysConfig.data?.channels[channelKey];
          // Optimistic mode wins; truth restores when the Pi mirrors back.
          const mode = channelModes[channelKey];
          // In 'auto' the relay is engine-driven, so the live mirror is the
          // only source of truth for whether it's actually energized.
          const liveOn = relaysState.data?.channels?.[channelKey]?.state ?? false;
          const energized = mode === 'auto' ? liveOn : mode === 'on';
          const statusText =
            mode === 'auto'
              ? `AUTO · ${energized ? 'ENGINE RUNNING' : 'IDLE'}`
              : energized ? 'ENERGIZED' : 'OFF';
          return (
            <View key={channel} style={styles.card}>
              <View style={styles.cardHeaderRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.cardLabel}>
                    {cfg?.label ?? `Channel ${channel}`}
                  </Text>
                  <Text style={styles.cardSubLabel}>
                    CH {String(channel).padStart(2, '0')}  ·  {statusText}
                  </Text>
                </View>
                <View
                  style={[
                    styles.indicator,
                    energized ? styles.indicatorOn : null,
                  ]}
                />
              </View>

              <View style={styles.segmentGroup}>
                {auxModesFor(channel).map((m) => {
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

        <FigCaption number={2} label="Outputs" detail={unitId ?? undefined} />
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
