import { useState, useRef, useEffect, useCallback } from 'react';

type NavMode = 'keyboard' | 'mouse';

export function useNavMode(): {
  navMode: NavMode;
  isKeyboard: boolean;
  activateKeyboard: () => void;
} {
  const [navMode, setNavMode] = useState<NavMode>('mouse');
  const lastPosRef = useRef({ x: -1, y: -1 });

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (lastPosRef.current.x !== e.clientX || lastPosRef.current.y !== e.clientY) {
        lastPosRef.current = { x: e.clientX, y: e.clientY };
        setNavMode('mouse');
      }
    };
    document.addEventListener('mousemove', onMove);
    return () => document.removeEventListener('mousemove', onMove);
  }, []);

  const activateKeyboard = useCallback(() => setNavMode('keyboard'), []);

  return { navMode, isKeyboard: navMode === 'keyboard', activateKeyboard };
}
