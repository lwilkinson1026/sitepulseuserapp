import React, { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Eyebrow, FigCaption, Screen } from '../../components';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';

// Phase-0 stub. Real command flow lands in Phase 1: app writes an intent
// to units/{unitId}/commands; the Pi consumes and ACKs by writing status.

const OUTLETS = [
  { id: 1, name: 'Main Tools', draw: '420 W' },
  { id: 2, name: 'Site Lighting', draw: '88 W' },
  { id: 3, name: 'Battery Charger', draw: '—' },
  { id: 4, name: 'Welder / Plasma', draw: '732 W' },
  { id: 5, name: 'Site Office', draw: '—' },
  { id: 6, name: 'Spare / Misc', draw: '—' },
];

export function OutletsScreen() {
  const [state, setState] = useState<Record<number, boolean>>({
    1: true,
    2: true,
    3: false,
    4: true,
    5: false,
    6: false,
  });

  return (
    <Screen>
      <View style={styles.header}>
        <Eyebrow parts={['02 / Outlets', '6 channels']} />
      </View>

      <View style={styles.list}>
        {OUTLETS.map((o, idx) => {
          const on = state[o.id];
          return (
            <View
              key={o.id}
              style={[styles.row, idx === OUTLETS.length - 1 ? null : styles.rowDivider]}
            >
              <View style={styles.info}>
                <Text style={styles.name}>{o.name}</Text>
                <Text style={styles.meta}>CHANNEL {String(o.id).padStart(2, '0')}  ·  {on ? o.draw : 'OFF'}</Text>
              </View>
              <Pressable
                style={[styles.toggle, on ? styles.toggleOn : null]}
                onPress={() => setState((s) => ({ ...s, [o.id]: !s[o.id] }))}
              >
                <Text style={[styles.toggleLabel, on ? styles.toggleLabelOn : null]}>
                  {on ? 'ON' : 'OFF'}
                </Text>
              </Pressable>
            </View>
          );
        })}
      </View>

      <View style={styles.footer}>
        <FigCaption number={2} label="Outlets" detail="UNIT-001" />
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: {
    paddingTop: spacing.md,
    paddingBottom: spacing.lg,
  },
  list: {
    flex: 1,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingVertical: spacing.md,
  },
  rowDivider: {
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderHairline,
  },
  info: {
    flex: 1,
  },
  name: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyLG,
  },
  meta: {
    marginTop: 2,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  toggle: {
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    minWidth: 64,
    alignItems: 'center',
  },
  toggleOn: {
    backgroundColor: colors.textDisplay,
    borderColor: colors.textDisplay,
  },
  toggleLabel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  toggleLabelOn: {
    color: colors.background,
  },
  footer: {
    paddingVertical: spacing.md,
  },
});
