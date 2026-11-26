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
import { DeviceInfo, SystemReadinessReport } from '../../core/models/system.model';

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

  it('renders unified AI Model Setup panel without mode cards and auto-fetches active provider models', () => {
    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    component.activeTab.set('diagnostics');
    fixture.detectChanges();

    const envReq = http.expectOne('/api/system/model-config-env');
    envReq.flush({
      ...buildMockConfig(1280),
      pro_targets: [
        {
          key: 'object_detector',
          label: 'Object Detector',
          config_path: 'nodes.object_detector',
          description: 'Detects UI elements',
          supports_fallback: true,
          explicit_provider: 'openai',
          explicit_model: 'gpt-4o',
          effective_provider: 'openai',
          effective_model: 'gpt-4o',
          is_inheriting_default: false,
          active_badge: 'openai/gpt-4o'
        }
      ],
      env_vars: [
        {
          name: 'GEMINI_API_KEY',
          provider: 'google',
          is_set: false,
          preview: '',
          description: 'Gemini key'
        },
        {
          name: 'VISION_API_KEY',
          provider: 'ocr',
          is_set: false,
          preview: '',
          description: 'OCR key'
        }
      ]
    });
    drainNonSaveRequests();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.model-choice-cards-row')).toBeNull();
    expect(fixture.nativeElement.querySelector('.custom-config-inspector')).toBeTruthy();

    // Pro Mode Component Models section starts collapsed by default and expands on toggle
    expect(component.showProModeConfig()).toBeFalse();
    expect(fixture.nativeElement.querySelector('.pro-targets-list')).toBeNull();
    component.toggleProModeConfig();
    fixture.detectChanges();
    expect(component.showProModeConfig()).toBeTrue();
    expect(fixture.nativeElement.querySelector('.pro-targets-list')).toBeTruthy();

    // Auto-fetched default provider ('google') and Pro Mode target provider ('openai')
    const googleModelsReq = http.expectOne(
      r => r.url === '/api/system/custom-models' && r.params.get('provider') === 'google'
    );
    googleModelsReq.flush({
      provider: 'google',
      models: [{ id: 'gemini-3.8-flash', name: 'Gemini 3.8 Flash', provider: 'google' }]
    });

    const openaiModelsReq = http.expectOne(
      r => r.url === '/api/system/custom-models' && r.params.get('provider') === 'openai'
    );
    openaiModelsReq.flush({
      provider: 'openai',
      models: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai' }]
    });

    fixture.destroy();
  });

  it('supports editing, testing, and clearing GEMINI_API_KEY and VISION_API_KEY in unified panel', () => {
    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    component.activeTab.set('diagnostics');
    fixture.detectChanges();

    const initialConfig = {
      ...buildMockConfig(1280),
      env_vars: [
        {
          name: 'GEMINI_API_KEY',
          provider: 'google',
          is_set: false,
          preview: '',
          description: 'Gemini key'
        },
        {
          name: 'VISION_API_KEY',
          provider: 'ocr',
          is_set: false,
          preview: '',
          description: 'OCR key'
        }
      ]
    };

    http.expectOne('/api/system/model-config-env').flush(initialConfig);
    drainNonSaveRequests();
    fixture.detectChanges();

    // Flush the automatic initial model discovery for default provider ('google')
    http
      .expectOne(r => r.url === '/api/system/custom-models' && r.params.get('provider') === 'google')
      .flush({ provider: 'google', models: [] });

    // Test GEMINI_API_KEY
    component.onGeminiKeyChange('AIza-test-gemini-1234');
    fixture.detectChanges();

    const testGeminiBtn = fixture.nativeElement.querySelector(
      '[data-action="test-gemini-key"]'
    ) as HTMLButtonElement;
    testGeminiBtn.click();

    const testGeminiReq = http.expectOne('/api/system/credentials/test');
    expect(testGeminiReq.request.body).toEqual({
      provider: 'google',
      api_key: 'AIza-test-gemini-1234',
      base_url: undefined
    });
    testGeminiReq.flush({ valid: true, provider: 'google', message: 'Valid key' });
    fixture.detectChanges();

    // Save GEMINI_API_KEY -> refreshes env and reloads Google model list
    const saveGeminiBtn = fixture.nativeElement.querySelector(
      '[data-action="save-gemini-key"]'
    ) as HTMLButtonElement;
    saveGeminiBtn.click();

    const saveGeminiReq = http.expectOne('/api/system/credentials');
    expect(saveGeminiReq.request.body).toEqual({
      provider: 'google',
      api_key: 'AIza-test-gemini-1234',
      persist_to_env: true
    });
    saveGeminiReq.flush({ status: 'success', message: 'Saved' });

    const refreshedEnvAfterSave = http.expectOne('/api/system/model-config-env');
    refreshedEnvAfterSave.flush({
      ...initialConfig,
      env_vars: [
        {
          name: 'GEMINI_API_KEY',
          provider: 'google',
          is_set: true,
          preview: 'AIza...1234',
          description: 'Gemini key'
        },
        initialConfig.env_vars[1]
      ]
    });

    const reloadModelsReq = http.expectOne(
      r => r.url === '/api/system/custom-models' && r.params.get('provider') === 'google'
    );
    reloadModelsReq.flush({
      provider: 'google',
      models: [{ id: 'gemini-3.8-flash', name: 'Gemini 3.8 Flash', provider: 'google' }]
    });
    fixture.detectChanges();
    expect(component.isCredentialsReady()).toBeTrue();

    // Clear GEMINI_API_KEY -> refreshes env and reloads Google model list
    const clearGeminiBtn = fixture.nativeElement.querySelector(
      '[data-action="clear-gemini-key"]'
    ) as HTMLButtonElement;
    clearGeminiBtn.click();

    const clearGeminiReq = http.expectOne('/api/system/credentials');
    expect(clearGeminiReq.request.body).toEqual({
      provider: 'google',
      api_key: '',
      persist_to_env: true
    });
    clearGeminiReq.flush({ status: 'success', message: 'Cleared' });

    http.expectOne('/api/system/model-config-env').flush(initialConfig);
    http
      .expectOne(r => r.url === '/api/system/custom-models' && r.params.get('provider') === 'google')
      .flush({ provider: 'google', models: [] });
    fixture.detectChanges();
    expect(component.isCredentialsReady()).toBeFalse();

    // Expand Optional Vision OCR row and save/test/clear OCR key
    const ocrToggle = fixture.nativeElement.querySelector('.ocr-card-toggle-bar') as HTMLElement;
    ocrToggle.click();
    fixture.detectChanges();

    component.onOcrKeyChange('AIza-test-ocr-9999');
    fixture.detectChanges();

    const saveOcrBtn = fixture.nativeElement.querySelector(
      '[data-action="save-ocr-key"]'
    ) as HTMLButtonElement;
    saveOcrBtn.click();

    const saveOcrReq = http.expectOne('/api/system/credentials');
    expect(saveOcrReq.request.body).toEqual({
      provider: 'ocr',
      api_key: 'AIza-test-ocr-9999',
      persist_to_env: true
    });
    saveOcrReq.flush({ status: 'success', message: 'OCR Saved' });
    http.expectOne('/api/system/model-config-env').flush({
      ...initialConfig,
      env_vars: [
        initialConfig.env_vars[0],
        {
          name: 'VISION_API_KEY',
          provider: 'ocr',
          is_set: true,
          preview: 'AIza...9999',
          description: 'OCR key'
        }
      ]
    });
    fixture.detectChanges();

    const testOcrBtn = fixture.nativeElement.querySelector(
      '[data-action="test-ocr-key"]'
    ) as HTMLButtonElement;
    testOcrBtn.click();
    const testOcrReq = http.expectOne('/api/system/credentials/test');
    expect(testOcrReq.request.body).toEqual({
      provider: 'ocr',
      api_key: 'AIza-test-ocr-9999',
      base_url: undefined
    });
    testOcrReq.flush({ valid: true, provider: 'ocr', message: 'Valid OCR key' });
    fixture.detectChanges();

    const clearOcrBtn = fixture.nativeElement.querySelector(
      '[data-action="clear-ocr-key"]'
    ) as HTMLButtonElement;
    clearOcrBtn.click();
    const clearOcrReq = http.expectOne('/api/system/credentials');
    expect(clearOcrReq.request.body).toEqual({
      provider: 'ocr',
      api_key: '',
      persist_to_env: true
    });
    clearOcrReq.flush({ status: 'success', message: 'OCR Cleared' });
    http.expectOne('/api/system/model-config-env').flush(initialConfig);

    fixture.destroy();
  });
});

