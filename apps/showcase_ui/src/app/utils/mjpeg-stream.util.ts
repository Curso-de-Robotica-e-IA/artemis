/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export interface MjpegFrame {
  bytes: Uint8Array;
  contentType: string;
}

export interface MjpegStreamCallbacks {
  onFrame: (frame: MjpegFrame) => void;
  onError?: (error: unknown) => void;
  onComplete?: () => void;
}

const MAX_BUFFER_BYTES = 16 * 1024 * 1024;
const HEADER_TERMINATOR = new Uint8Array([13, 10, 13, 10]); // \r\n\r\n
const encoder = new TextEncoder();
const decoder = new TextDecoder('latin1');

export function extractMultipartBoundary(contentType: string | null | undefined): string {
  if (!contentType) return 'frame';
  const match = contentType.match(/boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))/i);
  const raw = (match?.[1] || match?.[2] || 'frame').trim();
  return raw.replace(/^--+/, '') || 'frame';
}

function indexOfBytes(
  haystack: Uint8Array,
  needle: Uint8Array,
  fromIndex: number = 0
): number {
  const limit = haystack.length - needle.length;
  if (needle.length === 0 || limit < fromIndex) return -1;
  const first = needle[0];
  for (let i = fromIndex; i <= limit; i++) {
    if (haystack[i] !== first) continue;
    let matched = true;
    for (let j = 1; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) {
        matched = false;
        break;
      }
    }
    if (matched) return i;
  }
  return -1;
}

function parsePartHeaders(rawHeaders: string): { contentType: string; contentLength: number | null } {
  let contentType = 'image/png';
  let contentLength: number | null = null;
  for (const line of rawHeaders.split('\r\n')) {
    const colonIdx = line.indexOf(':');
    if (colonIdx <= 0) continue;
    const key = line.slice(0, colonIdx).trim().toLowerCase();
    const val = line.slice(colonIdx + 1).trim();
    if (key === 'content-type' && val) {
      contentType = val;
    } else if (key === 'content-length') {
      const parsed = Number.parseInt(val, 10);
      if (Number.isFinite(parsed) && parsed >= 0) {
        contentLength = parsed;
      }
    }
  }
  return { contentType, contentLength };
}

/**
 * Incremental parser for ``multipart/x-mixed-replace`` image frame streams.
 */
export class MultipartFrameParser {
  private buffer: Uint8Array<ArrayBufferLike> = new Uint8Array(0);
  private readonly boundaryMarker: Uint8Array;

  constructor(boundary: string = 'frame') {
    const clean = boundary.trim().replace(/^--+/, '') || 'frame';
    this.boundaryMarker = encoder.encode(`--${clean}`);
  }

  public push(chunk: Uint8Array): MjpegFrame[] {
    if (!chunk || chunk.length === 0) return [];
    if (this.buffer.length === 0) {
      this.buffer = chunk.slice();
    } else {
      const merged = new Uint8Array(this.buffer.length + chunk.length);
      merged.set(this.buffer, 0);
      merged.set(chunk, this.buffer.length);
      this.buffer = merged;
    }

    if (this.buffer.length > MAX_BUFFER_BYTES) {
      this.buffer = new Uint8Array(0);
      return [];
    }

    const frames: MjpegFrame[] = [];
    while (true) {
      const boundaryIdx = indexOfBytes(this.buffer, this.boundaryMarker, 0);
      if (boundaryIdx === -1) {
        // Keep only enough tail bytes to avoid splitting a boundary across chunks.
        const keepTail = Math.max(0, this.buffer.length - this.boundaryMarker.length);
        if (keepTail > 0) {
          this.buffer = this.buffer.slice(keepTail);
        }
        break;
      }

      const afterBoundary = boundaryIdx + this.boundaryMarker.length;
      if (
        afterBoundary + 2 <= this.buffer.length
        && this.buffer[afterBoundary] === 45
        && this.buffer[afterBoundary + 1] === 45
      ) {
        // Final multipart closing boundary ("--<boundary>--").
        this.buffer = this.buffer.slice(afterBoundary + 2);
        break;
      }

      const headerEnd = indexOfBytes(this.buffer, HEADER_TERMINATOR, afterBoundary);
      if (headerEnd === -1) {
        if (boundaryIdx > 0) {
          this.buffer = this.buffer.slice(boundaryIdx);
        }
        break;
      }

      const rawHeaders = decoder.decode(this.buffer.subarray(afterBoundary, headerEnd));
      const { contentType, contentLength } = parsePartHeaders(rawHeaders);
      const bodyStart = headerEnd + HEADER_TERMINATOR.length;

      if (contentLength !== null) {
        if (this.buffer.length < bodyStart + contentLength) {
          if (boundaryIdx > 0) {
            this.buffer = this.buffer.slice(boundaryIdx);
          }
          break;
        }
        const bytes = this.buffer.slice(bodyStart, bodyStart + contentLength);
        if (bytes.length > 0) {
          frames.push({ bytes, contentType });
        }
        let nextOffset = bodyStart + contentLength;
        if (
          nextOffset + 1 < this.buffer.length
          && this.buffer[nextOffset] === 13
          && this.buffer[nextOffset + 1] === 10
        ) {
          nextOffset += 2;
        }
        this.buffer = this.buffer.slice(nextOffset);
        continue;
      }

      const nextBoundary = indexOfBytes(this.buffer, this.boundaryMarker, bodyStart);
      if (nextBoundary === -1) {
        if (boundaryIdx > 0) {
          this.buffer = this.buffer.slice(boundaryIdx);
        }
        break;
      }

      let bodyEnd = nextBoundary;
      if (bodyEnd >= bodyStart + 2 && this.buffer[bodyEnd - 2] === 13 && this.buffer[bodyEnd - 1] === 10) {
        bodyEnd -= 2;
      }
      const bytes = this.buffer.slice(bodyStart, bodyEnd);
      if (bytes.length > 0) {
        frames.push({ bytes, contentType });
      }
      this.buffer = this.buffer.slice(nextBoundary);
    }

    return frames;
  }
}

/**
 * Consume an MJPEG HTTP stream via Fetch + AbortSignal so closing/switching
 * cancels the underlying TCP socket immediately rather than lingering in Blink's
 * HTMLImageElement loader cache.
 */
export async function consumeMjpegStream(
  url: string,
  signal: AbortSignal,
  callbacks: MjpegStreamCallbacks
): Promise<void> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  try {
    const response = await fetch(url, { signal, cache: 'no-store' });
    if (!response.ok || !response.body) {
      throw new Error(`Live stream failed with HTTP ${response.status}`);
    }
    const boundary = extractMultipartBoundary(response.headers.get('content-type'));
    const parser = new MultipartFrameParser(boundary);
    reader = response.body.getReader();

    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value && value.length > 0) {
        const frames = parser.push(value);
        for (const frame of frames) {
          if (signal.aborted) break;
          callbacks.onFrame(frame);
        }
      }
    }
    if (!signal.aborted) {
      callbacks.onComplete?.();
    }
  } catch (err: any) {
    if (signal.aborted || err?.name === 'AbortError') {
      return;
    }
    callbacks.onError?.(err);
  } finally {
    if (reader) {
      try {
        await reader.cancel();
      } catch {
        // Reader may already be closed or cancelled.
      }
    }
  }
}
