// Dashboard fuel warning strip.
//
// Renders nothing while fuel is fine; surfaces a single hairline-bordered
// banner when useFuelModel reports 'low' or 'empty' so the owner sees a
// refuel warning on the main screen without opening the Activity tab.

import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { useFuelModel } from '../hooks/useFuelModel';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../theme';

interface FuelAlertBannerProps {
  unitId: string | null;
}

export function FuelAlertBanner({ unitId }: FuelAlertBannerProps) {
  const { model } = useFuelModel(unitId);
  if (model.alert === 'ok') return null;

  const color = model.alert === 'empty' ? colors.danger : colors.warning;
  const headline =
    model.alert === 'empty'
      ? model.ranDryDetected
        ? 'TANK RAN DRY'
        : 'OUT OF FUEL'
      : 'LOW FUEL';

  const detail =
    model.level !== null
      ? `${model.level.toFixed(1)} GAL LEFT` +
        (model.runwayDays !== null && model.runwayDays < 10
          ? `  ·  ~${model.runwayDays < 1 ? `${Math.round(model.runwayDays * 24)} HR` : `${model.runwayDays.toFixed(1)} D`}`
          : '')
      : 'REFUEL TO RESUME TRACKING';

  return (
    <View style={[styles.banner, { borderColor: color }]}>
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.headline, { color }]}>{headline}</Text>
      <Text style={styles.detail}>{detail}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    borderWidth: hairline,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.sm,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  headline: {
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  detail: {
    flex: 1,
    textAlign: 'right',
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
