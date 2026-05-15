import React, { useEffect } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { NativeStackScreenProps } from '@react-navigation/native-stack';
import { LiveDot, Screen } from '../../components';
import { colors, fonts, spacing, tracking, typeScale } from '../../theme';
import { AuthStackParamList } from '../../navigation/types';

// Black field. SITEPULSE wordmark. Single status dot. Then route to sign-in.
// Spec §9 — "decides between sign-in flow and home", restores selected unit.
// At this scaffold stage we route forward after a short hold.

type Props = NativeStackScreenProps<AuthStackParamList, 'Splash'>;

export function SplashScreen({ navigation }: Props) {
  useEffect(() => {
    const t = setTimeout(() => navigation.replace('SignIn'), 900);
    return () => clearTimeout(t);
  }, [navigation]);

  return (
    <Screen edges={['top', 'bottom', 'left', 'right']}>
      <View style={styles.center}>
        <View style={styles.markRow}>
          <LiveDot size={8} />
          <Text style={styles.wordmark}>SITEPULSE</Text>
        </View>
        <Text style={styles.tag}>HYBRID  ·  JOB-SITE  ·  POWER</Text>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  markRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  wordmark: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.titleLG,
    letterSpacing: tracking.displayNormal,
  },
  tag: {
    marginTop: spacing.md,
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
});
