import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import {
  createUserWithEmailAndPassword,
  getIdTokenResult,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
  User,
} from 'firebase/auth';
import { ensureFirebase } from '../firebase/config';
import { usePushTokenRegistration } from './usePushTokenRegistration';

type AuthState = {
  initializing: boolean;          // true until the first onAuthStateChanged fires
  user: User | null;
  /**
   * SitePulse staff: the `admin: true` custom claim (set with
   * scripts/grant-admin.mjs). Admins see every unit, not just ones they own.
   * The rules enforce this independently — this flag only shapes the UI.
   */
  isAdmin: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<void>;
  signOutNow: () => Promise<void>;
};

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [initializing, setInitializing] = useState(true);
  const [user, setUser] = useState<User | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    const { auth } = ensureFirebase();
    let cancelled = false;
    // Auth can change again while the token fetch below is in flight (e.g. a
    // quick sign-out); only the newest callback may commit its result.
    let seq = 0;
    const unsub = onAuthStateChanged(auth, async (next) => {
      const mine = ++seq;
      // Resolve the claim before clearing `initializing`, so the unit list
      // is never queried with the wrong scope. Force-refresh: a claim granted
      // while the user was signed in only shows up in a fresh ID token.
      let admin = false;
      if (next) {
        try {
          admin = (await getIdTokenResult(next, true)).claims.admin === true;
        } catch {
          admin = false;
        }
      }
      if (cancelled || mine !== seq) return;
      setIsAdmin(admin);
      setUser(next);
      setInitializing(false);
    });
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  // Register the device's Expo Push token once the user is signed in.
  // Hook is a no-op until `user` is non-null; safe to call unconditionally.
  usePushTokenRegistration(user);

  const value = useMemo<AuthState>(
    () => ({
      initializing,
      user,
      isAdmin,
      async signIn(email, password) {
        const { auth } = ensureFirebase();
        await signInWithEmailAndPassword(auth, email, password);
      },
      async signUp(email, password) {
        const { auth } = ensureFirebase();
        await createUserWithEmailAndPassword(auth, email, password);
      },
      async signOutNow() {
        const { auth } = ensureFirebase();
        await signOut(auth);
      },
    }),
    [initializing, user, isAdmin],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
