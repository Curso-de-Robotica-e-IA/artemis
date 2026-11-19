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

describe('FloatingVideoPlayerComponent live stream target routing', () => {
  it('binds liveScreenUrl to active session and device serial and updates on device switch', () => {
    const recordingPlaybackStatus = signal<'idle' | 'live' | 'processing' | 'ready' | 'failed' | 'unavailable'>('live');
    const currentSessionId = signal<string | null>('wifi-sess');
    const activeVideoSessionIdSignal = signal<string | null>('wifi-sess');
    const activeVideoDeviceSerial = signal<string | null>('192.168.1.55:5555');

    const agentServiceStub = {
      isVideoWindowOpen: signal(true),
      isVideoMinimized: signal(false),
      activeVideoUrl: signal<string | null>(null),
      activeVideoSegments: signal<any[]>([]),
      activeVideoTitle: signal('WiFi Task'),
      isVideoLoading: signal(false),
      recordingPlaybackStatus,
      recordingPlaybackMessage: signal(''),
      shouldAutoplayVideo: signal(false),
      videoSeekRequest: signal(null),
      stepSeekRequest: signal(null),
      playerMode: signal<'video' | 'steps'>('video'),
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

    TestBed.configureTestingModule({
      imports: [FloatingVideoPlayerComponent],
      providers: [{ provide: AgentService, useValue: agentServiceStub }]
    });

    const fixture = TestBed.createComponent(FloatingVideoPlayerComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.liveStreamUrl()).toBe(
      '/api/stream/device-live?serial=192.168.1.55%3A5555&session_id=wifi-sess'
    );

    const imgEl = fixture.nativeElement.querySelector('img.live-screen-img') as HTMLImageElement | null;
    expect(imgEl).toBeTruthy();
    expect(imgEl!.getAttribute('src')).toBe(
      '/api/stream/device-live?serial=192.168.1.55%3A5555&session_id=wifi-sess'
    );

    // Switching viewed session to a USB device updates stream target immediately
    currentSessionId.set('usb-sess');
    activeVideoSessionIdSignal.set('usb-sess');
    activeVideoDeviceSerial.set('usb-001');
    fixture.detectChanges();

    expect(component.liveStreamUrl()).toBe(
      '/api/stream/device-live?serial=usb-001&session_id=usb-sess'
    );
    expect(imgEl!.getAttribute('src')).toBe(
      '/api/stream/device-live?serial=usb-001&session_id=usb-sess'
    );

    // Clicking retry preserves serial & session_id alongside cache-busting timestamp
    component.retryLiveStream();
    fixture.detectChanges();
    expect(component.liveStreamUrl()).toContain('serial=usb-001&session_id=usb-sess&t=');

    fixture.destroy();
  });
});
