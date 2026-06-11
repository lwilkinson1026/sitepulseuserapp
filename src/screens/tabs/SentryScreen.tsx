import React, { useEffect, useRef, useState } from 'react';
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
import { useOptimistic } from '../../hooks/useOptimistic';
import { useUnitEvents, type EventEntry } from '../../hooks/useUnitEvents';
import { useStorageUrl } from '../../hooks/useStorageUrl';
import { useAuth } from '../../hooks/AuthContext';
import {
  armSentry,
  disarmSentry,
  startCameraStream,
  stopCameraStream,
  updateSentryConfig,
} from '../../firebase/commands';
import type { CameraState, SentryConfig, SentryState } from '../../firebase/types';
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
  const camera = useUnitDoc<CameraState & { error?: string | null }>(DEV_UNIT_ID, 'current', 'camera');
  const events = useUnitEvents(DEV_UNIT_ID, { kindFilter: 'motion', pageSize: 20 });

  // ALL hook calls up-front so hook order is consistent across renders
  // (React's rules-of-hooks). The early loading return below must come
  // AFTER every hook call.

  // Optimistic state so toggles snap on tap. Real value arrives once the Pi
  // ACKs and mirrors the config back into Firestore.
  const [enabled, setEnabledOptimistic] =
    useOptimistic(config.data?.enabled ?? false);
  const [sensitivity, setSensitivityOptimistic] =
    useOptimistic(config.data?.sensitivity ?? 0.4);
  const [autoLight, setAutoLightOptimistic] =
    useOptimistic(config.data?.autoLight ?? true);
  const [notifyOnMotion, setNotifyOptimistic] =
    useOptimistic(config.data?.notifyOnMotion ?? true);
  const [scareMode, setScareOptimistic] =
    useOptimistic(config.data?.scareMode ?? false);

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

  const armed = state.data?.armed ?? false;

  const onToggleEnabled = (next: boolean) => {
    setEnabledOptimistic(next);
    void (next ? armSentry(DEV_UNIT_ID, user.uid) : disarmSentry(DEV_UNIT_ID, user.uid));
  };

  const onSetSensitivity = (value: number) => {
    setSensitivityOptimistic(value);
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { sensitivity: value });
  };

  const onToggleAutoLight = (next: boolean) => {
    setAutoLightOptimistic(next);
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { autoLight: next });
  };

  const onToggleNotify = (next: boolean) => {
    setNotifyOptimistic(next);
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { notifyOnMotion: next });
  };

  const onToggleScare = (next: boolean) => {
    setScareOptimistic(next);
    void updateSentryConfig(DEV_UNIT_ID, user.uid, { scareMode: next });
  };

  const onGoLive = () => {
    void startCameraStream(DEV_UNIT_ID, user.uid);
  };

  const onStopStream = () => {
    void stopCameraStream(DEV_UNIT_ID, user.uid);
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

        {/* ── Scare mode ─────────────────────────────────────────────── */}
        <View style={[styles.row, scareMode ? styles.scareRowActive : null]}>
          <View style={{ flex: 1 }}>
            <Text style={styles.rowLabel}>Scare mode</Text>
            <Text style={styles.rowSubLabel}>
              {scareMode
                ? 'ARMED  ·  MOTION FIRES LIGHT + TWO-STROKE ENGINE'
                : 'OFF  ·  MOTION ONLY TRIGGERS THE LIGHT'}
            </Text>
            {scareMode ? (
              <Text style={styles.scareWarn}>
                LOUD. BYPASSES QUIET HOURS. 5-MIN COOLDOWN BETWEEN TRIGGERS.
              </Text>
            ) : null}
          </View>
          <Switch
            value={scareMode}
            onValueChange={onToggleScare}
            trackColor={{ false: colors.borderStrong, true: colors.danger }}
            thumbColor={scareMode ? colors.textDisplay : colors.textMuted}
            ios_backgroundColor={colors.borderStrong}
          />
        </View>

        {/* ── Live stream ────────────────────────────────────────────── */}
        <View style={styles.section}>
          <Eyebrow
            parts={['Live stream', camera.data?.streaming ? 'live' : 'idle']}
            live={!!camera.data?.streaming}
          />
          <LiveStreamCard
            streaming={!!camera.data?.streaming}
            hlsUrl={camera.data?.hlsUrl ?? null}
            error={camera.data?.error ?? null}
            onGoLive={onGoLive}
            onStop={onStopStream}
          />
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

// ─── live stream card ─────────────────────────────────────────────────────
//
// Stream lifecycle phases:
//   idle       — no stream requested. Show "GO LIVE" button.
//   connecting — Pi reports streaming, but Cloudflare's HLS manifest takes
//                ~5–15 s to become playable after RTMP ingest starts. We
//                hold off mounting the player and show a spinner instead,
//                so the iOS player doesn't render its persistent
//                "media unavailable" slashed-play icon.
//   playing    — grace elapsed; player is mounted. If expo-video signals
//                readyToPlay we transition here early.
//   failed     — player has been mounted for the failure window without
//                ever reaching readyToPlay. Show retry + stop.
//
// CONNECT_GRACE_MS: covers Cloudflare ingest warmup. ~8 s is comfortably
//   above the typical 5–6 s; bumping higher just delays valid streams.
// PLAYBACK_FAIL_MS: how long after mount we tolerate the player not
//   becoming ready before declaring failure. 15 s covers slow Starlink
//   uplinks; below that we'd false-alarm on a fresh stream.

const CONNECT_GRACE_MS = 8000;
const PLAYBACK_FAIL_MS = 15000;

type StreamPhase = 'idle' | 'connecting' | 'playing' | 'failed';

function LiveStreamCard({
  streaming,
  hlsUrl,
  error,
  onGoLive,
  onStop,
}: {
  streaming: boolean;
  hlsUrl: string | null;
  error: string | null;
  onGoLive: () => void;
  onStop: () => void;
}) {
  const [phase, setPhase] = useState<StreamPhase>('idle');
  const failTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const graceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // useVideoPlayer needs a stable source ref; pass empty string when we're
  // not yet trying to play so the player stays unmounted-equivalent.
  const shouldPlay = (phase === 'playing' || phase === 'failed') && !!hlsUrl;
  const livePlayer = useVideoPlayer(shouldPlay ? hlsUrl! : '', (p) => {
    p.loop = false;
    p.muted = false;
    if (shouldPlay) p.play();
  });

  // Drive the phase state machine from the Pi-reported `streaming` flag.
  // When streaming flips false at any point, we collapse back to idle.
  useEffect(() => {
    if (!streaming || !hlsUrl) {
      if (graceTimerRef.current) clearTimeout(graceTimerRef.current);
      if (failTimerRef.current) clearTimeout(failTimerRef.current);
      graceTimerRef.current = null;
      failTimerRef.current = null;
      setPhase('idle');
      return;
    }
    // streaming just became true (or component just mounted with it true).
    setPhase('connecting');
    graceTimerRef.current = setTimeout(() => {
      setPhase('playing');
      // Once we mount the player, start a failure deadline. If the player
      // signals readyToPlay before then, the listener below clears it.
      failTimerRef.current = setTimeout(() => {
        setPhase('failed');
      }, PLAYBACK_FAIL_MS);
    }, CONNECT_GRACE_MS);
    return () => {
      if (graceTimerRef.current) clearTimeout(graceTimerRef.current);
      if (failTimerRef.current) clearTimeout(failTimerRef.current);
    };
  }, [streaming, hlsUrl]);

  // Cancel the failure deadline as soon as the player is genuinely playable.
  useEffect(() => {
    if (!livePlayer) return;
    const sub = livePlayer.addListener('statusChange', (event) => {
      // expo-video's StatusChangeEventPayload has the new status under `status`.
      if (event?.status === 'readyToPlay' && failTimerRef.current) {
        clearTimeout(failTimerRef.current);
        failTimerRef.current = null;
      }
      if (event?.status === 'error') {
        setPhase('failed');
      }
    });
    return () => sub.remove();
  }, [livePlayer]);

  const handleRetry = () => {
    // Tear down the stream on the Pi, then re-issue start after a beat
    // so the ffmpeg process has time to release the camera.
    onStop();
    setTimeout(onGoLive, 1500);
  };

  if (phase === 'connecting') {
    return (
      <View style={styles.streamCard}>
        <View style={styles.streamConnectingBox}>
          <ActivityIndicator color={colors.textMuted} />
          <Text style={styles.streamPlaceholder}>CONNECTING…</Text>
          <Text style={styles.streamHint}>
            WAITING FOR CLOUDFLARE TO INGEST THE STREAM (5–10 S)
          </Text>
        </View>
        <Pressable onPress={onStop} style={styles.streamStopButton}>
          <Text style={styles.streamStopButtonLabel}>CANCEL</Text>
        </Pressable>
      </View>
    );
  }

  if (phase === 'playing') {
    return (
      <View style={styles.streamCard}>
        <VideoView
          player={livePlayer}
          style={styles.video}
          allowsFullscreen
          allowsPictureInPicture
          nativeControls
        />
        <Pressable onPress={onStop} style={styles.streamStopButton}>
          <Text style={styles.streamStopButtonLabel}>STOP STREAM</Text>
        </Pressable>
      </View>
    );
  }

  if (phase === 'failed') {
    return (
      <View style={styles.streamCard}>
        <Text style={styles.streamPlaceholder}>STREAM UNAVAILABLE</Text>
        <Text style={styles.streamHint}>
          PI REPORTS LIVE BUT THE PLAYBACK URL ISN’T RESPONDING. CHECK THAT
          THE CLOUDFLARE ENV IS SET ON THE PI AND THAT FFMPEG IS REACHING
          CLOUDFLARE.
        </Text>
        <View style={styles.streamFailedActions}>
          <Pressable onPress={handleRetry} style={styles.streamButton}>
            <Text style={styles.streamButtonLabel}>RETRY</Text>
          </Pressable>
          <Pressable onPress={onStop} style={styles.streamStopButton}>
            <Text style={styles.streamStopButtonLabel}>STOP</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  // phase === 'idle'
  return (
    <View style={styles.streamCard}>
      <Text style={styles.streamPlaceholder}>
        {error ? error : 'CAMERA IDLE'}
      </Text>
      <Text style={styles.streamHint}>
        TAP GO LIVE TO PUSH FROM THE PI USB WEBCAM TO CLOUDFLARE STREAM
      </Text>
      <Pressable onPress={onGoLive} style={styles.streamButton}>
        <Text style={styles.streamButtonLabel}>GO LIVE</Text>
      </Pressable>
    </View>
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
    backgroundColor: colors.textDisplay,
  },
  streamButtonLabel: {
    color: colors.background,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  streamStopButton: {
    marginTop: spacing.sm,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.xl,
    borderWidth: hairline,
    borderColor: colors.danger,
    alignSelf: 'center',
  },
  streamConnectingBox: {
    width: '100%',
    aspectRatio: 16 / 9,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    padding: spacing.lg,
  },
  streamFailedActions: {
    flexDirection: 'row',
    gap: spacing.sm,
    marginTop: spacing.sm,
  },
  streamStopButtonLabel: {
    color: colors.danger,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },

  scareRowActive: {
    paddingHorizontal: spacing.md,
    marginHorizontal: -spacing.md,
    borderLeftWidth: 2,
    borderLeftColor: colors.danger,
  },
  scareWarn: {
    marginTop: spacing.xs,
    color: colors.danger,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
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
