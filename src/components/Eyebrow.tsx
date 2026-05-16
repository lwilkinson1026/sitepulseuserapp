import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors, fonts, tracking, typeScale } from '../theme';

// Mono-caps eyebrow with dot separators. Appears above every section.
// Examples: "UNIT-001 · BOZEMAN PLANT · LIVE", "01 / DASHBOARD".
// Pass `parts` as an array and the component handles the · separators
// and uppercase casing for you. `live` shows a small pulsing green dot
// on the right (used by the dashboard "LIVE" eyebrow).

type EyebrowProps = {
  parts: string[];
  live?: boolean;
  staleness?: 'fresh' | 'stale' | 'offline';
};

export function Eyebrow({ parts, live = false, staleness = 'fresh' }: EyebrowProps) {
  const dotColor =
    staleness === 'offline'
      ? colors.danger
      : staleness === 'stale'
      ? colors.warning
      : colors.live;

  return (
    <View style={styles.row}>
      <Text style={styles.text} numberOfLines={1}>
        {parts.map((p) => p.toUpperCase()).join('  ·  ')}
      </Text>
      {live ? <View style={[styles.dot, { backgroundColor: dotColor }]} /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  text: {
    flexShrink: 1,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: 3,
  },
});
