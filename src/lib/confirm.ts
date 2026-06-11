// Cross-platform confirmation prompt.
//
// React Native's `Alert.alert()` only works on iOS/Android — on web it's
// a silent no-op, so any callback wired through its button list never
// fires. That's a real footgun on the dashboard's admin engine controls,
// where the visible button does nothing on the Vercel-hosted web build.
//
// This helper returns a Promise<boolean> resolved with the user's choice:
//   - native: shows Alert.alert with Cancel/Confirm; resolves true on
//     confirm, false on cancel.
//   - web:    shows window.confirm() with title + message concatenated;
//     resolves true on OK, false on Cancel.
//
// Call sites become:
//   if (await confirm({ title, message, confirmLabel, destructive })) {
//     await doTheThing();
//   }

import { Alert, Platform } from 'react-native';

export interface ConfirmOptions {
  title: string;
  message: string;
  /** Label for the confirm button (native only — web uses "OK"). */
  confirmLabel?: string;
  /** Marks the confirm button red on iOS (native only). */
  destructive?: boolean;
}

export function confirm(opts: ConfirmOptions): Promise<boolean> {
  const { title, message, confirmLabel = 'OK', destructive = false } = opts;

  if (Platform.OS === 'web') {
    // window.confirm exists in any browser RN-Web targets.
    // Empty message would render a blank line; only join when both present.
    const text = message ? `${title}\n\n${message}` : title;
    // Synchronous call — wrap in a Promise to keep the API uniform.
    const ok = typeof window !== 'undefined' && window.confirm(text);
    return Promise.resolve(!!ok);
  }

  // iOS / Android.
  return new Promise<boolean>((resolve) => {
    Alert.alert(
      title,
      message,
      [
        { text: 'Cancel', style: 'cancel', onPress: () => resolve(false) },
        {
          text: confirmLabel,
          style: destructive ? 'destructive' : 'default',
          onPress: () => resolve(true),
        },
      ],
      // onDismiss only fires when the user dismisses the modal some other
      // way (back button on Android, swipe on iOS 14+). Treat that as a
      // cancel so the promise never hangs.
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}
