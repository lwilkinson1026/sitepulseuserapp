// Row of unit chips for accounts that can open more than one unit — in
// practice SitePulse fleet admins. Renders nothing for a customer with a
// single unit, so their dashboard is unchanged.
//
// The pick lives in ActiveUnitContext, so every tab follows it.

import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text } from 'react-native';
import { useActiveUnit } from '../hooks/ActiveUnitContext';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../theme';

export function UnitSwitcher() {
  const { unitIds, unitId, setUnitId, isOwner } = useActiveUnit();
  if (unitIds.length < 2) return null;

  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.row}
      style={styles.scroller}
    >
      {unitIds.map((id) => {
        const active = id === unitId;
        return (
          <Pressable
            key={id}
            onPress={() => setUnitId(id)}
            style={({ pressed }) => [
              styles.chip,
              active ? styles.chipActive : null,
              pressed && !active ? { opacity: 0.7 } : null,
            ]}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            accessibilityLabel={`Show ${id}`}
          >
            <Text style={[styles.label, active ? styles.labelActive : null]}>{id}</Text>
          </Pressable>
        );
      })}
      {!isOwner ? <Text style={styles.note}>ADMIN VIEW</Text> : null}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  scroller: {
    flexGrow: 0,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
  },
  chip: {
    borderWidth: hairline,
    borderColor: colors.borderHairline,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  chipActive: {
    borderColor: colors.textDisplay,
  },
  label: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  labelActive: {
    color: colors.textDisplay,
  },
  note: {
    marginLeft: spacing.xs,
    color: colors.warning,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
