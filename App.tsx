import React from 'react';
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
import { RootNavigator } from './src/navigation/RootNavigator';
import { ensureFirebase } from './src/firebase/config';
import { colors } from './src/theme';

ensureFirebase();

export default function App() {
  const [interLoaded] = useInter({
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_900Black,
  });
  const [monoLoaded] = useJetBrains({
    JetBrainsMono_400Regular,
    JetBrainsMono_500Medium,
  });

  if (!interLoaded || !monoLoaded) {
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
        <RootNavigator />
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
