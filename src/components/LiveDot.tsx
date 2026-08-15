import React, { useEffect, useRef } from 'react';
import { Animated, Easing, StyleSheet, View, ViewStyle } from 'react-native';
import { colors, motion } from '../theme';

// 6-px green dot on a 1.6 s breath cycle (opacity 0.4 → 1.0 → 0.4).
// Switches to amber when stale and red when offline. The thresholds live in
// useUnitTelemetry.ts, derived from the Pi's publish cadence — this component
// only renders whichever state it is handed.
// Stops pulsing when feed is stale or when prefers-reduced-motion is set.

type LiveDotProps = {
  staleness?: 'fresh' | 'stale' | 'offline';
  size?: number;
  style?: ViewStyle;
  reducedMotion?: boolean;
};

export function LiveDot({
  staleness = 'fresh',
  size = 6,
  style,
  reducedMotion = false,
}: LiveDotProps) {
  const opacity = useRef(new Animated.Value(1)).current;
  const isFresh = staleness === 'fresh';

  useEffect(() => {
    if (!isFresh || reducedMotion) {
      opacity.setValue(1);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, {
          toValue: 0.4,
          duration: motion.livePulse / 2,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
        Animated.timing(opacity, {
          toValue: 1,
          duration: motion.livePulse / 2,
          easing: Easing.inOut(Easing.ease),
          useNativeDriver: true,
        }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [isFresh, reducedMotion, opacity]);

  const color =
    staleness === 'offline'
      ? colors.danger
      : staleness === 'stale'
      ? colors.warning
      : colors.live;

  return (
    <View style={style}>
      <Animated.View
        style={[
          styles.dot,
          { width: size, height: size, borderRadius: size / 2, backgroundColor: color, opacity },
        ]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  dot: {},
});
