import React, { useEffect, useRef } from 'react';
import { View, StyleSheet } from 'react-native';
import type { StyleProp, ViewStyle } from 'react-native';
import Hls from 'hls.js';

// Web HLS player. Safari and iOS Safari can decode HLS in a plain <video>;
// Chrome/Firefox/Edge cannot, so we attach hls.js to translate. expo-video's
// web shim wraps <video> too, but it just sets src=url and inherits the same
// codec limitation — which is why Chrome would show STREAM UNAVAILABLE even
// while Cloudflare was serving fine.

export type HlsVideoProps = {
  url: string;
  style?: StyleProp<ViewStyle>;
  onReady?: () => void;
  onError?: (error: unknown) => void;
};

export function HlsVideo({ url, style, onReady, onError }: HlsVideoProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const onReadyRef = useRef(onReady);
  const onErrorRef = useRef(onError);
  useEffect(() => { onReadyRef.current = onReady; }, [onReady]);
  useEffect(() => { onErrorRef.current = onError; }, [onError]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !url) return;

    const tryPlay = () => {
      video.play().catch(() => {
        // Autoplay can be blocked by browser policy; the user can hit the
        // built-in play button. Not an HLS failure.
      });
    };

    // Path 1: Safari / iOS — <video> can decode HLS natively.
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
      const onLoaded = () => onReadyRef.current?.();
      const onErr = () => onErrorRef.current?.(video.error);
      video.addEventListener('loadedmetadata', onLoaded);
      video.addEventListener('error', onErr);
      tryPlay();
      return () => {
        video.removeEventListener('loadedmetadata', onLoaded);
        video.removeEventListener('error', onErr);
        video.removeAttribute('src');
        video.load();
      };
    }

    // Path 2: Chrome / Firefox / Edge — fall back to hls.js.
    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: true,
        liveSyncDurationCount: 3,
      });
      hlsRef.current = hls;
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        onReadyRef.current?.();
        tryPlay();
      });
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data?.fatal) onErrorRef.current?.(data);
      });
      return () => {
        hls.destroy();
        hlsRef.current = null;
      };
    }

    onErrorRef.current?.(new Error('HLS not supported in this browser'));
    return undefined;
  }, [url]);

  return (
    <View style={[styles.container, style]}>
      <video
        ref={videoRef}
        controls
        playsInline
        autoPlay
        // muted + preload required for unprompted autoplay on iOS Safari and
        // most mobile browsers. The Pi stream uses anullsrc, so muting costs
        // nothing — the user can unmute via the native controls if needed.
        muted
        preload="metadata"
        style={{ width: '100%', height: '100%', backgroundColor: '#000' }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    backgroundColor: '#000',
  },
});
