import React, { useEffect, useRef } from 'react';
import type { StyleProp, ViewStyle } from 'react-native';
import { useVideoPlayer, VideoView } from 'expo-video';

// Native HLS player. iOS AVPlayer and Android ExoPlayer both speak HLS
// natively, so on native this just thinly wraps expo-video. The web build
// gets its own implementation from HlsVideo.web.tsx that plugs hls.js in
// when the browser <video> element can't decode HLS by itself.

export type HlsVideoProps = {
  url: string;
  style?: StyleProp<ViewStyle>;
  onReady?: () => void;
  onError?: (error: unknown) => void;
};

export function HlsVideo({ url, style, onReady, onError }: HlsVideoProps) {
  // Refs so the listener doesn't re-attach every render when the parent
  // passes inline callbacks.
  const onReadyRef = useRef(onReady);
  const onErrorRef = useRef(onError);
  useEffect(() => { onReadyRef.current = onReady; }, [onReady]);
  useEffect(() => { onErrorRef.current = onError; }, [onError]);

  const player = useVideoPlayer(url, (p) => {
    p.loop = false;
    p.muted = false;
    p.play();
  });

  useEffect(() => {
    if (!player) return;
    try { player.play(); } catch { /* play() throws aren't actionable */ }
  }, [player, url]);

  useEffect(() => {
    if (!player) return;
    const sub = player.addListener('statusChange', (event: any) => {
      if (event?.status === 'readyToPlay') onReadyRef.current?.();
      else if (event?.status === 'error') onErrorRef.current?.(event?.error);
    });
    return () => sub.remove();
  }, [player]);

  return (
    <VideoView
      player={player}
      style={style}
      allowsFullscreen
      allowsPictureInPicture
      nativeControls
    />
  );
}
