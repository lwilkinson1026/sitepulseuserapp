import React, { useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  View,
} from 'react-native';
import { Image as ExpoImage } from 'expo-image';
import { useVideoPlayer, VideoView } from 'expo-video';
import { Eyebrow, FigCaption, Screen } from '../../components';
import { useUnitDoc } from '../../hooks/useUnitDoc';
import { useUnitEvents, type EventEntry } from '../../hooks/useUnitEvents';
import { useStorageUrl } from '../../hooks/useStorageUrl';
import { useAuth } from '../../hooks/AuthContext';
import {
  armSentry,
  disarmSentry,
  startCameraStream,
  updateSentryConfig,
} from '../../firebase/commands';
import type { SentryConfig, SentryState } from '../../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase D1 — sentry tab.
//
// Reads config/sentry + current/sentry + the last N motion events.
// Writes are issued via the typed command helpers; the Pi reacts and
// mirrors state back. Live HLS streaming is Phase D2 — the "Go Live"
// button issues `camera.startStream` but the URL pipeline isn't wired
// until a Cloudflare Stream Live Input is provisioned.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

const SENSITIVITY_STEPS: Array<{ label: string; value: number }> = [
  { label: 'LOW',    value: 0.2 },
  { label: 'MED',    value: 0.4 },
  { label: 'HIGH',   value: 0.7 },
];

