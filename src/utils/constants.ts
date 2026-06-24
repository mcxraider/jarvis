// src/utils/constants.ts — Shared constants used across the application.

// All audio MIME types that the bot accepts for transcription processing.
// This list is checked by FileService.isAudioFile() and WhisperService's MIME validation.
export const AudioMimeTypes = [
  'audio/flac',
  'audio/m4a',
  'audio/ogg',
  'audio/mpeg',
  'audio/mp3',
  'audio/mpga',
  'audio/wav',
  'audio/x-wav',
  'audio/mp4',
  'audio/aac',
  'audio/webm',
] as const;
