import { afterEach, describe, expect, test, vi } from 'vitest';
import axios from 'axios';
import {
  extractPathSuffix,
  formatProxyEnvText,
  getCapacity,
  getEntryTraffic,
  getInspectionReport,
  getInspectionReportMarkdown,
  getMonitor,
  getObservability,
  getOpsStatus,
  getServicesSnapshot,
  getTailnet,
  healthProbePayload,
  parseSystemdUnits,
  runServiceAction,
  saveMachine,
} from './api';
import { cn } from './utils';
import type { MachineFormValues, ObservabilityPayload } from './types';

// getObservability 走 axios 实例；用 hoisted 的 get mock 让 api.ts 模块加载时创建的实例复用同一个 mock
const { mockedAxiosGet, mockedAxiosPost } = vi.hoisted(() => ({
  mockedAxiosGet: vi.fn(), mockedAxiosPost: vi.fn(),
}));

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => ({
      get: mockedAxiosGet,
      post: mockedAxiosPost,
      put: vi.fn(),
      delete: vi.fn(),
    })),
  },
}));

const createMock = vi.mocked(axios.create);
afterEach(() => mockedAxiosPost.mockReset());

test('升级等待后端 pull/up 和版本同步，其余动作保持原超时', async () => {
  mockedAxiosPost.mockResolvedValue({ data: { ok: true } });
  await runServiceAction('magpie', 'upgrade');
  expect(mockedAxiosPost).toHaveBeenLastCalledWith(
    '/service/action', { name: 'magpie', action: 'upgrade' }, { timeout: 420000 }
  );
  await runServiceAction('magpie', 'restart');
  expect(mockedAxiosPost).toHaveBeenLastCalledWith(
    '/service/action', { name: 'magpie', action: 'restart' }, { timeout: 60000 }
  );
});

describe('getEntryTraffic 指标采集时间', () => {
  test('旧后端缺字段归一 null', async () => {
    mockedAxiosGet.mockResolvedValueOnce({ data: { available: true, upstreams: [] } });
    expect((await getEntryTraffic()).metrics_sampled_at).toBeNull();
  });
  test('新字段透传且不是 API 读取时间', async () => {
    mockedAxiosGet.mockResolvedValueOnce({ data: {
      sampled_at: '2026-09-07T12:00:00Z',
      metrics_sampled_at: '2026-09-07T11:59:00Z',
    } });
    expect((await getEntryTraffic()).metrics_sampled_at).toBe('2026-09-07T11:59:00Z');
  });
});

describe('文件源采集时间兼容', () => {
  test.each([undefined, null, 42, {}])('缺失或非法时间 %s 不冒充接口时间', async (invalid) => {
    mockedAxiosGet.mockResolvedValueOnce({ data: {
      sampled_at: '2026-09-08T12:00:00Z', source_sampled_at: invalid,
    } });
    expect((await getTailnet()).source_sampled_at).toBeNull();
    mockedAxiosGet.mockResolvedValueOnce({ data: {
      sampled_at: '2026-09-08T12:00:00Z', upstreams_sampled_at: invalid,
    } });
    expect((await getEntryTraffic()).upstreams_sampled_at).toBeNull();
  });

  test('两个 JSON 源均保留与接口不同的独立采集时间', async () => {
    const source = '2026-09-08T11:59:00.125Z';
    mockedAxiosGet.mockResolvedValueOnce({ data: {
      sampled_at: '2026-09-08T12:00:00Z', source_sampled_at: source,
    } });
    expect((await getTailnet()).source_sampled_at).toBe(source);
    mockedAxiosGet.mockResolvedValueOnce({ data: {
      sampled_at: '2026-09-08T12:00:00Z', upstreams_sampled_at: source,
      metrics_sampled_at: '2026-09-08T11:58:00Z',
    } });
    const entry = await getEntryTraffic();
    expect(entry.upstreams_sampled_at).toBe(source);
    expect(entry.metrics_sampled_at).toBe('2026-09-08T11:58:00Z');
  });
});

