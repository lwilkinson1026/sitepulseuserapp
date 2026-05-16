import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import {
  createUserWithEmailAndPassword,
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
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<void>;
  signOutNow: () => Promise<void>;
};

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [initializing, setInitializing] = useState(true);
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    const { auth } = ensureFirebase();
    const unsub = onAuthStateChanged(auth, (next) => {
      setUser(next);
      setInitializing(false);
    });
    return unsub;
  }, []);

  // Register the device's Expo Push token once the user is signed in.
  // Hook is a no-op until `user` is non-null; safe to call unconditionally.
  usePushTokenRegistration(user);

  const value = useMemo<AuthState>(
    () => ({
      initializing,
      user,
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
    [initializing, user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
