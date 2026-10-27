type Voice = { voice_id: string; name: string };

/** Body for POST /providers/{id}/preview: `voiceId` makes the server speak in that voice, not the saved one. */
export function previewBody(text: string, voiceId?: string): { text: string; voice_id?: string } {
  return voiceId ? { text, voice_id: voiceId } : { text };
}

/** The voice the picker shows as chosen. A saved voice that is no longer in the list (deleted from the account,
 * or outside the language filter) still shows, under its saved label, rather than reading as unset. */
export function chosenVoice<V extends Voice>(
  voices: V[], currentVoiceId: string | null | undefined, savedLabel?: string | null,
): Voice | undefined {
  if (!currentVoiceId) return undefined;
  return voices.find((v) => v.voice_id === currentVoiceId) ?? { voice_id: currentVoiceId, name: savedLabel || currentVoiceId };
}
