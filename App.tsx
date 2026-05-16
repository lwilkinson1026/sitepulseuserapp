import React from 'react';
import { ActivityIndicator, Platform, StyleSheet, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';

// On web, expo-font's runtime registration isn't reliably picking up
// the bundled Inter / JetBrains Mono assets, so we side-load Google Fonts
// and alias them to the Expo key names. Native builds are unaffected.
if (Platform.OS === 'web' && typeof document !== 'undefined') {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href =
    'https://fonts.googleapis.com/css2?' +
    'family=Inter:wght@400;500;600;900&' +
    'family=JetBrains+Mono:wght@400;500&display=swap';
  document.head.appendChild(link);

  const style = document.createElement('style');
  style.textContent = `
    @font-face { font-family: 'Inter_400Regular';  src: local('Inter'); font-weight: 400; }
    @font-face { font-family: 'Inter_500Medium';   src: local('Inter'); font-weight: 500; }
    @font-face { font-family: 'Inter_600SemiBold'; src: local('Inter'); font-weight: 600; }
    @font-face { font-family: 'Inter_900Black';    src: local('Inter'); font-weight: 900; }
    @font-face { font-family: 'JetBrainsMono_400Regular'; src: local('JetBrains Mono'); font-weight: 400; }
    @font-face { font-family: 'JetBrainsMono_500Medium';  src: local('JetBrains Mono'); font-weight: 500; }
  `;
  document.head.appendChild(style);
}
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
