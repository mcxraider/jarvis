#!/usr/bin/env ts-node
// scripts/eval_chunking_params.ts — Grid search over chunking parameters to find optimal WER.

import 'dotenv/config';

import * as fs from 'fs';
import * as http from 'http';
import * as path from 'path';
import { WhisperService } from '../src/services/ai/whisper.service';
import { planAudioChunks } from '../src/utils/ai/audio-chunk-plan';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const AUDIO_DIR = '/Users/Jerry_YANG_from.TP/Downloads/audio_files';

const SAMPLES = [
  { audio: 'sample_1.ogg', truth: 'sample_1.txt', mime: 'audio/ogg' },
  { audio: 'sample_2.ogg', truth: 'sample_2.txt', mime: 'audio/ogg' },
];

const CORE_VALUES = [15, 20, 30, 45, 60];
const OVERLAP_VALUES = [0, 3, 5, 8];
// Groq free tier: 20 RPM. Pause between combos to avoid 429s.
const COOLDOWN_MS = 65_000;

// ---------------------------------------------------------------------------
// WER (Word Error Rate) — standard ASR metric
// ---------------------------------------------------------------------------

function normalizeText(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^\w\s']/g, '')
    .split(/\s+/)
    .filter(Boolean);
}

function computeWER(reference: string, hypothesis: string): number {
  const ref = normalizeText(reference);
  const hyp = normalizeText(hypothesis);
  if (ref.length === 0) return hyp.length === 0 ? 0 : 1;

  const prev = new Array(hyp.length + 1);
  const curr = new Array(hyp.length + 1);
  for (let j = 0; j <= hyp.length; j++) prev[j] = j;
  for (let i = 1; i <= ref.length; i++) {
    curr[0] = i;
    for (let j = 1; j <= hyp.length; j++) {
      if (ref[i - 1] === hyp[j - 1]) curr[j] = prev[j - 1];
      else curr[j] = 1 + Math.min(prev[j - 1], prev[j], curr[j - 1]);
    }
    for (let j = 0; j <= hyp.length; j++) prev[j] = curr[j];
  }
  return prev[hyp.length] / ref.length;
}

// ---------------------------------------------------------------------------
// HTTP server — serves multiple files
// ---------------------------------------------------------------------------

function serveFiles(
  files: { route: string; filePath: string; mime: string }[],
): Promise<{ baseUrl: string; close: () => Promise<void> }> {
  const routeMap = new Map(files.map((f) => [f.route, f]));
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const entry = routeMap.get(req.url ?? '');
      if (!entry) {
        res.writeHead(404);
        res.end();
        return;
      }
      const stat = fs.statSync(entry.filePath);
      res.writeHead(200, { 'Content-Type': entry.mime, 'Content-Length': stat.size });
      fs.createReadStream(entry.filePath).pipe(res);
    });
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      if (!addr || typeof addr === 'string') return reject(new Error('Failed to bind'));
      resolve({
        baseUrl: `http://127.0.0.1:${addr.port}`,
        close: () => new Promise<void>((r) => server.close(() => r())),
      });
    });
    server.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

interface RunResult {
  core: number;
  overlap: number;
  samples: {
    name: string;
    chunks: number;
    wer: number;
    timeMs: number;
  }[];
  avgWer: number;
  avgTimeSec: number;
}