function instanceGet() {
  const instance = createMock.mock.results[0].value as { get: typeof mockedAxiosGet };
  return instance.get;
}

describe('页面快照响应头', () => {
  test('服务列表保留数组接口，并读取采样与刷新状态', async () => {
    const get = instanceGet();
    get.mockResolvedValueOnce({
      data: [{ name: 'demo' }],
      headers: {
        'x-snapshot-at': '2026-09-29T01:00:00Z',
        'x-snapshot-refreshing': '1',
        'x-snapshot-error': '0',
      },
    });
    const result = await getServicesSnapshot();
    expect(get).toHaveBeenLastCalledWith('/services');
    expect(result.services).toEqual([{ name: 'demo' }]);
    expect(result.snapshot).toEqual({
      sampledAt: '2026-09-29T01:00:00Z', refreshing: true, error: false,
    });
  });

  test('监控强刷保留原 payload，缺少响应头按旧后端处理', async () => {
    const get = instanceGet();
    get.mockResolvedValueOnce({ data: { ok: true, systems: [] }, headers: {} });
    const result = await getMonitor(true);
    expect(get).toHaveBeenLastCalledWith('/monitor?force=1');
    expect(result).toEqual({
      ok: true, systems: [],
      snapshot: { sampledAt: null, refreshing: false, error: false },
    });
  });
});

describe('extractPathSuffix', () => {
  test('空值与非法 URL 返回空串', () => {
    expect(extractPathSuffix()).toBe('');
    expect(extractPathSuffix('')).toBe('');
    expect(extractPathSuffix('not a url')).toBe('');
  });

  test('根路径不产生后缀', () => {
    expect(extractPathSuffix('http://100.64.0.10:9080/')).toBe('');
    expect(extractPathSuffix('http://100.64.0.10:9080')).toBe('');
  });

  test('保留路径与 hash（SPA 面板常见形态）', () => {
    expect(extractPathSuffix('http://host:80/ui')).toBe('/ui');
    expect(extractPathSuffix('http://host:80/#/dashboard')).toBe('#/dashboard');
    expect(extractPathSuffix('http://host:80/ui/#/proxies')).toBe('/ui/#/proxies');
  });
});

describe('cn', () => {
  test('合并 class 并按 tailwind-merge 去冲突', () => {
    expect(cn('p-2', 'p-4')).toBe('p-4');
    const hiddenEnabled: boolean = false;
    expect(cn('text-sm', hiddenEnabled && 'hidden', 'font-bold')).toBe('text-sm font-bold');
  });
});

describe('容量来源声明', () => {
  test.each(['beszel', 'console_pg'] as const)('%s 来源不被归一化丢弃', async (source) => {
    mockedAxiosGet.mockResolvedValueOnce({ data: { ok: true, source, machines: {} } });
    expect((await getCapacity()).source).toBe(source);
  });
});

