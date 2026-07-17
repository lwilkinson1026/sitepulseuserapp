import React from 'react';
import {
  ActivityIndicator,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Eyebrow, FigCaption, FuelPanel, Screen, SecondaryCTA } from '../../components';
import { useAuth } from '../../hooks/AuthContext';
import { useUnitEvents, type EventEntry } from '../../hooks/useUnitEvents';
import type { EventKind } from '../../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Reverse-chronological event log backed by units/{unitId}/events.
// The Pi and cloud functions append here; we render the last N with the
// same formatting the Sentry tab uses for motion rows.

const DEV_UNIT_ID = process.env.EXPO_PUBLIC_DEV_UNIT_ID ?? 'UNIT-001';

// Display labels for each EventKind. Kept here (rather than in types.ts)
// because mapping is purely a presentation concern.
const KIND_LABEL: Record<EventKind, string> = {
  motion:           'MOTION',
  'soc.critical':   'LOW SOC ALERT',
  'engine.start':   'ENGINE STARTED',
  'engine.stop':    'ENGINE STOPPED',
  'light.toggled':  'LIGHT TOGGLED',
  'sentry.armed':   'SENTRY ARMED',
  'sentry.disarmed':'SENTRY DISARMED',
  'theft.tripped':  'THEFT ALERT',
  'system.online':  'SYSTEM ONLINE',
  'system.offline': 'SYSTEM OFFLINE',
  'fuel.refuel':    'REFUELED',
  'fuel.low':       'LOW FUEL',
  'fuel.empty':     'OUT OF FUEL',
};

// Pull a one-line summary out of the event payload. Different event kinds
// stuff different shapes in here; we surface only the ones we know about
// and gracefully fall through to a generic JSON-ish line for unknown kinds.
function summarize(event: EventEntry): string {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  switch (event.kind) {
    case 'motion':
      return event.clipPath ? 'CLIP RECORDED' : 'NO CLIP';
    case 'soc.critical': {
      const soc = typeof p.soc === 'number' ? `${Math.round(p.soc)}%` : '—';
      return `SOC ${soc}`;
    }
    case 'engine.start':
    case 'engine.stop': {
      const reason = typeof p.reason === 'string' ? p.reason.toUpperCase() : null;
      return reason ?? (event.source === 'app' ? 'MANUAL' : 'AUTO');
    }
    case 'light.toggled': {
      const mode = typeof p.mode === 'string' ? p.mode.toUpperCase() : '—';
      return `MODE ${mode}`;
    }
    case 'sentry.armed':
    case 'sentry.disarmed':
      return event.source === 'app' ? 'BY USER' : 'BY SYSTEM';
    case 'theft.tripped':
      return 'GEOFENCE BREACHED';
    case 'fuel.refuel': {
      const method = p.method === 'add' ? 'ADDED' : 'FILLED';
      const gallons = typeof p.gallons === 'number' ? `${p.gallons} GAL` : null;
      return gallons ? `${method} · ${gallons}` : method;
    }
    case 'fuel.low':
    case 'fuel.empty': {
      const level = typeof p.level === 'number' ? `${p.level.toFixed(1)} GAL` : null;
      return level ?? '—';
    }
    case 'system.online':
    case 'system.offline':
      // Streamer reuses system.online for camera lifecycle — pull action if set.
      return typeof p.action === 'string' ? p.action.toUpperCase() : '—';
    default:
      return '';
  }
}

function formatTs(event: EventEntry): string {
  const d = event.at?.toDate ? event.at.toDate() : null;
  if (!d) return '—';
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  }
  // Yesterday or earlier — keep the calendar day visible.
  return d.toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).toUpperCase();
}

export function ActivityScreen() {
  const { signOutNow, user } = useAuth();
  const events = useUnitEvents(DEV_UNIT_ID, { pageSize: 50 });

  return (
    <Screen>
      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <View style={styles.header}>
          <Eyebrow
            parts={['05 / Activity', `${events.events.length} events`]}
          />
        </View>

        <View style={styles.fuel}>
          <FuelPanel unitId={DEV_UNIT_ID} />
        </View>

        <View style={styles.listHeader}>
          <Eyebrow parts={['Event log']} />
        </View>

        <View style={styles.list}>
          {events.loading ? (
            <View style={styles.loading}>
              <ActivityIndicator color={colors.textMuted} />
            </View>
          ) : events.error ? (
            <Text style={styles.empty}>
              COULD NOT LOAD EVENTS{'\n'}{events.error.message.toUpperCase()}
            </Text>
          ) : events.events.length === 0 ? (
            <Text style={styles.empty}>NO EVENTS YET</Text>
          ) : (
            events.events.map((evt) => (
              <View key={evt.id} style={styles.row}>
                <Text style={styles.ts}>{formatTs(evt)}</Text>
                <View style={styles.entry}>
                  <Text style={styles.kind}>
                    {KIND_LABEL[evt.kind] ?? evt.kind.toUpperCase()}
                  </Text>
                  <Text style={styles.body}>{summarize(evt)}</Text>
                </View>
              </View>
            ))
          )}
        </View>

        <View style={styles.account}>
          <Text style={styles.accountLabel}>SIGNED IN AS</Text>
          <Text style={styles.accountValue}>{user?.email ?? '—'}</Text>
          <View style={{ marginTop: spacing.md }}>
            <SecondaryCTA label="Sign out" onPress={signOutNow} />
          </View>
        </View>

        <View style={styles.footer}>
          <FigCaption number={5} label="Activity" detail={DEV_UNIT_ID} />
        </View>
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  scroll: {
    paddingTop: spacing.md,
    paddingBottom: spacing.xxl,
  },
  header: { paddingTop: spacing.md, paddingBottom: spacing.lg },
  fuel: {
    paddingBottom: spacing.lg,
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
  },
  listHeader: { paddingTop: spacing.lg, paddingBottom: spacing.sm },
  list: {
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  loading: {
    paddingVertical: spacing.xl,
    alignItems: 'center',
  },
  empty: {
    paddingVertical: spacing.xl,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    textAlign: 'center',
  },
  row: {
    flexDirection: 'row',
    gap: spacing.md,
    paddingVertical: spacing.sm,
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
  },
  ts: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    width: 96,
  },
  entry: { flex: 1 },
  kind: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  body: {
    marginTop: 2,
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  account: {
    marginTop: spacing.xl,
    paddingTop: spacing.lg,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  accountLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  accountValue: {
    marginTop: spacing.xxs,
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  footer: { paddingVertical: spacing.md },
});