export function SentryScreen() {
  const { user } = useAuth();
  const config = useUnitDoc<SentryConfig>(DEV_UNIT_ID, 'config', 'sentry');
  const state = useUnitDoc<SentryState>(DEV_UNIT_ID, 'current', 'sentry');
  const events = useUnitEvents(DEV_UNIT_ID, { kindFilter: 'motion', pageSize: 20 });

  const loading = config.loading || state.loading;

  if (!user || loading) {
    return (
      <Screen>
        <View style={styles.center}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.connectLabel}>LOADING SENTRY</Text>
        </View>
      </Screen>
    );
  }

  const enabled = config.data?.enabled ?? false;
  const sensitivity = config.data?.sensitivity ?? 0.4;
  const autoLight = config.data?.autoLight ?? true;
  const notifyOnMotion = config.data?.notifyOnMotion ?? true;
  const armed = state.data?.armed ?? false;

  const onToggleEnabled = (next: boolean) => {
    void (next ? armSentry(DEV_UNIT_ID, user.uid) : disarmSentry(DEV_UNIT_ID, user.uid));
  };

  const onSetSensitivity = (value: number) => {
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { sensitivity: value });
  };

  const onToggleAutoLight = (next: boolean) => {
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { autoLight: next });
  };

  const onToggleNotify = (next: boolean) => {
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { notifyOnMotion: next });
  };

  const onGoLive = () => {
    void startCameraStream(DEV_UNIT_ID, user.uid);
  };

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.header}>
          <Eyebrow
            parts={['04 / Sentry', armed ? 'armed' : 'disarmed']}
            live={armed}
          />
          <Text style={styles.headline}>Camera{'\n'}sentry</Text>
        </View>

        {/* ── Master arm ─────────────────────────────────────────────── */}
        <View style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Sentry</Text>
            <Text style={styles.rowSubLabel}>
              {enabled
                ? 'CAMERA WATCHING  ·  MOTION TRIGGERS PUSH + CLIP'
                : 'DISARMED  ·  NO MOTION DETECTION'}
            </Text>
          </View>
          <Switch
            value={enabled}
            onValueChange={onToggleEnabled}
            trackColor={{ false: colors.borderStrong, true: colors.textDisplay }}
            thumbColor={enabled ? colors.background : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        {/* ── Sensitivity ────────────────────────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Sensitivity', `${(sensitivity * 100).toFixed(0)}%`]} />
          <View style={styles.segmentGroup}>
            {SENSITIVITY_STEPS.map((step) => {
              const active = Math.abs(sensitivity - step.value) < 0.05;
              return (
                <Pressable
                  key={step.label}
                  onPress={() => onSetSensitivity(step.value)}
                  style={[styles.segment, active ? styles.segmentActive : null]}
                >
                  <Text
                    style={[
                      styles.segmentLabel,
                      active ? styles.segmentLabelActive : null,
                    ]}
                  >
                    {step.label}
                  </Text>
                </Pressable>
              );
            })}
          </View>
        </View>

        {/* ── Toggles ────────────────────────────────────────────────── */}
        <View style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Trigger security light on motion</Text>
            <Text style={styles.rowSubLabel}>
              CH 01 PULSES WHEN MOTION FIRES IN AUTO MODE
            </Text>
          </View>
          <Switch
            value={autoLight}
            onValueChange={onToggleAutoLight}
            trackColor={{ false: colors.borderStrong, true: colors.textDisplay }}
            thumbColor={autoLight ? colors.background : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        <View style={styles.row}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Push notification on motion</Text>
            <Text style={styles.rowSubLabel}>
              {notifyOnMotion
                ? 'ENABLED  ·  EVENT SENT TO REGISTERED DEVICES'
                : 'DISABLED  ·  EVENTS LOGGED BUT NO PUSH'}
            </Text>
          </View>
          <Switch
            value={notifyOnMotion}
            onValueChange={onToggleNotify}
            trackColor={{ false: colors.borderStrong, true: colors.textDisplay }}
            thumbColor={notifyOnMotion ? colors.background : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        {/* ── Live stream (D2 placeholder) ───────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Live stream', 'phase D2']} />
          <View style={styles.streamCard}>
            <Text style={styles.streamPlaceholder}>
              STREAMING NOT CONFIGURED
            </Text>
            <Text style={styles.streamHint}>
              PROVISION A CLOUDFLARE STREAM LIVE INPUT, THEN TAP GO LIVE
            </Text>
            <Pressable
              onPress={onGoLive}
              style={[styles.streamButton, styles.streamButtonDisabled]}
            >
              <Text style={styles.streamButtonLabel}>GO LIVE</Text>
            </Pressable>
          </View>
        </View>

        {/* ── Motion events ──────────────────────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow parts={['Recent motion', `${events.events.length} events`]} />
          {events.loading ? (
            <ActivityIndicator color={colors.textMuted} />
          ) : events.events.length === 0 ? (
            <Text style={styles.emptyState}>
              NO MOTION EVENTS YET
            </Text>
          ) : (
            events.events.map((evt) => (
              <MotionEventRow key={evt.id} event={evt} />
            ))
          )}
        </View>

        <FigCaption number={4} label="Sentry" detail={DEV_UNIT_ID} />
      </ScrollView>
    </Screen>
  );
}

// ─── motion event row with inline player ──────────────────────────────────

function MotionEventRow({ event }: { event: EventEntry }) {
  const [expanded, setExpanded] = useState(false);
  const clipPath = (event.clipPath as string | undefined) ?? null;
  const thumbPath = (event.thumbnailPath as string | undefined) ?? null;

  const thumb = useStorageUrl(thumbPath);
  const clip = useStorageUrl(expanded ? clipPath : null);

  const player = useVideoPlayer(clip.url ?? '', (p) => {
    p.loop = false;
    p.muted = true;
  });

  const ts = event.at?.toDate ? event.at.toDate() : null;
  const tsLabel = ts ? ts.toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }) : '—';

  return (
    <Pressable
      onPress={() => setExpanded((v) => !v)}
      style={styles.eventRow}
      disabled={!clipPath}
    >
      <View style={styles.eventHeader}>
        <Text style={styles.eventKind}>MOTION</Text>
        <Text style={styles.eventTs}>{tsLabel.toUpperCase()}</Text>
      </View>

      {expanded && clip.url ? (
        <VideoView
          player={player}
          style={styles.video}
          allowsFullscreen
          allowsPictureInPicture
          nativeControls
        />
      ) : thumb.url ? (
        <ExpoImage
          source={{ uri: thumb.url }}
          style={styles.thumb}
          contentFit="cover"
          transition={120}
        />
      ) : (
        <View style={[styles.thumb, styles.thumbPlaceholder]}>
          <Text style={styles.thumbPlaceholderText}>
            {clipPath ? 'TAP TO LOAD CLIP' : 'CLIP NOT AVAILABLE'}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  scroll: {
    paddingTop: spacing.md,
    paddingBottom: spacing.xxl,
    gap: spacing.lg,
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

  section: {
    gap: spacing.md,
    paddingTop: spacing.md,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    paddingVertical: spacing.sm,
  },
  rowLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  rowSubLabel: {
    marginTop: 2,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
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
    backgroundColor: colors.background,
  },
  segmentActive: {
    backgroundColor: colors.textDisplay,
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

  streamCard: {
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    padding: spacing.lg,
    gap: spacing.sm,
    alignItems: 'center',
  },
  streamPlaceholder: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  streamHint: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    textAlign: 'center',
  },
  streamButton: {
    marginTop: spacing.sm,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.xl,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
  },
  streamButtonDisabled: {
    opacity: 0.4,
  },
  streamButtonLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },

  emptyState: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    textAlign: 'center',
    paddingVertical: spacing.lg,
  },

  eventRow: {
    gap: spacing.sm,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.sm,
  },
  eventHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'baseline',
  },
  eventKind: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  eventTs: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  thumb: {
    width: '100%',
    aspectRatio: 16 / 9,
    backgroundColor: colors.surface,
  },
  thumbPlaceholder: {
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: hairline,
    borderColor: colors.borderHairline,
  },
  thumbPlaceholderText: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  video: {
    width: '100%',
    aspectRatio: 16 / 9,
    backgroundColor: colors.background,
  },
});