describe('getObservability（§4.2，D1）', () => {
  test('force=1 时请求 /observability?force=1，默认不带参数', async () => {
    const get = instanceGet();
    get.mockResolvedValue({ data: {} });
    await getObservability(true);
    expect(get).toHaveBeenCalledWith('/observability?force=1');
    get.mockClear();
    await getObservability(false);
    expect(get).toHaveBeenCalledWith('/observability');
  });

  test('数组字段缺失时归一为空数组，不把 undefined 透给组件', async () => {
    const get = instanceGet();
    get.mockResolvedValue({ data: { ok: false } });
    const out = await getObservability();
    expect(out.ok).toBe(false);
    expect(out.source_errors).toEqual([]);
    expect(out.active_alerts).toEqual([]);
    expect(out.recent_events).toEqual([]);
    expect(out.systemd_services).toEqual([]);
    expect(out.smart_devices).toEqual([]);
    expect(out.machine_links).toEqual({});
  });

  test('PG 来源与未覆盖能力声明保留到组件消费层', async () => {
    const capabilities = { systemd: 'unsupported', smart: 'unsupported' } as const;
    mockedAxiosGet.mockResolvedValueOnce({ data: { ok: true, source: 'console_pg', capabilities } });
    const out = await getObservability();
    expect(out.source).toBe('console_pg');
    expect(out.capabilities).toEqual(capabilities);
    expect(out.systemd_services).toEqual([]);
    expect(out.smart_devices).toEqual([]);
  });

  test('部分失败仍返回 HTTP 200 的既有数据（source_errors + 其余数组保留）', async () => {
    const get = instanceGet();
    const payload: ObservabilityPayload = {
      ok: false,
      updated_at: '2026-08-29T19:20:00Z',
      probe_interval: 600,
      source_errors: [{ source: 'systemd_services', message: 'HTTP 503' }],
      machine_links: { nas: '/beszel/system/abc' },
      active_alerts: [{ machine: 'aly', name: 'Status', severity: 'error', threshold: 0, duration_min: 5, triggered_at: '2026-08-29T18:37:42Z' }],
      recent_events: [],
      systemd_services: [],
      smart_devices: [],
    };
    get.mockResolvedValue({ data: payload });
    const out = await getObservability();
    expect(out.ok).toBe(false);
    expect(out.source_errors).toHaveLength(1);
    expect(out.source_errors[0].source).toBe('systemd_services');
    expect(out.active_alerts).toHaveLength(1);
    expect(out.active_alerts[0].machine).toBe('aly');
    expect(out.machine_links.nas).toBe('/beszel/system/abc');
  });
});

describe('getOpsStatus（A7 六键 + F17 第七键）', () => {
  test('七键 fixture 原样透传，含 last_deploy / last_probe_run / timed_backup', async () => {
    const get = instanceGet();
    get.mockResolvedValue({
      data: {
        running_commit: 'abc123',
        versions_last_checked: '2026-09-06 12:00:00',
        feed_last_fetched: '2026-09-06 12:05:00',
        last_backup_tag: '20260906-214411',
        last_deploy: {
          commit: 'abc123',
          backup_tag: '20260906-214411',
          deployed_at: '2026-09-06T21:44:11+08:00',
          migrations_applied: 10,
        },
        last_probe_run: '2026-09-07T02:30:00Z',
        timed_backup: {
          state: 'ok',
          last_attempt: '2026-09-08T04:10:12+08:00',
          last_success: '2026-09-08T04:10:12+08:00',
          snapshot: 'console.db.20260908',
          stage: null,
        },
      },
    });
    const out = await getOpsStatus();
    expect(out.running_commit).toBe('abc123');
    expect(out.versions_last_checked).toBe('2026-09-06 12:00:00');
    expect(out.feed_last_fetched).toBe('2026-09-06 12:05:00');
    expect(out.last_backup_tag).toBe('20260906-214411');
    expect(out.last_deploy).toEqual({
      commit: 'abc123',
      backup_tag: '20260906-214411',
      deployed_at: '2026-09-06T21:44:11+08:00',
      migrations_applied: 10,
    });
    expect(out.last_probe_run).toBe('2026-09-07T02:30:00Z');
    expect(out.timed_backup).toEqual({
      state: 'ok',
      last_attempt: '2026-09-08T04:10:12+08:00',
      last_success: '2026-09-08T04:10:12+08:00',
      snapshot: 'console.db.20260908',
      stage: null,
    });
  });

  test('旧四键 payload（无 last_deploy/last_probe_run 字段）归一为 null，向后兼容', async () => {
    const get = instanceGet();
    get.mockResolvedValue({
      data: {
        running_commit: null,
        versions_last_checked: null,
        feed_last_fetched: null,
        last_backup_tag: null,
      },
    });
    const out = await getOpsStatus();
    expect(out.last_deploy).toBeNull();
    expect(out.last_probe_run).toBeNull();
    expect(out.timed_backup).toBeNull();
  });

  test('last_deploy 对象内部分键缺失时逐键归一为 null，migrations_applied=0 不当空值丢弃', async () => {
    const get = instanceGet();
    get.mockResolvedValue({
      data: { last_deploy: { commit: 'x', migrations_applied: 0 } },
    });
    const out = await getOpsStatus();
    expect(out.last_deploy).toEqual({
      commit: 'x',
      backup_tag: null,
      deployed_at: null,
      migrations_applied: 0,
    });
  });

  test('data 为空对象时七键全部归一为 null', async () => {
    const get = instanceGet();
    get.mockResolvedValue({ data: {} });
    const out = await getOpsStatus();
    expect(out).toEqual({
      running_commit: null,
      versions_last_checked: null,
      feed_last_fetched: null,
      last_backup_tag: null,
      last_deploy: null,
      last_probe_run: null,
      timed_backup: null,
    });
  });

  test('F17：timed_backup 五键原样透传', async () => {
    const get = instanceGet();
    get.mockResolvedValue({
      data: {
        timed_backup: {
          state: 'stale',
          last_attempt: '2026-09-07T04:10:12+08:00',
          last_success: '2026-09-07T04:10:12+08:00',
          snapshot: 'console.db.20260907',
          stage: null,
        },
      },
    });
    const out = await getOpsStatus();
    expect(out.timed_backup).toEqual({
      state: 'stale',
      last_attempt: '2026-09-07T04:10:12+08:00',
      last_success: '2026-09-07T04:10:12+08:00',
      snapshot: 'console.db.20260907',
      stage: null,
    });
  });

  test('F17：timed_backup 缺失/非对象 → null，坏 state → unknown（不猜成正常）', async () => {
    const get = instanceGet();
    get.mockResolvedValue({ data: { timed_backup: null } });
    expect((await getOpsStatus()).timed_backup).toBeNull();

    get.mockResolvedValue({ data: { timed_backup: 'ok' } });
    expect((await getOpsStatus()).timed_backup).toBeNull();

    get.mockResolvedValue({ data: { timed_backup: { state: 'weird' } } });
    expect((await getOpsStatus()).timed_backup).toEqual({
      state: 'unknown',
      last_attempt: null,
      last_success: null,
      snapshot: null,
      stage: null,
    });
  });
});

