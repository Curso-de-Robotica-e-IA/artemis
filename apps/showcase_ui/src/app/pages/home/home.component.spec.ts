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

import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { HomeComponent } from './home.component';
import { AgentService } from '../../services/agent.service';
import { ModelConfigEnvResponse } from '../../services/system.service';

describe('HomeComponent Screenshot Resolution Card', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    const agentServiceStub = {
      getProTuningDefaults: () => of({}),
      runTask: () => of({})
    };

    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: AgentService, useValue: agentServiceStub }
      ]
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
  });

  function drainNonSaveRequests(): void {
    http.match(r => r.url !== '/api/system/image-resolution').forEach(other => {
      if (other.cancelled) {
        return;
      }
      if (other.request.url === '/api/system/adb/server') {
        other.flush({
          endpoint: { mode: 'local', host: '127.0.0.1', port: 5037, source: 'default', persisted: false },
          active_connections: []
        });
      } else if (other.request.url.startsWith('/api/system/readiness')) {
        other.flush({ probes: [], overall_status: 'pass', ready: true, active_device: null, os_type: 'linux' });
      } else {
        other.flush({});
      }
    });
  }

  afterEach(() => {
    drainNonSaveRequests();
    http.verify();
  });

  function buildMockConfig(imageMaxDim: number): ModelConfigEnvResponse {
    return {
      config_path: 'config/artemis.jsonc',
      config_filename: 'artemis.jsonc',
      config_content: '{}',
      image_max_dim: imageMaxDim,
      default_model: {
        provider: 'google',
        model: 'gemini-3.8-flash'
      },
      pro_targets: [],
      supported_providers: [
        { id: 'google', label: 'Google Gemini', uses_base_url: false, env_key: 'GEMINI_API_KEY' }
      ],
      presets: {},
      env_path: '.env',
      env_filename: '.env',
      env_vars: []
    };
  }

  function flushInitialModelConfig(imageMaxDim: number): void {
    const req = http.expectOne('/api/system/model-config-env');
    req.flush(buildMockConfig(imageMaxDim));
    drainNonSaveRequests();
  }

  it('renders only the 6 fixed resolution options on one row without custom input or zero labels', () => {
    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    component.activeTab.set('diagnostics');
    component.modelSetupMode.set('custom');
    fixture.detectChanges();

    flushInitialModelConfig(1280);
    fixture.detectChanges();

    const card: HTMLElement | null = fixture.nativeElement.querySelector('.image-resolution-card');
    expect(card).toBeTruthy();

    expect(card!.querySelector('input')).toBeNull();
    expect(card!.textContent).not.toContain('disables downscaling');
    expect(card!.textContent).not.toContain('(0)');
    expect(card!.textContent).not.toContain('(Disabled)');

    const row = card!.querySelector('.resolution-controls-row');
    expect(row).toBeTruthy();
    expect(row!.querySelector('.resolution-segmented-control')).toBeTruthy();
    expect(row!.querySelector('.btn-picker-primary')?.textContent).toContain('Apply');

    const optionButtons = Array.from(
      row!.querySelectorAll<HTMLButtonElement>('.resolution-segment-btn')
    );
    expect(optionButtons.map(btn => btn.textContent?.trim())).toEqual([
      '720',
      '1024',
      '1280',
      '1600',
      '1920',
      'Original'
    ]);

    const badge = card!.querySelector('.resolution-active-badge');
    expect(badge?.textContent?.trim()).toBe('1280 px');

    const selectedInitially = optionButtons.filter(btn => btn.classList.contains('is-selected'));
    expect(selectedInitially.length).toBe(1);
    expect(selectedInitially[0].textContent?.trim()).toBe('1280');

    // Selecting 1920 then Original keeps only one option highlighted at a time.
    optionButtons[4].click();
    fixture.detectChanges();
    const selected1920 = optionButtons.filter(btn => btn.classList.contains('is-selected'));
    expect(selected1920.length).toBe(1);
    expect(selected1920[0].textContent?.trim()).toBe('1920');

    const originalBtn = optionButtons[5];
    originalBtn.click();
    fixture.detectChanges();

    const selectedAfterClick = optionButtons.filter(btn => btn.classList.contains('is-selected'));
    expect(selectedAfterClick.length).toBe(1);
    expect(selectedAfterClick[0].textContent?.trim()).toBe('Original');

    const applyBtn = row!.querySelector<HTMLButtonElement>('.btn-picker-primary')!;
    applyBtn.click();

    const saveReq = http.expectOne('/api/system/image-resolution');
    expect(saveReq.request.method).toBe('POST');
    expect(saveReq.request.body).toEqual({ image_max_dim: 0 });
    saveReq.flush({
      status: 'success',
      message: 'Screenshot downscaling disabled (Original resolution active).',
      image_max_dim: 0,
      config_path: 'config/artemis.jsonc',
      model_config_env: buildMockConfig(0)
    });
    fixture.detectChanges();

    expect(badge?.textContent?.trim()).toBe('Original');
    expect(badge?.classList.contains('is-original')).toBeTrue();

    fixture.destroy();
  });
});
