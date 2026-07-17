// Fuel usage dashboard header.
//
// Turns the raw engine/refuel event stream (via useFuelModel) into a usage
// panel: a runway hero ("refuel in ~X days"), a tank gauge, a trailing
// daily-burn sparkline, and the two owner-authored refuel actions. All the
// math lives in lib/fuelModel; this file is presentation + the amount-entry
// modal that feeds refuel().

import React, { useState } from 'react';
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Eyebrow } from './Eyebrow';
import { useFuelModel } from '../hooks/useFuelModel';
import type { FuelRefuelMethod } from '../firebase/types';
import { colors, fonts, hairline, spacing, tracking, typeScale } from '../theme';

interface FuelPanelProps {
  unitId: string | null;
}

// Which button opened the amount sheet. `full` treats the amount as optional
// ("how much did it take?"), `add` requires it.
type RefuelDraft = { method: FuelRefuelMethod } | null;

function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

// Human runway. Sub-day runway reads in hours so "0.3 days" doesn't hide an
// imminent empty behind a rounded-down day count.
function runwayLabel(days: number | null): { value: string; unit: string } {
  if (days === null || !Number.isFinite(days)) return { value: '—', unit: 'DAYS' };
  if (days < 1) return { value: String(Math.max(0, Math.round(days * 24))), unit: 'HOURS' };
  if (days < 10) return { value: days.toFixed(1), unit: 'DAYS' };
  return { value: String(Math.round(days)), unit: 'DAYS' };
}