describe('parseSystemdUnits（§4.1，D1）', () => {
  test('逗号/换行/全角逗号拆分，trim 去空', () => {
    expect(parseSystemdUnits('sing-box, nginx')).toEqual(['sing-box', 'nginx']);
    expect(parseSystemdUnits('sing-box\nnginx\ncronie')).toEqual(['sing-box', 'nginx', 'cronie']);
    expect(parseSystemdUnits('sing-box，docker.service')).toEqual(['sing-box', 'docker.service']);
    expect(parseSystemdUnits('  sing-box  ,  ')).toEqual(['sing-box']);
  });

  test('去重且保留首次出现顺序', () => {
    expect(parseSystemdUnits('sing-box\nsing-box\nnginx\nnginx')).toEqual(['sing-box', 'nginx']);
  });

  test('空串/纯分隔符返回空数组（空值发 []）', () => {
    expect(parseSystemdUnits('')).toEqual([]);
    expect(parseSystemdUnits(' ,\n， ')).toEqual([]);
  });
});

describe('saveMachine systemd_units payload（§4.1 / D1）', () => {
  function form(text: string, publicProbePort = ''): MachineFormValues {
    return {
      id: 'us',
      label: 'US',
      role: 'server',
      provider: '',
      os: '',
      region: '',
      public_host: '',
      tailscale: '',
      ssh: '',
      ssh_key: '',
      beszel_name: '',
      capabilities: [],
      probe_port: '',
      public_probe_port: publicProbePort,
      traffic_quota_gb: '',
      traffic_nic: '',
      renewal_ref: '',
      systemd_units_text: text,
      upgrade_enabled: false,
      upgrade_proxy_env_text: '',
    };
  }

  test('新增走 POST，systemd_units_text 清洗成数组（trim/去重/保留顺序）', async () => {
    const instance = createMock.mock.results[0].value as {
      post: ReturnType<typeof vi.fn>;
      put: ReturnType<typeof vi.fn>;
    };
    instance.post.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(' sing-box , sing-box\nnginx '));
    expect(instance.post).toHaveBeenCalledWith('/machines', expect.anything());
    const payload = instance.post.mock.calls[0][1] as Record<string, unknown>;
    expect(payload.systemd_units).toEqual(['sing-box', 'nginx']);
  });

  test('编辑走 PUT，空文本发 []（不丢字段也不误建白名单）', async () => {
    const instance = createMock.mock.results[0].value as {
      post: ReturnType<typeof vi.fn>;
      put: ReturnType<typeof vi.fn>;
    };
    instance.put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(''), 'us');
    expect(instance.put).toHaveBeenCalledWith('/machines/us', expect.anything());
    const payload = instance.put.mock.calls[0][1] as Record<string, unknown>;
    expect(payload.systemd_units).toEqual([]);
  });

  test('家宽公网探测端口：有值发 {type, port}，留空发 null（整份提交即清空）', async () => {
    const instance = createMock.mock.results[0].value as {
      post: ReturnType<typeof vi.fn>;
      put: ReturnType<typeof vi.fn>;
    };
    instance.put.mockClear();
    instance.put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form('', '62037'), 'us');
    await saveMachine(form(''), 'us');
    const [withPort, cleared] = instance.put.mock.calls.map(
      (call) => call[1] as Record<string, unknown>
    );
    expect(withPort.public_probe).toEqual({ type: 'tcp', port: '62037' });
    expect(cleared.public_probe).toBeNull();
  });
});

