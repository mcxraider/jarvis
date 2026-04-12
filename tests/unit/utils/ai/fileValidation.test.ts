import {
  formatFileSize,
  validateFileExtension,
  validateFileSize,
} from '../../../../src/utils/ai/fileValidation';

describe('fileValidation utilities', () => {
  it('rejects oversized files', () => {
    expect(() => validateFileSize(5 * 1024 * 1024, 10 * 1024 * 1024)).not.toThrow();
    expect(() => validateFileSize(11 * 1024 * 1024, 10 * 1024 * 1024)).toThrow(
      'File size (11MB) exceeds maximum allowed size (10MB)',
    );
  });

  it('formats byte counts into readable units', () => {
    expect(formatFileSize(0)).toBe('0 Bytes');
    expect(formatFileSize(1536)).toBe('1.5 KB');
  });

  it('validates extensions against an allow-list', () => {
    expect(validateFileExtension('memo.MP3', ['mp3', 'wav'])).toBe(true);
    expect(validateFileExtension('memo.txt', ['mp3', 'wav'])).toBe(false);
    expect(validateFileExtension('memo', ['mp3', 'wav'])).toBe(false);
  });
});
