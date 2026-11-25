import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { SystemReadinessReport } from '../core/models/system.model';
import { SystemService } from './system.service';

describe('SystemService readiness polling', () => {
  let service: SystemService;
  let http: HttpTestingController;

  const report = (timestamp: number): SystemReadinessReport => ({
    overall_ready: true,
    blocker_count: 3,
    passed_blocker_count: 3,
    probes: [],
    active_device: null,
    os_type: 'windows',
    timestamp
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        SystemService,
        provideHttpClient(withXhr()),
        provideHttpClientTesting()
      ]
    });
    service = TestBed.inject(SystemService);
    http = TestBed.inject(HttpTestingController);
    service.stopAutoPolling();
  });

  afterEach(() => http.verify());

  it('shares one HTTP request across overlapping readiness callers', () => {
    let firstTimestamp = 0;
    let secondTimestamp = 0;

    service.fetchReadiness().subscribe(value => firstTimestamp = value.timestamp);
    service.fetchReadiness(true).subscribe(value => secondTimestamp = value.timestamp);

    const request = http.expectOne('/api/system/readiness');
    request.flush(report(10));

    expect(firstTimestamp).toBe(10);
    expect(secondTimestamp).toBe(10);
    expect(service.readinessReport()?.timestamp).toBe(10);
    expect(service.isLoading()).toBeFalse();
  });

  it('does not let an older snapshot replace a newer report', () => {
    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush(report(20));

    service.fetchReadiness().subscribe();
    http.expectOne('/api/system/readiness').flush(report(10));

    expect(service.readinessReport()?.timestamp).toBe(20);
  });

  it('requests a forced backend refresh for a manual re-check', () => {
    service.fetchReadiness(false, true).subscribe();

    const request = http.expectOne(req =>
      req.url === '/api/system/readiness' && req.params.get('force') === 'true'
    );
    expect(request.request.params.get('force')).toBe('true');
    request.flush(report(30));
  });

  it('loads the active ADB server endpoint', () => {
    service.fetchAdbServerStatus().subscribe();

    const request = http.expectOne('/api/system/adb/server');
    request.flush({
      endpoint: {
        host: '127.0.0.1',
        port: 5038,
        socket: 'tcp:127.0.0.1:5038',
        mode: 'remote',
        is_local_default: false
      }
    });

    expect(service.isRemoteAdbServer()).toBeTrue();
    expect(service.adbServerStatus()?.endpoint.port).toBe(5038);
  });

  it('activates an ADB server endpoint and applies its readiness report', () => {
    service.connectAdbServer('127.0.0.1', 5038, true).subscribe();

    const request = http.expectOne('/api/system/adb/server/connect');
    expect(request.request.body).toEqual({
      host: '127.0.0.1',
      port: 5038,
      persist: true
    });
    request.flush({
      connection_result: {
        success: true,
        message: 'Connected.',
        endpoint: {
          host: '127.0.0.1',
          port: 5038,
          socket: 'tcp:127.0.0.1:5038',
          mode: 'remote',
          is_local_default: false
        },
        devices: []
      },
      report: report(40)
    });

    expect(service.isRemoteAdbServer()).toBeTrue();
    expect(service.readinessReport()?.timestamp).toBe(40);
  });

  it('probes an ADB server without changing the active endpoint', () => {
    service.probeAdbServer('127.0.0.1', 5038).subscribe();

    const request = http.expectOne('/api/system/adb/server/probe');
    expect(request.request.body).toEqual({
      host: '127.0.0.1',
      port: 5038,
      persist: false
    });
    request.flush({
      connection_result: {
        success: true,
        message: 'Endpoint is reachable.',
        endpoint: {
          host: '127.0.0.1',
          port: 5038,
          socket: 'tcp:127.0.0.1:5038',
          identity: 'tcp:127.0.0.1:5038',
          mode: 'remote',
          is_local_default: false
        },
        devices: []
      }
    });

    expect(service.adbServerStatus()).toBeNull();
  });

  it('restores the standard local ADB server explicitly', () => {
    service.useLocalAdbServer(true).subscribe();

    const request = http.expectOne(req =>
      req.url === '/api/system/adb/server/local' && req.params.get('persist') === 'true'
    );
    request.flush({
      connection_result: {
        success: true,
        message: 'Using local ADB.',
        endpoint: {
          host: '127.0.0.1',
          port: 5037,
          socket: 'tcp:127.0.0.1:5037',
          mode: 'local',
          is_local_default: true
        },
        devices: []
      },
      report: report(50)
    });

    expect(service.isRemoteAdbServer()).toBeFalse();
    expect(service.adbServerStatus()?.endpoint.port).toBe(5037);
  });

  it('fetches available models from a custom Ollama endpoint', () => {
    service.fetchCustomModels('http://192.168.155.5:11434/v1').subscribe();

    const request = http.expectOne(req =>
      req.url === '/api/system/custom-models' &&
      req.params.get('base_url') === 'http://192.168.155.5:11434/v1'
    );
    request.flush({
      base_url: 'http://192.168.155.5:11434/v1',
      endpoint_used: 'http://192.168.155.5:11434/v1/models',
      source: 'ollama',
      models: [
        { id: 'qwen3.8:27b', name: 'qwen3.8:27b', owned_by: 'library', parameter_size: '27B' }
      ]
    });

    expect(service.customModels().length).toBe(1);
    expect(service.customModels()[0].id).toBe('qwen3.8:27b');
  });

  it('updates default model configuration and refreshes modelConfigEnv', () => {
    service.updateDefaultModel({
      provider: 'openai',
      model: 'qwen3.8:27b',
      update_fallback: true,
      base_url: 'http://192.168.155.5:11434/v1',
      persist_base_url: true
    }).subscribe();

    const request = http.expectOne('/api/system/model-config/default');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      provider: 'openai',
      model: 'qwen3.8:27b',
      update_fallback: true,
      base_url: 'http://192.168.155.5:11434/v1',
      persist_base_url: true
    });
    request.flush({
      status: 'success',
      message: 'Default model updated to openai/qwen3.8:27b.',
      default_model: { provider: 'openai', model: 'qwen3.8:27b' },
      config_path: 'config/artemis.jsonc',
      model_config_env: {
        config_path: 'config/artemis.jsonc',
        config_filename: 'artemis.jsonc',
        config_content: '{}',
        default_model: { provider: 'openai', model: 'qwen3.8:27b' },
        presets: {},
        env_path: '.env',
        env_filename: '.env',
        env_vars: []
      }
    });

    expect(service.modelConfigEnv()?.default_model?.model).toBe('qwen3.8:27b');
  });

  it('fetches provider models and caches them per provider', () => {
    service.fetchCustomModels(undefined, 'google').subscribe();

    const request = http.expectOne(req =>
      req.url === '/api/system/custom-models' &&
      req.params.get('provider') === 'google' &&
      !req.params.has('base_url')
    );
    request.flush({
      provider: 'google',
      base_url: null,
      endpoint_used: 'https://generativelanguage.googleapis.com/v1beta/models',
      source: 'google',
      fallback_used: false,
      models: [
        { id: 'gemini-2.5-flash', name: 'Gemini 2.5 Flash', provider: 'google', owned_by: 'google' }
      ]
    });

    expect(service.providerModelsCache()['google']?.models.length).toBe(1);
    expect(service.providerModelsCache()['google']?.models[0].id).toBe('gemini-2.5-flash');
  });

  it('updates and resets a Pro Mode target model configuration', () => {
    service.updateTargetModel({
      target: 'object_detector',
      provider: 'google',
      model: 'gemini-robotics-er-2-preview'
    }).subscribe();

    const applyReq = http.expectOne('/api/system/model-config/target');
    expect(applyReq.request.method).toBe('POST');
    expect(applyReq.request.body).toEqual({
      target: 'object_detector',
      provider: 'google',
      model: 'gemini-robotics-er-2-preview'
    });
    applyReq.flush({
      status: 'success',
      message: 'Updated nodes.object_detector',
      target: {
        key: 'object_detector',
        label: 'Object Detector',
        config_path: 'nodes.object_detector',
        description: 'Locates UI elements',
        supports_fallback: true,
        effective_provider: 'google',
        effective_model: 'gemini-robotics-er-2-preview',
        is_inheriting_default: false,
        active_badge: 'google / gemini-robotics-er-2-preview'
      },
      pro_targets: [],
      config_path: 'config/artemis.jsonc',
      model_config_env: {
        config_path: 'config/artemis.jsonc',
        config_filename: 'artemis.jsonc',
        config_content: '{}',
        default_model: { provider: 'ollama', model: 'qwen3.8:27b' },
        pro_targets: [{
          key: 'object_detector',
          label: 'Object Detector',
          config_path: 'nodes.object_detector',
          description: 'Locates UI elements',
          supports_fallback: true,
          effective_provider: 'google',
          effective_model: 'gemini-robotics-er-2-preview',
          is_inheriting_default: false,
          active_badge: 'google / gemini-robotics-er-2-preview'
        }],
        presets: {},
        env_path: '.env',
        env_filename: '.env',
        env_vars: []
      }
    });

    expect(service.modelConfigEnv()?.pro_targets?.[0].is_inheriting_default).toBeFalse();

    service.resetTargetModel('object_detector').subscribe();
    const resetReq = http.expectOne('/api/system/model-config/target/reset');
    expect(resetReq.request.method).toBe('POST');
    expect(resetReq.request.body).toEqual({ target: 'object_detector' });
    resetReq.flush({
      status: 'success',
      message: 'Reset nodes.object_detector',
      target: {
        key: 'object_detector',
        label: 'Object Detector',
        config_path: 'nodes.object_detector',
        description: 'Locates UI elements',
        supports_fallback: true,
        effective_provider: 'ollama',
        effective_model: 'qwen3.8:27b',
        is_inheriting_default: true,
        active_badge: 'Inheriting Default'
      },
      pro_targets: [],
      config_path: 'config/artemis.jsonc',
      model_config_env: {
        config_path: 'config/artemis.jsonc',
        config_filename: 'artemis.jsonc',
        config_content: '{}',
        default_model: { provider: 'ollama', model: 'qwen3.8:27b' },
        pro_targets: [{
          key: 'object_detector',
          label: 'Object Detector',
          config_path: 'nodes.object_detector',
          description: 'Locates UI elements',
          supports_fallback: true,
          effective_provider: 'ollama',
          effective_model: 'qwen3.8:27b',
          is_inheriting_default: true,
          active_badge: 'Inheriting Default'
        }],
        presets: {},
        env_path: '.env',
        env_filename: '.env',
        env_vars: []
      }
    });

    expect(service.modelConfigEnv()?.pro_targets?.[0].is_inheriting_default).toBeTrue();
  });

  it('fetches and updates screenshot max dimension (image_max_dim)', () => {
    service.fetchImageResolution().subscribe(res => {
      expect(res.image_max_dim).toBe(1280);
      expect(res.source).toBe('artemis.jsonc');
    });

    const getReq = http.expectOne('/api/system/image-resolution');
    expect(getReq.request.method).toBe('GET');
    getReq.flush({
      image_max_dim: 1280,
      source: 'artemis.jsonc',
      config_path: 'config/artemis.jsonc'
    });

    service.updateImageResolution(1600).subscribe(res => {
      expect(res.image_max_dim).toBe(1600);
    });

    const postReq = http.expectOne('/api/system/image-resolution');
    expect(postReq.request.method).toBe('POST');
    expect(postReq.request.body).toEqual({ image_max_dim: 1600 });
    postReq.flush({
      status: 'success',
      message: 'Screenshot resolution updated to 1600 px.',
      image_max_dim: 1600,
      config_path: 'config/artemis.jsonc',
      model_config_env: {
        config_path: 'config/artemis.jsonc',
        config_filename: 'artemis.jsonc',
        config_content: '{}',
        image_max_dim: 1600,
        default_model: { provider: 'ollama', model: 'qwen3.8:27b' },
        presets: {},
        env_path: '.env',
        env_filename: '.env',
        env_vars: []
      }
    });

    expect(service.modelConfigEnv()?.image_max_dim).toBe(1600);
  });

  it('evaluates isCredentialsReady dynamically for local-only, cloud, and Pro Mode providers', () => {
    // 1. Fallback before modelConfigEnv loads relies on llmProbe status.
    expect(service.isCredentialsReady()).toBeFalse();
    service.readinessReport.set({
      overall_ready: true,
      blocker_count: 3,
      passed_blocker_count: 3,
      probes: [
        {
          id: 'gemini_api_key',
          category: 'auth',
          title: 'Multimodal LLM API Key',
          status: 'pass',
          is_blocker: true,
          summary: 'Active',
          description: 'Ready',
          metadata: { api_keys: { google: 'AIza-test' } },
          actions: []
        }
      ],
      active_device: null,
      os_type: 'linux',
      timestamp: 1
    });
    expect(service.isCredentialsReady()).toBeTrue();

    // 2. Local-only configuration (ollama default + local/inheriting Pro targets) is ready without API keys.
    service.readinessReport.set({
      overall_ready: false,
      blocker_count: 3,
      passed_blocker_count: 2,
      probes: [
        {
          id: 'gemini_api_key',
          category: 'auth',
          title: 'Multimodal LLM API Key',
          status: 'fail',
          is_blocker: true,
          summary: 'Missing',
          description: 'No key set',
          metadata: { api_keys: {} },
          actions: []
        }
      ],
      active_device: null,
      os_type: 'linux',
      timestamp: 2
    });
    service.modelConfigEnv.set({
      config_path: 'config/artemis.jsonc',
      config_filename: 'artemis.jsonc',
      config_content: '{}',
      default_model: { provider: 'ollama', model: 'qwen3.8:27b' },
      pro_targets: [
        {
          key: 'object_detector',
          label: 'Object Detector',
          config_path: 'nodes.object_detector',
          description: 'Detector',
          supports_fallback: true,
          effective_provider: 'ollama',
          effective_model: 'qwen3.8:27b',
          is_inheriting_default: true,
          active_badge: 'Inheriting Default'
        },
        {
          key: 'hopper',
          label: 'Hopper',
          config_path: 'nodes.hopper',
          description: 'Hopper',
          supports_fallback: true,
          effective_provider: 'vllm',
          effective_model: 'local-model',
          is_inheriting_default: false,
          active_badge: 'vllm/local-model'
        }
      ],
      presets: {},
      env_path: '.env',
      env_filename: '.env',
      env_vars: [
        { name: 'GEMINI_API_KEY', provider: 'google', is_set: false, preview: '', description: '' },
        { name: 'OPENAI_BASE_URL', provider: 'custom', is_set: false, preview: '', description: '' }
      ]
    });
    expect(service.isCredentialsReady()).toBeTrue();

    // 3. Hybrid setup: local default + active cloud Pro Mode override requires that cloud provider key.
    service.modelConfigEnv.update((current) => ({
      ...current!,
      pro_targets: [
        {
          key: 'object_detector',
          label: 'Object Detector',
          config_path: 'nodes.object_detector',
          description: 'Detector',
          supports_fallback: true,
          effective_provider: 'openai',
          effective_model: 'gpt-4o',
          is_inheriting_default: false,
          active_badge: 'openai/gpt-4o'
        }
      ],
      env_vars: [
        { name: 'GEMINI_API_KEY', provider: 'google', is_set: true, preview: 'AIza...1', description: '' },
        { name: 'OPENAI_API_KEY', provider: 'openai', is_set: false, preview: '', description: '' }
      ]
    }));
    expect(service.isCredentialsReady()).toBeFalse();

    // Setting OPENAI_API_KEY satisfies readiness.
    service.modelConfigEnv.update((current) => ({
      ...current!,
      env_vars: [
        { name: 'GEMINI_API_KEY', provider: 'google', is_set: true, preview: 'AIza...1', description: '' },
        { name: 'OPENAI_API_KEY', provider: 'openai', is_set: true, preview: 'sk-...9', description: '' }
      ]
    }));
    expect(service.isCredentialsReady()).toBeTrue();
  });
});