describe('HomeComponent Device Connection & Selection Flow', () => {
  let http: HttpTestingController;
  let runTaskSpy: jasmine.Spy;

  const dev1: DeviceInfo = {
    serial: 'emulator-5554',
    state: 'device',
    model: 'Pixel 8',
    product: 'sdk_gphone64',
    android_version: '14',
    screen_resolution: '1080x2400',
    is_locked: false,
    is_emulator: true
  };

  const dev2: DeviceInfo = {
    serial: '192.168.1.55:5555',
    state: 'device',
    model: 'SM S928B',
    product: 'e3q',
    android_version: '15',
    screen_resolution: '1440x3120',
    is_locked: false,
    is_emulator: false
  };

  function buildReadiness(
    devices: DeviceInfo[],
    activeDevice: DeviceInfo | null = devices[0] ?? null,
    timestamp = 100
  ): SystemReadinessReport {
    const ready = devices.length > 0;
    return {
      overall_ready: ready,
      blocker_count: 3,
      passed_blocker_count: ready ? 3 : 2,
      probes: [
        {
          id: 'python_runtime',
          category: 'runtime',
          title: 'Python',
          status: 'pass',
          is_blocker: true,
          summary: 'OK',
          description: 'OK',
          metadata: {},
          actions: []
        },
        {
          id: 'system_config',
          category: 'runtime',
          title: 'Config',
          status: 'pass',
          is_blocker: true,
          summary: 'OK',
          description: 'OK',
          metadata: {},
          actions: []
        },
        {
          id: 'toolchain',
          category: 'toolchain',
          title: 'Toolchain',
          status: 'pass',
          is_blocker: false,
          summary: 'OK',
          description: 'OK',
          metadata: {},
          actions: []
        },
        {
          id: 'android_adb',
          category: 'device',
          title: 'Android ADB',
          status: ready ? 'pass' : 'warn',
          is_blocker: true,
          summary: ready ? `${devices.length} connected` : 'No devices',
          description: ready ? 'Connected' : 'Connect a device',
          metadata: { installed: true, devices, installed_avds: [] },
          actions: []
        },
        {
          id: 'gemini_api_key',
          category: 'auth',
          title: 'API Key',
          status: 'pass',
          is_blocker: true,
          summary: 'Active',
          description: 'Ready',
          metadata: { api_keys: { google: 'AIza-test' } },
          actions: []
        }
      ],
      active_device: activeDevice,
      os_type: 'linux',
      timestamp
    };
  }

  function flushInitialBootstrap(readinessPayload: any): void {
    http.expectOne('/api/system/readiness').flush(readinessPayload);
    http.expectOne('/api/system/model-config-env').flush({
      config_path: 'config/artemis.jsonc',
      config_filename: 'artemis.jsonc',
      config_content: '{}',
      image_max_dim: 1280,
      default_model: { provider: 'google', model: 'gemini-3.8-flash' },
      pro_targets: [],
      supported_providers: [],
      presets: {},
      env_path: '.env',
      env_filename: '.env',
      env_vars: [
        { name: 'GEMINI_API_KEY', provider: 'google', is_set: true, preview: 'AIza...1', description: '' }
      ]
    });
    http.expectOne('/api/system/adb/server').flush({
      endpoint: {
        host: '127.0.0.1',
        port: 5037,
        socket: '127.0.0.1:5037',
        identity: 'local',
        mode: 'local',
        is_local_default: true
      }
    });
    http.match('/api/system/custom-models').forEach(req => {
      if (!req.cancelled) {
        req.flush({ provider: 'google', models: [] });
      }
    });
  }

  beforeEach(async () => {
    runTaskSpy = jasmine.createSpy('runTask').and.returnValue(of({ status: 'queued' }));
    const agentServiceStub = {
      getProTuningDefaults: () => of({}),
      runTask: runTaskSpy
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

  afterEach(() => {
    http.match(() => true).forEach(req => {
      if (!req.cancelled) {
        req.flush({});
      }
    });
    http.verify();
  });

  it('renders all connected device cards side-by-side in Settings without SELECT ACTIVE DEVICE buttons', () => {
    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    component.activeTab.set('diagnostics');
    fixture.detectChanges();

    flushInitialBootstrap(buildReadiness([dev1, dev2], dev1, 100));
    fixture.detectChanges();

    const grid = fixture.nativeElement.querySelector('.connected-devices-grid') as HTMLElement | null;
    expect(grid).toBeTruthy();
    expect(grid!.classList.contains('grid-cols-1')).toBeTrue();
    expect(grid!.classList.contains('md:grid-cols-2')).toBeTrue();
    expect(grid!.classList.contains('gap-3')).toBeTrue();

    const cards = Array.from(grid!.querySelectorAll<HTMLElement>('.device-hero-card'));
    expect(cards.length).toBe(2);

    expect(cards[0].textContent).toContain('Pixel 8');
    expect(cards[0].textContent).toContain('emulator-5554');
    expect(cards[0].textContent).toContain('1080×2400');
    expect(cards[0].textContent).toContain('Android 14');
    expect(cards[0].textContent).toContain('Connected');

    expect(cards[1].textContent).toContain('SM S928B');
    expect(cards[1].textContent).toContain('192.168.1.55:5555');
    expect(cards[1].textContent).toContain('1440×3120');
    expect(cards[1].textContent).toContain('Android 15');
    expect(cards[1].textContent).toContain('Connected');

    // No active device selection controls inside System Setup & Prerequisites
    const setupView = fixture.nativeElement.querySelector('.setup-guide-view') as HTMLElement;
    expect(setupView.querySelector('.multi-device-switcher')).toBeNull();
    expect(setupView.textContent?.toUpperCase()).not.toContain('SELECT ACTIVE DEVICE');
    expect(setupView.textContent?.toUpperCase()).not.toContain('SELECT TARGET DEVICES');

    // Header actions remain intact
    expect(setupView.textContent).toContain('Change Connection');
    expect(setupView.textContent).toContain('Restart ADB');

    fixture.destroy();
  });

  it('supports 1-to-N device selection with Select All toggle and dynamic Run Task button labels', () => {
    const dev3: DeviceInfo = {
      serial: 'pixel-9-usb',
      state: 'device',
      model: 'Pixel 9',
      product: 'tokay',
      android_version: '15',
      screen_resolution: '1080x2424',
      is_locked: false,
      is_emulator: false
    };

    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();

    flushInitialBootstrap(buildReadiness([dev1, dev2, dev3], dev1, 100));
    fixture.detectChanges();

    expect(component.activeTab()).toBe('launcher');

    const deviceBar = fixture.nativeElement.querySelector('.composer-device-bar') as HTMLElement | null;
    expect(deviceBar).toBeTruthy();
    expect(deviceBar!.textContent).toContain('SELECT TARGET DEVICES:');

    const pills = Array.from(deviceBar!.querySelectorAll<HTMLButtonElement>('.device-chip-btn'));
    expect(pills.length).toBe(3);
    expect(pills[0].classList.contains('selected')).toBeTrue();
    expect(pills[0].textContent).toContain('✓ Selected');
    expect(pills[1].classList.contains('selected')).toBeFalse();
    expect(pills[2].classList.contains('selected')).toBeFalse();

    const runBtn = fixture.nativeElement.querySelector('.btn-run-composer') as HTMLButtonElement;
    expect(runBtn.textContent).toContain('Run Task');
    expect(runBtn.textContent).not.toContain('Devices');

    // Cannot deselect the last remaining selected device
    pills[0].click();
    fixture.detectChanges();
    expect(pills[0].classList.contains('selected')).toBeTrue();

    // Select second device -> 2 devices selected, label shows Run Task (2 Devices)
    pills[1].click();
    fixture.detectChanges();
    expect(pills[0].classList.contains('selected')).toBeTrue();
    expect(pills[1].classList.contains('selected')).toBeTrue();
    expect(pills[2].classList.contains('selected')).toBeFalse();
    expect(runBtn.textContent).toContain('Run Task (2 Devices)');

    // Select All button appears for N >= 2 devices and toggles all 3 on, then resets to first device
    const selectAllBtn = deviceBar!.querySelector('.select-all-chip-btn') as HTMLButtonElement | null;
    expect(selectAllBtn).toBeTruthy();
    expect(selectAllBtn!.textContent).toContain('Select All (3)');

    selectAllBtn!.click();
    fixture.detectChanges();
    expect(pills.every(p => p.classList.contains('selected'))).toBeTrue();
    expect(runBtn.textContent).toContain('Run Task (3 Devices)');

    selectAllBtn!.click();
    fixture.detectChanges();
    expect(pills[0].classList.contains('selected')).toBeTrue();
    expect(pills[1].classList.contains('selected')).toBeFalse();
    expect(pills[2].classList.contains('selected')).toBeFalse();
    expect(runBtn.textContent).toContain('Run Task');

    // Re-select dev1 and dev2 and submit -> submits ordered array of serials
    pills[1].click();
    component.taskGoal.set('Open Settings');
    fixture.detectChanges();
    runBtn.click();

    expect(runTaskSpy).toHaveBeenCalledWith(
      'Open Settings',
      'flash',
      undefined,
      undefined,
      undefined,
      ['emulator-5554', '192.168.1.55:5555']
    );

    // If dev2 disconnects and only dev1 remains, Select All hides and dev1 stays selected
    component.systemService.readinessReport.set(buildReadiness([dev1], null, 120));
    fixture.detectChanges();

    const remainingPills = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
        '.composer-device-bar .device-chip-btn'
      )
    );
    expect(remainingPills.length).toBe(1);
    expect(remainingPills[0].classList.contains('selected')).toBeTrue();
    expect(remainingPills[0].textContent).toContain('Pixel 8');
    expect(fixture.nativeElement.querySelector('.select-all-chip-btn')).toBeNull();
    expect(runBtn.textContent).toContain('Run Task');
    expect(runBtn.textContent).not.toContain('Devices');

    fixture.destroy();
  });

  it('automatically redirects to System Setup & Prerequisites and disables launcher tab when no devices are connected', () => {
    const fixture = TestBed.createComponent(HomeComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();

    // Start with 1 connected device on launcher tab
    flushInitialBootstrap(buildReadiness([dev1], dev1, 100));
    fixture.detectChanges();
    expect(component.activeTab()).toBe('launcher');

    const tabButtons = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>('.mode-tab-btn')
    );
    const launcherTabBtn = tabButtons[1];
    expect(launcherTabBtn.disabled).toBeFalse();

    // Disconnect all devices -> auto-redirects to System Setup & Prerequisites with connection tabs
    component.systemService.readinessReport.set(buildReadiness([], null, 110));
    fixture.detectChanges();

    expect(component.activeTab()).toBe('diagnostics');
    expect(launcherTabBtn.disabled).toBeTrue();
    expect(fixture.nativeElement.querySelector('.foolproof-guide-container')).toBeTruthy();
    expect(fixture.nativeElement.textContent).toContain('Android Emulator');
    expect(fixture.nativeElement.textContent).toContain('USB Cable');
    expect(fixture.nativeElement.textContent).toContain('Wireless ADB');
    expect(fixture.nativeElement.textContent).toContain('Remote Computer');

    // Calling setTab('launcher') while disconnected stays on diagnostics
    component.setTab('launcher');
    expect(component.activeTab()).toBe('diagnostics');

    fixture.destroy();
  });
});