async function main(): Promise<void> {
  if (!process.env.GROQ_API_KEY) {
    console.error('GROQ_API_KEY is not set.');
    process.exit(1);
  }

  // Load ground truths
  const truths = SAMPLES.map((s) => ({
    ...s,
    truthText: fs.readFileSync(path.join(AUDIO_DIR, s.truth), 'utf-8').trim(),
    audioPath: path.join(AUDIO_DIR, s.audio),
  }));

  for (const t of truths) {
    if (!fs.existsSync(t.audioPath)) {
      console.error(`Missing: ${t.audioPath}`);
      process.exit(1);
    }
  }

  // Build param grid
  const grid: { core: number; overlap: number }[] = [];
  for (const core of CORE_VALUES) {
    for (const overlap of OVERLAP_VALUES) {
      grid.push({ core, overlap });
    }
  }

  console.log(`\nChunking parameter grid search`);
  console.log(`  Samples: ${SAMPLES.map((s) => s.audio).join(', ')}`);
  console.log(`  Combinations: ${grid.length}`);
  console.log(`  Metric: Word Error Rate (lower = better)\n`);

  // Start HTTP server
  const { baseUrl, close } = await serveFiles(
    truths.map((t, i) => ({ route: `/${i}`, filePath: t.audioPath, mime: t.mime })),
  );

  const results: RunResult[] = [];

  try {
    // Initial cooldown in case a previous run exhausted the RPM window
    process.stdout.write('Waiting 65s for rate limit window to reset...');
    await new Promise((r) => setTimeout(r, COOLDOWN_MS));
    process.stdout.write(' ready\n\n');

    for (let gi = 0; gi < grid.length; gi++) {
      const { core, overlap } = grid[gi];
      const tag = `[${gi + 1}/${grid.length}] core=${String(core).padStart(2)} overlap=${overlap}`;
      const sampleResults: RunResult['samples'] = [];

      for (let si = 0; si < truths.length; si++) {
        const t = truths[si];
        const service = new WhisperService({
          coreSeconds: core,
          overlapSeconds: overlap,
          maxConcurrentRequests: 1,
          enforceEnglishOnly: true,
          language: 'en',
        });

        const url = `${baseUrl}/${si}`;
        const result = await service.transcribeAudio(url);
        const wer = computeWER(t.truthText, result.text);
        const chunks = planAudioChunks(result.durationSeconds, { coreSeconds: core, overlapSeconds: overlap }).length;

        sampleResults.push({
          name: t.audio,
          chunks,
          wer,
          timeMs: result.processingTimeMs,
        });
      }

      const avgWer = sampleResults.reduce((s, r) => s + r.wer, 0) / sampleResults.length;
      const avgTimeSec = sampleResults.reduce((s, r) => s + r.timeMs, 0) / sampleResults.length / 1000;

      results.push({ core, overlap, samples: sampleResults, avgWer, avgTimeSec });

      const parts = sampleResults.map(
        (r) => `${r.name}: WER=${r.wer.toFixed(3)} (${(r.timeMs / 1000).toFixed(1)}s, ${r.chunks}ch)`,
      );
      console.log(`${tag} | ${parts.join(' | ')}`);

      // Rate limit cooldown between combos
      if (gi < grid.length - 1) {
        const totalChunks = sampleResults.reduce((s, r) => s + r.chunks, 0);
        const waitSec = Math.ceil(COOLDOWN_MS / 1000);
        process.stdout.write(`  ↳ ${totalChunks} API calls used, cooling down ${waitSec}s...`);
        await new Promise((r) => setTimeout(r, COOLDOWN_MS));
        process.stdout.write(' done\n');
      }
    }
  } finally {
    await close();
  }

  // --- Sorted results ---
  const sorted = [...results].sort((a, b) => a.avgWer - b.avgWer);

  const header = `Rank  Core  Overlap  ${truths.map((t) => `Ch₁  WER₁   `).join('')} Avg WER  Avg Time`;
  const divider = '─'.repeat(header.length + 10);

  console.log(`\n${divider}`);
  console.log('Results sorted by average WER (best first):\n');

  // Dynamic header
  const colHeaders = truths.map((_, i) => `Ch${i + 1}   WER${i + 1}   `).join('');
  console.log(`Rank  Core  Overlap  ${colHeaders}Avg WER  Avg Time`);
  console.log(divider);

  for (let i = 0; i < sorted.length; i++) {
    const r = sorted[i];
    const rank = String(i + 1).padStart(3);
    const core = String(r.core).padStart(4);
    const overlap = String(r.overlap).padStart(5);
    const sampleCols = r.samples
      .map((s) => `${String(s.chunks).padStart(3)}   ${s.wer.toFixed(3).padStart(5)}   `)
      .join('');
    const avgWer = r.avgWer.toFixed(3).padStart(7);
    const avgTime = `${r.avgTimeSec.toFixed(1)}s`.padStart(7);
    const marker = r.core === 30 && r.overlap === 5 ? '  ← current default' : '';
    console.log(`${rank}  ${core}  ${overlap}   ${sampleCols}${avgWer}  ${avgTime}${marker}`);
  }

  console.log(divider);
  console.log(`\nBest: core=${sorted[0].core}s, overlap=${sorted[0].overlap}s (avg WER=${sorted[0].avgWer.toFixed(3)})`);
  console.log(`Current default (core=30, overlap=5): avg WER=${results.find((r) => r.core === 30 && r.overlap === 5)?.avgWer.toFixed(3)}\n`);

  process.exit(0);
}

main().catch((err) => {
  console.error('\nFailed:', err instanceof Error ? err.message : err);
  if (err instanceof Error && err.stack) console.error(err.stack);
  process.exit(1);
});
