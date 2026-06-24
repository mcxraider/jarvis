// src/utils/ai/fileValidation.ts — Simple file validation helpers shared across
// AI services (Whisper transcription, audio conversion). Keep these pure functions
// with no external dependencies so they're easy to test.

// Throws if the file exceeds the allowed size. Used before sending files to the
// Whisper API (which has a 25MB hard limit) and after audio format conversion.
export function validateFileSize(fileSizeBytes: number, maxFileSizeBytes: number): void {
  if (fileSizeBytes > maxFileSizeBytes) {
    const maxSizeMB = Math.round(maxFileSizeBytes / (1024 * 1024));
    const actualSizeMB = Math.round(fileSizeBytes / (1024 * 1024));

    throw new Error(`File size (${actualSizeMB}MB) exceeds maximum allowed size (${maxSizeMB}MB)`);
  }
}

// Converts raw byte counts to human-readable strings (e.g. "1.2 MB", "340 KB").
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 Bytes';

  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Checks if a file's extension is in the allowed list (extensions without dots).
export function validateFileExtension(fileName: string, allowedExtensions: string[]): boolean {
  const extension = fileName.split('.').pop()?.toLowerCase();
  return extension ? allowedExtensions.includes(extension) : false;
}
