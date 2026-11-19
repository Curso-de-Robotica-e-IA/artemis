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

import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ChatInterfaceComponent } from './chat-interface.component';
import { AgentService, Session } from '../../services/agent.service';

describe('ChatInterfaceComponent Active Queue sequential multi-device rendering', () => {
  it('renders 1 RUNNING task followed by N-1 PENDING tasks with device serial badges', () => {
    const sessions = signal<Session[]>([
      {
        session_id: 's1',
        initial_goal: 'Open Settings',
        start_time: 100,
        status: 'running',
        device_serial: 'emulator-5554'
      },
      {
        session_id: 's2',
        initial_goal: 'Open Settings',
        start_time: 100.001,
        status: 'pending',
        device_serial: '192.168.1.55:5555'
      },
      {
        session_id: 's3',
        initial_goal: 'Open Settings',
        start_time: 100.002,
        status: 'pending',
        device_serial: 'usb-pixel-9'
      }
    ]);
    const currentSessionId = signal<string | null>('s1');
    const selectSessionSpy = jasmine.createSpy('selectSession');

    const agentServiceStub = {
      sessions,
      currentSessionId,
      runningSessionId: signal<string | null>('s1'),
      agentStatus: signal<string>('running'),
      activeTab: signal<'tasks' | 'notes'>('tasks'),
      currentNotes: signal<Record<string, string>>({}),
      selectedNoteKey: signal<string>('task_plan.md'),
      selectSession: selectSessionSpy
    };

    TestBed.configureTestingModule({
      imports: [ChatInterfaceComponent],
      providers: [{ provide: AgentService, useValue: agentServiceStub }]
    });

    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.activeQueue().length).toBe(3);

    const host = fixture.nativeElement as HTMLElement;
    const cards = Array.from(host.querySelectorAll('.task-card'));
    expect(cards.length).toBe(3);

    expect(cards[0].querySelector('.task-badge')?.textContent?.trim()).toBe('RUNNING');
    expect(cards[0].querySelector('.device-name')?.textContent?.trim()).toBe('emulator-5554');
    expect(cards[1].querySelector('.task-badge')?.textContent?.trim()).toBe('PENDING');
    expect(cards[1].querySelector('.device-name')?.textContent?.trim()).toBe('192.168.1.55:5555');
    expect(cards[2].querySelector('.task-badge')?.textContent?.trim()).toBe('PENDING');
    expect(cards[2].querySelector('.device-name')?.textContent?.trim()).toBe('usb-pixel-9');

    // When s1 finishes and s2 advances to RUNNING, Active Queue shows s2 RUNNING and s3 PENDING
    sessions.set([
      {
        session_id: 's1',
        initial_goal: 'Open Settings',
        start_time: 100,
        status: 'completed',
        device_serial: 'emulator-5554'
      },
      {
        session_id: 's2',
        initial_goal: 'Open Settings',
        start_time: 100.001,
        status: 'running',
        device_serial: '192.168.1.55:5555'
      },
      {
        session_id: 's3',
        initial_goal: 'Open Settings',
        start_time: 100.002,
        status: 'pending',
        device_serial: 'usb-pixel-9'
      }
    ]);
    currentSessionId.set('s2');
    fixture.detectChanges();

    expect(component.activeQueue().map(s => s.session_id)).toEqual(['s2', 's3']);
    expect(component.historyTasks().map(s => s.session_id)).toEqual(['s1']);

    fixture.destroy();
  });
});
