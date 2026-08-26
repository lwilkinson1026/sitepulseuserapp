import React, { useEffect, useState } from 'react';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import {
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
  Inter_900Black,
  useFonts as useInter,
} from '@expo-google-fonts/inter';
import {
  JetBrainsMono_400Regular,
  JetBrainsMono_500Medium,
  useFonts as useJetBrains,
} from '@expo-google-fonts/jetbrains-mono';
import { AuthProvider } from './src/hooks/AuthContext';
import { ActiveUnitProvider } from './src/hooks/ActiveUnitContext';
import { RootNavigator } from './src/navigation/RootNavigator';
import { ensureFirebase } from './src/firebase/config';
import { colors } from './src/theme';

ensureFirebase();

// Type is cosmetic. Past this point we render with whatever the platform gives
// us rather than leave the operator looking at a spinner.
const FONT_GRACE_MS = 3000;

export default function App() {
  const [interLoaded, interError] = useInter({
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_900Black,
  });
  const [monoLoaded, monoError] = useJetBrains({
    JetBrainsMono_400Regular,
    JetBrainsMono_500Medium,
  });

  const [graceElapsed, setGraceElapsed] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setGraceElapsed(true), FONT_GRACE_MS);
    return () => clearTimeout(timer);
  }, []);

  // `useFonts` reports failure through its second slot. Treat an error as
  // settled — a missing typeface must never gate sign-in.
  const fontsSettled =
    (interLoaded || interError) && (monoLoaded || monoError);

  if (!fontsSettled && !graceElapsed) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator color={colors.textMuted} />
      </View>
    );
  }

  return (
    <SafeAreaProvider>
      <StatusBar style="light" />
      <AuthProvider>
        {/* Inside AuthProvider: unit resolution keys off the signed-in uid. */}
        <ActiveUnitProvider>
          <RootNavigator />
        </ActiveUnitProvider>
      </AuthProvider>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  loading: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.background,
  },
});
