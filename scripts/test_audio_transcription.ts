#!/usr/bin/env ts-node
// scripts/test_audio_transcription.ts — Exercise the real WhisperService pipeline against a local audio file.

import 'dotenv/config';

import * as fs from 'fs';
import * as http from 'http';
import * as os from 'os';
import * as path from 'path';
import { WhisperService } from '../src/services/ai/whisper.service';
import { planAudioChunks } from '../src/utils/ai/audio-chunk-plan';
import { AUDIO_LIMITS } from '../src/utils/ai/audio-limits';

// ---------------------------------------------------------------------------
// Extension / MIME
// ---------------------------------------------------------------------------

const EXT_TO_MIME: Record<string, string> = {
  '.flac': 'audio/flac',
  '.m4a': 'audio/m4a',
  '.ogg': 'audio/ogg',
  '.oga': 'audio/ogg',
  '.mp3': 'audio/mpeg',
  '.mpga': 'audio/mpeg',
  '.wav': 'audio/wav',
  '.mp4': 'audio/mp4',
  '.aac': 'audio/aac',
  '.webm': 'audio/webm',
};

const ACCEPTED_EXTS = Object.keys(EXT_TO_MIME).join(', ');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolvePath(raw: string): string {
  if (raw.startsWith('~')) raw = path.join(os.homedir(), raw.slice(1));
  return path.resolve(raw);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function pad(n: number, width = 8): string {
  return n.toFixed(3).padStart(width);
}

// ---------------------------------------------------------------------------
// Temp HTTP server — serves one file on localhost, random port
// ---------------------------------------------------------------------------

function serveFile(filePath: string, mime: string): Promise<{ url: string; close: () => Promise<void> }> {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      if (req.url !== '/audio') {
        res.writeHead(404);
        res.end();
        return;
      }
      const stat = fs.statSync(filePath);
      res.writeHead(200, {
        'Content-Type': mime,
        'Content-Length': stat.size,
      });
      fs.createReadStream(filePath).pipe(res);
    });

    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      if (!addr || typeof addr === 'string') {
        reject(new Error('Failed to bind server'));
        return;
      }
      const url = `http://127.0.0.1:${addr.port}/audio`;
      const close = () =>
        new Promise<void>((res) => {
          server.close(() => res());
        });
      resolve({ url, close });
    });

    server.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  // --- CLI arg ---
  const rawPath = process.argv[2];
  if (!rawPath) {
    console.error('Usage: npm run test:audio -- <path-to-audio-file>');
    console.error(`Accepted extensions: ${ACCEPTED_EXTS}`);
    process.exit(1);
  }

  // --- Env ---
  if (!process.env.GROQ_API_KEY) {
    console.error('GROQ_API_KEY is not set. Add it to .env or export it.');
    process.exit(1);
  }

  // --- File validation ---
  const filePath = resolvePath(rawPath);

  if (!fs.existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    process.exit(1);
  }

  const stat = fs.statSync(filePath);
  if (!stat.isFile()) {
    console.error(`Not a regular file: ${filePath}`);
    process.exit(1);
  }

  const ext = path.extname(filePath).toLowerCase();
  const mime = EXT_TO_MIME[ext];
  if (!mime) {
    console.error(`Unsupported extension "${ext}". Accepted: ${ACCEPTED_EXTS}`);
    process.exit(1);
  }

  // --- Print input ---
  console.log('\nAudio transcription test\n');
  console.log('Input:');
  console.log(`  File: ${filePath}`);
  console.log(`  Size: ${formatBytes(stat.size)}`);
  console.log(`  Type: ${mime}`);

  // --- Serve file & transcribe ---
  const { url, close } = await serveFile(filePath, mime);

  try {
    const service = new WhisperService({
      enforceEnglishOnly: true,
      language: 'en',
      qualityMonitoringEnabled: true,
    });

    console.log(`\n  Serving at ${url}`);
    console.log('  Transcribing…\n');

    const result = await service.transcribeAudio(url);

    // --- Chunk plan (same resolution as WhisperService) ---
    const coreSeconds =
      (process.env.GROQ_AUDIO_CORE_SECONDS && parseFloat(process.env.GROQ_AUDIO_CORE_SECONDS)) ||
      AUDIO_LIMITS.CORE_SECONDS;
    const overlapSeconds = AUDIO_LIMITS.OVERLAP_SECONDS;
    const plan = planAudioChunks(result.durationSeconds, { coreSeconds, overlapSeconds });

    // --- Print results ---
    console.log('Result:');
    console.log(`  Duration: ${result.durationSeconds.toFixed(2)} s`);
    console.log(`  Chunks: ${result.chunkCount}`);
    console.log(`  Processing time: ${(result.processingTimeMs / 1000).toFixed(2)} s`);
    if (result.detectedLanguage) console.log(`  Language: ${result.detectedLanguage}`);
    if (result.quality) {
      const q = result.quality as { flaggedSegments: number; totalSegments: number };
      console.log(`  Quality: ${q.flaggedSegments}/${q.totalSegments} segments flagged`);
    }

    if (plan.length > 1) {
      console.log(`\nChunk plan (core=${coreSeconds}s, overlap=${overlapSeconds}s):`);
      for (const c of plan) {
        console.log(
          `  #${c.index}  core ${pad(c.coreStartSeconds)} → ${pad(c.coreEndSeconds)}   upload ${pad(c.startSeconds)} → ${pad(c.endSeconds)}`,
        );
      }
    }

    console.log('\nTranscript:');
    console.log('─'.repeat(50));
    console.log(result.text);
    console.log('─'.repeat(50));
  } finally {
    await close();
  }

  process.exit(0);
}

main().catch((err) => {
  console.error('\nFailed:', err instanceof Error ? err.message : err);
  if (err instanceof Error && err.stack) {
    console.error(err.stack);
  }
  process.exit(1);
});