describe('upgrade payload（2026-10-22 机器级升级闸）', () => {
  function form(upgradeEnabled: boolean, proxyText: string): MachineFormValues {
    return {
      id: 'us',
      label: 'US',
      role: 'server',
      provider: '',
      os: '',
      region: '',
      public_host: '',
      tailscale: '',
      ssh: '',
      ssh_key: '',
      beszel_name: '',
      capabilities: [],
      probe_port: '',
      public_probe_port: '',
      traffic_quota_gb: '',
      traffic_nic: '',
      renewal_ref: '',
      systemd_units_text: '',
      upgrade_enabled: upgradeEnabled,
      upgrade_proxy_env_text: proxyText,
    };
  }

  function instance() {
    return createMock.mock.results[0].value as {
      post: ReturnType<typeof vi.fn>;
      put: ReturnType<typeof vi.fn>;
    };
  }

  /** 取最后一次 put 的 payload，并在此之前清掉累积调用记录（测试间隔离）。 */
  function lastPutPayload(): Record<string, unknown> {
    const calls = instance().put.mock.calls;
    const payload = calls[calls.length - 1][1] as Record<string, unknown>;
    instance().put.mockClear();
    return payload;
  }

  test('未勾且未填代理 → upgrade=null（不写回，机器页该机器服务不出 upgrade 按钮）', async () => {
    instance().put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(false, ''), 'us');
    expect(lastPutPayload().upgrade).toBeNull();
  });

  test('只勾未填代理 → upgrade={enabled:true}，不发 proxy_env', async () => {
    instance().put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(true, ''), 'us');
    expect(lastPutPayload().upgrade).toEqual({ enabled: true });
  });

  test('填代理且勾 → upgrade={enabled:true, proxy_env:{...}}', async () => {
    instance().put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(true, 'HTTPS_PROXY=http://p:7890\nNO_PROXY=localhost'), 'us');
    expect(lastPutPayload().upgrade).toEqual({
      enabled: true,
      proxy_env: { HTTPS_PROXY: 'http://p:7890', NO_PROXY: 'localhost' },
    });
  });

  test('解析容错：缺 = 号行与注释行被忽略', async () => {
    instance().put.mockResolvedValue({ data: { ok: true } });
    await saveMachine(form(true, '# comment\nBROKEN_LINE\nHTTPS_PROXY=http://p:1'), 'us');
    expect(
      (lastPutPayload().upgrade as { proxy_env: Record<string, string> }).proxy_env
    ).toEqual({ HTTPS_PROXY: 'http://p:1' });
  });
});

