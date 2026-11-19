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

import {
  consumeMjpegStream,
  extractMultipartBoundary,
  MjpegFrame,
  MultipartFrameParser,
} from './mjpeg-stream.util';

const encoder = new TextEncoder();

function buildMultipartPart(payload: Uint8Array, contentType = 'image/png', withLength = true): Uint8Array {
  const header = withLength
    ? `--frame\r\nContent-Type: ${contentType}\r\nContent-Length: ${payload.length}\r\n\r\n`
    : `--frame\r\nContent-Type: ${contentType}\r\n\r\n`;
  const hBytes = encoder.encode(header);
  const trail = encoder.encode('\r\n');
  const out = new Uint8Array(hBytes.length + payload.length + trail.length);
  out.set(hBytes, 0);
  out.set(payload, hBytes.length);
  out.set(trail, hBytes.length + payload.length);
  return out;
}

describe('mjpeg-stream.util', () => {
  it('extracts quoted and unquoted boundaries with frame fallback', () => {
    expect(extractMultipartBoundary('multipart/x-mixed-replace; boundary=frame')).toBe('frame');
    expect(extractMultipartBoundary('multipart/x-mixed-replace; boundary="myboundary"')).toBe('myboundary');
    expect(extractMultipartBoundary(null)).toBe('frame');
  });

  it('parses frames split across arbitrary chunk boundaries', () => {
    const parser = new MultipartFrameParser('frame');
    const p1 = new Uint8Array([1, 2, 3, 4]);
    const p2 = new Uint8Array([5, 6, 7]);
    const part1 = buildMultipartPart(p1, 'image/png', true);
    const part2 = buildMultipartPart(p2, 'image/jpeg', true);
    const combined = new Uint8Array(part1.length + part2.length);
    combined.set(part1, 0);
    combined.set(part2, part1.length);

    const parsed: MjpegFrame[] = [];
    for (let i = 0; i < combined.length; i += 7) {
      parsed.push(...parser.push(combined.slice(i, i + 7)));
    }

    expect(parsed.length).toBe(2);
    expect(parsed[0].contentType).toBe('image/png');
    expect(Array.from(parsed[0].bytes)).toEqual([1, 2, 3, 4]);
    expect(parsed[1].contentType).toBe('image/jpeg');
    expect(Array.from(parsed[1].bytes)).toEqual([5, 6, 7]);
  });

  it('parses boundary-delimited frames even without Content-Length', () => {
    const parser = new MultipartFrameParser('frame');
    const p1 = new Uint8Array([10, 20, 30]);
    const p2 = new Uint8Array([40, 50]);
    const part1 = buildMultipartPart(p1, 'image/png', false);
    const part2 = buildMultipartPart(p2, 'image/png', false);
    const closing = encoder.encode('--frame--\r\n');
    const full = new Uint8Array(part1.length + part2.length + closing.length);
    full.set(part1, 0);
    full.set(part2, part1.length);
    full.set(closing, part1.length + part2.length);

    const parsed = parser.push(full);
    expect(parsed.length).toBe(2);
    expect(Array.from(parsed[0].bytes)).toEqual([10, 20, 30]);
    expect(Array.from(parsed[1].bytes)).toEqual([40, 50]);
  });

  it('aborts fetch and cancels reader cleanly without firing onError on abort', async () => {
    const controller = new AbortController();
    const cancelSpy = jasmine.createSpy('cancel').and.resolveTo();
    const frames: MjpegFrame[] = [];
    const errors: unknown[] = [];

    const firstPart = buildMultipartPart(new Uint8Array([9, 8, 7]), 'image/png', true);
    let reads = 0;
    const fakeStream = {
      getReader: () => ({
        read: () => {
          reads++;
          if (reads === 1) {
            return Promise.resolve({ done: false, value: firstPart });
          }
          return new Promise<ReadableStreamReadResult<Uint8Array>>((resolve) => {
            controller.signal.addEventListener('abort', () => resolve({ done: true, value: undefined }), { once: true });
          });
        },
        cancel: cancelSpy,
      }),
    };

    const fetchSpy = spyOn(globalThis, 'fetch').and.resolveTo({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'multipart/x-mixed-replace; boundary=frame' }),
      body: fakeStream as unknown as ReadableStream<Uint8Array>,
    } as Response);

    const donePromise = consumeMjpegStream('/api/stream/device-live?serial=usb-001', controller.signal, {
      onFrame: (f) => {
        frames.push(f);
        controller.abort();
      },
      onError: (e) => errors.push(e),
    });

    await donePromise;
    expect(fetchSpy).toHaveBeenCalled();
    expect(frames.length).toBe(1);
    expect(errors.length).toBe(0);
    expect(cancelSpy).toHaveBeenCalled();
  });
});
