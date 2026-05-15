import React, { useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { NativeStackScreenProps } from '@react-navigation/native-stack';
import { Eyebrow, FigCaption, PrimaryCTA, Screen } from '../../components';
import { useAuth } from '../../hooks/AuthContext';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../../theme';
import { AuthStackParamList } from '../../navigation/types';

type Props = NativeStackScreenProps<AuthStackParamList, 'SignUp'>;

export function SignUpScreen({ navigation }: Props) {
  const { signUp } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit() {
    setError(null);
    if (!email || password.length < 8) {
      setError('PASSWORD MUST BE 8+ CHARACTERS');
      return;
    }
    setBusy(true);
    try {
      await signUp(email.trim(), password);
    } catch (e: any) {
      setError(String(e?.message ?? 'SIGN UP FAILED').toUpperCase());
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen edges={['top', 'bottom', 'left', 'right']}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        style={styles.flex}
      >
        <ScrollView
          contentContainerStyle={styles.scroll}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <View style={styles.header}>
            <Eyebrow parts={['01 / Create account']} />
          </View>

          <Text style={styles.headline}>Claim{'\n'}the unit.</Text>

          <View style={styles.fields}>
            <Field
              label="EMAIL"
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              keyboardType="email-address"
              textContentType="emailAddress"
            />
            <Field
              label="PASSWORD"
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              textContentType="newPassword"
            />
          </View>

          {error ? <Text style={styles.error}>{error}</Text> : null}

          <View style={styles.cta}>
            <PrimaryCTA label={busy ? 'Creating…' : 'Create account'} onPress={onSubmit} disabled={busy} />
          </View>
        </ScrollView>

        <View style={styles.footer}>
          <Pressable onPress={() => navigation.navigate('SignIn')} hitSlop={12}>
            <Text style={styles.altText}>HAVE AN ACCOUNT  ·  SIGN IN  →</Text>
          </Pressable>
          <FigCaption number={1} label="Create account" detail="v0.1 scaffold" />
        </View>
      </KeyboardAvoidingView>
    </Screen>
  );
}

function Field(props: React.ComponentProps<typeof TextInput> & { label: string }) {
  const { label, style, ...rest } = props;
  return (
    <View style={styles.fieldWrap}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <TextInput
        autoCorrect={false}
        spellCheck={false}
        {...rest}
        placeholderTextColor={colors.textMuted}
        style={[styles.field, style]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  scroll: {
    paddingBottom: spacing.lg,
  },
  header: {
    paddingTop: spacing.md,
    paddingBottom: spacing.lg,
  },
  headline: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.displayLG,
    lineHeight: typeScale.displayLG * 0.98,
    letterSpacing: tracking.displayTight,
    marginBottom: spacing.xl,
  },
  fields: { gap: spacing.md },
  fieldWrap: {
    borderBottomWidth: hairline,
    borderBottomColor: colors.borderStrong,
    paddingBottom: spacing.xs,
  },
  fieldLabel: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
    marginBottom: spacing.xxs,
  },
  field: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyRegular,
    fontSize: typeScale.bodyLG,
    paddingVertical: spacing.xs,
  },
  error: {
    marginTop: spacing.md,
    color: colors.danger,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  cta: { marginTop: spacing.xl },
  altText: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  footer: {
    paddingTop: spacing.md,
    paddingBottom: spacing.md,
    gap: spacing.md,
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
  },
});