describe('formatProxyEnvText（编辑表单回填）', () => {
  test('null/undefined 返回空串', () => {
    expect(formatProxyEnvText(null)).toBe('');
    expect(formatProxyEnvText(undefined)).toBe('');
  });

  test('按键字典序稳定输出，每行 K=V', () => {
    expect(
      formatProxyEnvText({ NO_PROXY: 'localhost', HTTPS_PROXY: 'http://p:1' })
    ).toBe('HTTPS_PROXY=http://p:1\nNO_PROXY=localhost');
  });
});

describe('巡检报告客户端（C1）', () => {
  test('markdown 形态：显式 format=markdown，且关掉 axios 的 JSON 转换', async () => {
    const get = instanceGet();
    get.mockClear();
    get.mockResolvedValue({ data: '# 巡检报告\n\n## 总评\n' });

    await expect(getInspectionReportMarkdown()).resolves.toBe('# 巡检报告\n\n## 总评\n');

    const [url, config] = get.mock.calls[0] as [string, Record<string, unknown>];
    expect(url).toBe('/inspection/report');
    expect(config.params).toEqual({ format: 'markdown' });
    expect(config.responseType).toBe('text');
    // 报告正文以 # 开头，默认 transformResponse 会先试 JSON.parse；不能依赖它必然失败
    const transform = config.transformResponse as ((data: unknown) => unknown)[];
    expect(transform[0]('原样返回')).toBe('原样返回');
  });

  test('markdown 形态：拿到非字符串也不把 undefined 甩给调用方', async () => {
    const get = instanceGet();
    get.mockClear();
    get.mockResolvedValue({ data: undefined });
    await expect(getInspectionReportMarkdown()).resolves.toBe('');
  });

  test('JSON 形态：显式 format=json，原样透出 sections', async () => {
    const get = instanceGet();
    get.mockClear();
    const payload = {
      generated_at: '2026-09-06T12:00:00Z',
      summary: { verdict: '正常' },
      sections: { attention: { sampled_at: null, error: null, items: [] } },
    };
    get.mockResolvedValue({ data: payload });

    await expect(getInspectionReport()).resolves.toEqual(payload);
    expect(get.mock.calls[0][0]).toBe('/inspection/report');
    expect((get.mock.calls[0][1] as { params: unknown }).params).toEqual({ format: 'json' });
  });
});

describe('healthProbePayload（L1 三态 → 后端契约）', () => {
  const base = {
    name: 'api', machine: 'nas', port: '', purpose: '', priority: 'medium',
    note: '', deploy: '', home: '', tailscale: '', domain: '', repo_url: '',
    version_enabled: false, track_upstream: true,
    health_probe_mode: 'default' as const,
    health_probe_target: 'domain' as const,
    health_probe_status: '200', health_probe_latency: '', health_probe_body: '',
    tags: [],
  };

  test('关闭 → false；默认 → true（后端据此删键）', () => {
    expect(healthProbePayload({ ...base, health_probe_mode: 'off' })).toBe(false);
    expect(healthProbePayload({ ...base, health_probe_mode: 'default' })).toBe(true);
  });

  test('语义模式 → 对象，空串字段一律省略而不是发 0/空串', () => {
    expect(healthProbePayload({ ...base, health_probe_mode: 'http' })).toEqual({
      type: 'http',
      target: 'domain',
      expected_status: 200,
    });
  });

  test('填满四项都带上，且数字是 number 不是字符串', () => {
    expect(
      healthProbePayload({
        ...base,
        health_probe_mode: 'http',
        health_probe_target: 'tailscale',
        health_probe_status: '204',
        health_probe_latency: '1500',
        health_probe_body: '"status":"ok"',
      })
    ).toEqual({
      type: 'http',
      target: 'tailscale',
      expected_status: 204,
      max_latency_ms: 1500,
      body_contains: '"status":"ok"',
    });
  });

  test('状态码留空发显式 null（省略会被后端默认成 200）', () => {
    const spec = healthProbePayload({
      ...base, health_probe_mode: 'http', health_probe_status: '  ', health_probe_latency: '800',
    });
    expect(spec).toEqual({
      type: 'http', target: 'domain', expected_status: null, max_latency_ms: 800,
    });
  });
});