export function FuelPanel({ unitId }: FuelPanelProps) {
  const { loading, model, settings, error, refuel } = useFuelModel(unitId);
  const [draft, setDraft] = useState<RefuelDraft>(null);
  const [amountText, setAmountText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const openSheet = (method: FuelRefuelMethod) => {
    setAmountText('');
    setDraft({ method });
  };

  const closeSheet = () => {
    if (submitting) return;
    setDraft(null);
    setAmountText('');
  };

  const submit = async () => {
    if (!draft || submitting) return;
    const parsed = amountText.trim() === '' ? null : Number(amountText);
    const gallons =
      parsed !== null && Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    // "Add" is meaningless without an amount; block submit until one's entered.
    if (draft.method === 'add' && gallons === null) return;
    setSubmitting(true);
    try {
      await refuel(draft.method, gallons);
      setDraft(null);
      setAmountText('');
    } catch (e) {
      console.warn('[fuel] refuel failed', e);
    } finally {
      setSubmitting(false);
    }
  };

  const alertColor =
    model.alert === 'empty'
      ? colors.danger
      : model.alert === 'low'
        ? colors.warning
        : colors.textDisplay;

  const runway = runwayLabel(model.runwayDays);
  const pct = model.pctTank; // 0–1 or null
  const fillPct = pct === null ? 0 : Math.max(0, Math.min(1, pct));

  const sparkMax = Math.max(...model.sparkline, 1e-6);

  const addDisabled =
    draft?.method === 'add' &&
    (amountText.trim() === '' || !(Number(amountText) > 0));

  return (
    <View style={styles.container}>
      <Eyebrow parts={['Fuel', model.calibrated ? 'Calibrated' : 'Estimate']} />

      {model.alert !== 'ok' && (
        <View style={[styles.alert, { borderColor: alertColor }]}>
          <View style={[styles.alertDot, { backgroundColor: alertColor }]} />
          <Text style={[styles.alertText, { color: alertColor }]}>
            {model.alert === 'empty'
              ? model.ranDryDetected
                ? 'TANK RAN DRY · REFUEL REQUIRED'
                : 'OUT OF FUEL · REFUEL REQUIRED'
              : 'LOW FUEL · REFUEL SOON'}
          </Text>
        </View>
      )}

      {/* Runway hero */}
      <View style={styles.hero}>
        <Text style={styles.heroEyebrow}>REFUEL IN</Text>
        <View style={styles.heroRow}>
          <Text style={[styles.heroNumeric, { color: alertColor }]}>
            {runway.value}
          </Text>
          <Text style={styles.heroUnit}>{runway.unit}</Text>
        </View>
        <Text style={styles.heroSub}>
          {model.level === null
            ? 'LOG A REFUEL TO START TRACKING'
            : model.projectedEmptyMs !== null
              ? `EMPTY ~${new Date(model.projectedEmptyMs)
                  .toLocaleDateString([], { month: 'short', day: 'numeric' })
                  .toUpperCase()}`
              : model.engineRunning
                ? 'ENGINE RUNNING'
                : 'NO RECENT BURN'}
        </Text>
      </View>

      {/* Tank gauge */}
      <View style={styles.gaugeGroup}>
        <View style={styles.gaugeTrack}>
          <View
            style={[
              styles.gaugeFill,
              { width: `${fillPct * 100}%`, backgroundColor: alertColor },
            ]}
          />
        </View>
        <View style={styles.gaugeLabels}>
          <Text style={styles.gaugeLevel}>
            {fmt(model.level)} / {fmt(model.tankGallons)} GAL
          </Text>
          <Text style={styles.gaugePct}>
            {pct === null ? '—' : `${Math.round(fillPct * 100)}%`}
          </Text>
        </View>
      </View>

      {/* Daily-burn sparkline (trailing 14 days) */}
      <View style={styles.sparkGroup}>
        <Text style={styles.metricCaption}>BURN · LAST {model.sparkline.length} DAYS</Text>
        <View style={styles.sparkRow}>
          {model.sparkline.map((v, i) => (
            <View
              key={i}
              style={[
                styles.sparkBar,
                { height: Math.max(2, (v / sparkMax) * 40) },
              ]}
            />
          ))}
        </View>
      </View>

      {/* Metrics */}
      <View style={styles.metricsRow}>
        <View style={styles.metricCell}>
          <Text style={styles.metricValue}>{fmt(model.dailyBurnGallons, 2)}</Text>
          <Text style={styles.metricCaption}>GAL / DAY</Text>
        </View>
        <View style={styles.metricCell}>
          <Text style={styles.metricValue}>{fmt(model.burnRateGalPerHour, 2)}</Text>
          <Text style={styles.metricCaption}>GAL / HR</Text>
        </View>
        <View style={styles.metricCell}>
          <Text style={styles.metricValue}>{fmt(model.runtimeTodayHours, 1)}</Text>
          <Text style={styles.metricCaption}>HRS TODAY</Text>
        </View>
      </View>

      {model.costSinceRefuel !== null && (
        <Text style={styles.costLine}>
          ${fmt(model.costSinceRefuel, 2)} BURNED SINCE LAST REFUEL
        </Text>
      )}

      {error && (
        <Text style={styles.errorLine}>
          FUEL DATA ERROR · {error.message.toUpperCase()}
        </Text>
      )}

      {/* Refuel actions */}
      <View style={styles.actions}>
        <RefuelButton
          label="Filled to full"
          onPress={() => openSheet('full')}
          disabled={loading || !unitId}
        />
        <RefuelButton
          label="Add fuel"
          onPress={() => openSheet('add')}
          disabled={loading || !unitId}
        />
      </View>

      {/* Amount entry sheet */}
      <Modal
        visible={draft !== null}
        transparent
        animationType="fade"
        onRequestClose={closeSheet}
      >
        <KeyboardAvoidingView
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          style={styles.modalBackdrop}
        >
          <Pressable style={styles.backdropPress} onPress={closeSheet} />
          <View style={styles.sheet}>
            <Text style={styles.sheetTitle}>
              {draft?.method === 'full' ? 'FILLED TO FULL' : 'ADD FUEL'}
            </Text>
            <Text style={styles.sheetHint}>
              {draft?.method === 'full'
                ? 'Optional — how many gallons did it take? Helps calibrate the burn rate.'
                : 'How many gallons did you add?'}
            </Text>
            <View style={styles.inputRow}>
              <TextInput
                style={styles.input}
                value={amountText}
                onChangeText={setAmountText}
                keyboardType="decimal-pad"
                placeholder={draft?.method === 'full' ? 'e.g. 4.2 (optional)' : 'e.g. 2.0'}
                placeholderTextColor={colors.textMuted}
                autoFocus
                editable={!submitting}
              />
              <Text style={styles.inputUnit}>GAL</Text>
            </View>
            <View style={styles.sheetActions}>
              <RefuelButton label="Cancel" variant="ghost" onPress={closeSheet} disabled={submitting} />
              <RefuelButton
                label={submitting ? 'Saving…' : 'Confirm'}
                variant="solid"
                onPress={submit}
                disabled={submitting || addDisabled}
              />
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>
    </View>
  );
}

// Compact button variant local to the fuel panel — the shared SecondaryCTA
// has a trailing arrow + full-width layout that doesn't suit the paired
// refuel row or the modal's Cancel/Confirm pair.
function RefuelButton({
  label,
  onPress,
  disabled,
  variant = 'outline',
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  variant?: 'outline' | 'solid' | 'ghost';
}) {
  return (
    <Pressable
      onPress={disabled ? undefined : onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      style={({ pressed }) => [
        styles.btn,
        variant === 'solid' ? styles.btnSolid : null,
        variant === 'ghost' ? styles.btnGhost : null,
        pressed && !disabled ? styles.btnPressed : null,
        disabled ? styles.btnDisabled : null,
      ]}
    >
      <Text
        style={[
          styles.btnLabel,
          variant === 'solid' ? styles.btnLabelSolid : null,
        ]}
      >
        {label.toUpperCase()}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  container: {
    gap: spacing.lg,
    paddingBottom: spacing.lg,
  },
  alert: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    borderWidth: hairline,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.sm,
  },
  alertDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  alertText: {
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  hero: {
    alignItems: 'center',
    gap: spacing.xxs,
  },
  heroEyebrow: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  heroRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: spacing.xs,
  },
  heroNumeric: {
    fontFamily: fonts.display,
    fontSize: 88,
    lineHeight: 92,
    letterSpacing: -3,
  },
  heroUnit: {
    color: colors.textMuted,
    fontFamily: fonts.display,
    fontSize: typeScale.titleMD,
    marginBottom: spacing.md,
  },
  heroSub: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  gaugeGroup: {
    gap: spacing.xs,
  },
  gaugeTrack: {
    height: 10,
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    backgroundColor: colors.background,
  },
  gaugeFill: {
    position: 'absolute',
    left: 0,
    top: 0,
    bottom: 0,
  },
  gaugeLabels: {
    flexDirection: 'row',
    justifyContent: 'space-between',
  },
  gaugeLevel: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  gaugePct: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  sparkGroup: {
    gap: spacing.xs,
  },
  sparkRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: 3,
    height: 40,
  },
  sparkBar: {
    flex: 1,
    backgroundColor: colors.borderStrong,
  },
  metricsRow: {
    flexDirection: 'row',
    borderTopWidth: hairline,
    borderTopColor: colors.borderHairline,
    paddingTop: spacing.md,
  },
  metricCell: {
    flex: 1,
    gap: spacing.xxs,
  },
  metricValue: {
    color: colors.textDisplay,
    fontFamily: fonts.display,
    fontSize: typeScale.titleMD,
    letterSpacing: tracking.displayNormal,
  },
  metricCaption: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  costLine: {
    color: colors.textBody,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  errorLine: {
    color: colors.danger,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoSM,
    letterSpacing: tracking.monoCaps,
  },
  actions: {
    flexDirection: 'row',
    gap: spacing.sm,
  },
  btn: {
    flex: 1,
    borderWidth: hairline,
    borderColor: colors.textDisplay,
    paddingVertical: spacing.md,
    alignItems: 'center',
    justifyContent: 'center',
  },
  btnSolid: {
    backgroundColor: colors.ctaFill,
    borderColor: colors.ctaFill,
  },
  btnGhost: {
    borderColor: colors.borderStrong,
  },
  btnPressed: {
    opacity: 0.7,
  },
  btnDisabled: {
    opacity: 0.35,
  },
  btnLabel: {
    color: colors.textDisplay,
    fontFamily: fonts.bodyMedium,
    fontSize: typeScale.bodyMD,
    letterSpacing: tracking.buttonCaps,
  },
  btnLabelSolid: {
    color: colors.ctaText,
  },
  modalBackdrop: {
    flex: 1,
    justifyContent: 'flex-end',
  },
  backdropPress: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: 'rgba(0,0,0,0.7)',
  },
  sheet: {
    backgroundColor: colors.surfaceElevated,
    borderTopWidth: hairline,
    borderTopColor: colors.borderStrong,
    padding: spacing.lg,
    gap: spacing.md,
  },
  sheetTitle: {
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  sheetHint: {
    color: colors.textBody,
    fontFamily: fonts.bodyRegular,
    fontSize: typeScale.bodyMD,
  },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: hairline,
    borderColor: colors.borderStrong,
    paddingHorizontal: spacing.md,
  },
  input: {
    flex: 1,
    color: colors.textDisplay,
    fontFamily: fonts.mono,
    fontSize: typeScale.titleMD,
    paddingVertical: spacing.md,
  },
  inputUnit: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: typeScale.monoLG,
    letterSpacing: tracking.monoCaps,
  },
  sheetActions: {
    flexDirection: 'row',
    gap: spacing.sm,
  },
});
