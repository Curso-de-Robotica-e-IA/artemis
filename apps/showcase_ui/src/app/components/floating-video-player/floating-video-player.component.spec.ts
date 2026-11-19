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

import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { FloatingVideoPlayerComponent } from './floating-video-player.component';
import { AgentService } from '../../services/agent.service';

const textEncoder = new TextEncoder();

function buildFrameChunk(payload: Uint8Array, contentType = 'image/png'): Uint8Array {
  const header = textEncoder.encode(
    `--frame\r\nContent-Type: ${contentType}\r\nContent-Length: ${payload.length}\r\n\r\n`
  );
  const trail = textEncoder.encode('\r\n');
  const combined = new Uint8Array(header.length + payload.length + trail.length);
  combined.set(header, 0);
  combined.set(payload, header.length);
  combined.set(trail, header.length + payload.length);
  return combined;
}

describe('FloatingVideoPlayerComponent live stream target routing', () => {
  it('binds liveStreamUrl, streams via AbortController-managed fetch, and aborts cleanly on switch/end/destroy', async () => {
    const recordingPlaybackStatus = signal<'idle' | 'live' | 'processing' | 'ready' | 'failed' | 'unavailable'>('live');
    const isVideoWindowOpen = signal(true);
    const isVideoMinimized = signal(false);
    const playerMode = signal<'video' | 'steps'>('video');
    const currentSessionId = signal<string | null>('wifi-sess');
    const activeVideoSessionIdSignal = signal<string | null>('wifi-sess');
    const activeVideoDeviceSerial = signal<string | null>('192.168.1.55:5555');

    const agentServiceStub = {
      isVideoWindowOpen,
      isVideoMinimized,
      activeVideoUrl: signal<string | null>(null),
      activeVideoSegments: signal<any[]>([]),
      activeVideoTitle: signal('WiFi Task'),
      isVideoLoading: signal(false),
      recordingPlaybackStatus,
      recordingPlaybackMessage: signal(''),
      shouldAutoplayVideo: signal(false),
      videoSeekRequest: signal(null),
      stepSeekRequest: signal(null),
      playerMode,
      currentSessionId,
      activeVideoSessionIdSignal,
      activeVideoDeviceSerial,
      currentSessionStepFrames: signal<any[]>([]),
      hasCurrentSessionStepFrames: computed(() => false),
      setPlayerMode: () => {},
      closeVideoPlayer: () => {},
      retryVideoRecording: () => {},
      consumeVideoAutoplay: () => false
    };

    const requests: Array<{ url: string; signal: AbortSignal }> = [];
    spyOn(globalThis, 'fetch').and.callFake((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const signalArg = init?.signal as AbortSignal;
      requests.push({ url, signal: signalArg });
      const firstFrame = buildFrameChunk(new Uint8Array([137, 80, 78, 71, 1, 2, 3, 4]));
      let sent = false;
      const body = {
        getReader: () => ({
          read: () => {
            if (!sent) {
              sent = true;
              return Promise.resolve({ done: false, value: firstFrame });
            }
            return new Promise<ReadableStreamReadResult<Uint8Array>>((resolve) => {
              if (signalArg.aborted) {
                resolve({ done: true, value: undefined });
              } else {
                signalArg.addEventListener(
                  'abort',
                  () => resolve({ done: true, value: undefined }),
                  { once: true }
                );
              }
            });
          },
          cancel: () => Promise.resolve(),
        }),
      };
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'multipart/x-mixed-replace; boundary=frame' }),
        body: body as unknown as ReadableStream<Uint8Array>,
      } as Response);
    });

    TestBed.configureTestingModule({
      imports: [FloatingVideoPlayerComponent],
      providers: [{ provide: AgentService, useValue: agentServiceStub }]
    });

    const fixture = TestBed.createComponent(FloatingVideoPlayerComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    expect(component.liveStreamUrl()).toBe(
      '/api/stream/device-live?serial=192.168.1.55%3A5555&session_id=wifi-sess'
    );
    expect(requests.length).toBe(1);
    expect(requests[0].url).toBe('/api/stream/device-live?serial=192.168.1.55%3A5555&session_id=wifi-sess');
    expect(requests[0].signal.aborted).toBeFalse();

    const imgEl = fixture.nativeElement.querySelector('img.live-screen-img') as HTMLImageElement | null;
    expect(imgEl).toBeTruthy();
    expect(imgEl!.getAttribute('data-stream-url')).toBe(
      '/api/stream/device-live?serial=192.168.1.55%3A5555&session_id=wifi-sess'
    );
    expect(imgEl!.getAttribute('src')).toContain('blob:');

    // Switching viewed session to a USB device aborts prior stream immediately and opens the new target
    currentSessionId.set('usb-sess');
    activeVideoSessionIdSignal.set('usb-sess');
    activeVideoDeviceSerial.set('usb-001');
    fixture.detectChanges();
    await Promise.resolve();
    fixture.detectChanges();

    expect(requests.length).toBe(2);
    expect(requests[0].signal.aborted).toBeTrue();
    expect(requests[1].url).toBe('/api/stream/device-live?serial=usb-001&session_id=usb-sess');
    expect(requests[1].signal.aborted).toBeFalse();
    expect(imgEl!.getAttribute('data-stream-url')).toBe(
      '/api/stream/device-live?serial=usb-001&session_id=usb-sess'
    );

    // Transitioning out of live (e.g. task ends -> processing) aborts active fetch without leaking sockets
    recordingPlaybackStatus.set('processing');
    fixture.detectChanges();
    expect(requests[1].signal.aborted).toBeTrue();

    // Starting next session back in live opens stream 3, and Retry preserves serial & session_id
    recordingPlaybackStatus.set('live');
    fixture.detectChanges();
    expect(requests.length).toBe(3);
    component.retryLiveStream();
    fixture.detectChanges();
    expect(requests[2].signal.aborted).toBeTrue();
    expect(requests.length).toBe(4);
    expect(requests[3].url).toContain('serial=usb-001&session_id=usb-sess&t=');

    // Destroying the component aborts the active stream cleanly
    fixture.destroy();
    expect(requests[3].signal.aborted).toBeTrue();
  });
});
