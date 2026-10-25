"use client";

import { useRef, useState } from "react";
import { Play, ChevronDown } from "lucide-react";
import { ApiError, previewVoice } from "@/lib/api";
import { ElevenLabsVoice } from "@/lib/api";

export function VoicePickerWithPreview({
  voices,
  currentVoiceId,
  placeholder,
  providerId,
  onPick,
}: {
  voices: ElevenLabsVoice[];
  currentVoiceId?: string | null;
  placeholder: string;
  providerId: string;
  onPick: (voiceId: string, label: string) => void;
}) {
  const [playing, setPlaying] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);

  const currentVoice = voices.find((v) => v.voice_id === currentVoiceId);

  const playPreview = async (e: React.MouseEvent, voice: ElevenLabsVoice) => {
    e.stopPropagation();
    if (playing === voice.voice_id) {
      audioRef.current?.pause();
      setPlaying(null);
      return;
    }

    setPlaying(voice.voice_id);
    setPreviewError(null);
    try {
      const text = voice.name;
      const blob = await previewVoice(providerId, text);
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = URL.createObjectURL(blob);
      audioRef.current?.pause();
      const audio = new Audio(urlRef.current);
      audioRef.current = audio;
      audio.onended = () => setPlaying(null);
      audio.onerror = () => {
        setPreviewError("Could not play audio");
        setPlaying(null);
      };
      await audio.play();
    } catch (e) {
      setPreviewError(e instanceof ApiError ? e.detail : "Error playing preview");
      setPlaying(null);
    }
  };

  return (
    <div className="voice-picker-container">
      <div className="voice-picker-field">
        <button
          type="button"
          className="voice-picker-trigger"
          onClick={() => setIsOpen(!isOpen)}
        >
          <span className="voice-picker-text">
            {currentVoice?.name || placeholder}
          </span>
          <ChevronDown size={16} />
        </button>
        {currentVoice && (
          <button
            type="button"
            className="btn btn-ghost btn-sm voice-preview-btn"
            onClick={(e) => playPreview(e, currentVoice)}
            title={`Preview ${currentVoice.name}`}
          >
            <Play size={14} />
            {playing === currentVoice.voice_id ? "Playing…" : "Play"}
          </button>
        )}
      </div>

      {isOpen && (
        <div className="voice-options-list">
          {voices.map((v) => (
            <div
              key={v.voice_id}
              className={`voice-option ${currentVoiceId === v.voice_id ? "selected" : ""}`}
              onClick={() => {
                onPick(v.voice_id, v.name);
                setIsOpen(false);
              }}
              role="option"
              aria-selected={currentVoiceId === v.voice_id}
            >
              <span className="voice-name">{v.name}</span>
              <button
                type="button"
                className="btn btn-ghost btn-sm voice-preview-btn"
                onClick={(e) => playPreview(e, v)}
                title={`Preview ${v.name}`}
              >
                <Play size={14} />
                {playing === v.voice_id ? "Playing…" : "Play"}
              </button>
            </div>
          ))}
        </div>
      )}

      {previewError && (
        <div style={{ fontSize: "0.875rem", color: "var(--red)", marginTop: 8 }}>
          {previewError}
        </div>
      )}
    </div>
  );
}
