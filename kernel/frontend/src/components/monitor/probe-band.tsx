import { cn } from '@/lib/utils';
import type { ProbeSample } from '@/lib/types';

/** 色带格数，与后端 node_probe.BAND_SIZE 对齐。 */
export const BAND_SIZE = 20;

/**
 * 耗时分档（TCP connect 往返，含 DNS）：≤100 / 200 / 500 / 1000 / >1000ms。
 */
export function latencyTone(latencyMs: number | null | undefined): string {
  if (latencyMs == null) return 'bg-signal-1';
  if (latencyMs <= 100) return 'bg-signal-1';
  if (latencyMs <= 200) return 'bg-signal-2';
  if (latencyMs <= 500) return 'bg-signal-3';
  if (latencyMs <= 1000) return 'bg-signal-4';
  return 'bg-signal-5';
}

function toneOf(sample: ProbeSample): string {
  return sample.status === 'up' ? latencyTone(sample.latency_ms) : 'bg-signal-5';
}

/**
 * 探测时间节点的本地显示，固定完整格式 `2026-09-03 03:00:00`（不带 T）。
 * ts 是后端的 UTC ISO 串，按浏览器时区换算——色带节点即探测时刻。
 */
export function probeNodeLabel(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts || '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

function tooltipOf(sample: ProbeSample): string {
  const when = probeNodeLabel(sample.ts);
  if (sample.status !== 'up') return `${when} · 探测失败`;
  return `${when} · ${sample.latency_ms != null ? `${sample.latency_ms} ms` : '可达'}`;
}

/** 成败带的每格：只有二值，up 绿 / down 红，不掺耗时（否则和延迟带重复表达）。 */
function failureToneOf(sample: ProbeSample): string {
  return sample.status === 'up' ? 'bg-signal-1' : 'bg-signal-5';
}

function failureTooltipOf(sample: ProbeSample): string {
  return `${probeNodeLabel(sample.ts)} · ${sample.status === 'up' ? '成功' : '失败'}`;
}

/**
 * 探测失败率（§4.3）：最近 BAND_SIZE 次里 `status !== 'up'` 的占比。
 * **不是丢包率**——单次 TCP connect 测不出丢包，命名上刻意不叫丢包（§4.3 末段）。
 * 无样本返回 null，由调用方决定整行是否渲染，不拿 0% 冒充「一切正常」。
 */
export function failureRate(samples?: ProbeSample[]): number | null {
  const recent = (samples || []).slice(-BAND_SIZE);
  if (recent.length === 0) return null;
  const down = recent.filter((s) => s.status !== 'up').length;
  return (down / recent.length) * 100;
}

/** 最近一次探测的耗时展示；最新一次失败时显示「失败」而不是上一次的耗时。 */
export function latestLatencyText(samples?: ProbeSample[]): string {
  const recent = (samples || []).slice(-BAND_SIZE);
  const last = recent[recent.length - 1];
  if (!last) return '—';
  if (last.status !== 'up') return '失败';
  return last.latency_ms != null ? `${last.latency_ms} ms` : '可达';
}

/** 失败率的语义色：0 绿，≤5% 黄，更高红。阈值取「20 次里错 1 次」为黄线。 */
export function failureRateTone(rate: number | null): string {
  if (rate == null || rate === 0) return 'text-muted-foreground';
  if (rate <= 5) return 'text-signal-4';
  return 'text-signal-5';
}

/** latency = 按耗时分五档；failure = 只分成败两色（§4.3 第二条带）。 */
export type BandMode = 'latency' | 'failure';

interface ProbeBandProps {
  samples?: ProbeSample[];
  /** 省略即延迟带，保持 E4 的既有行为 */
  mode?: BandMode;
}

/**
 * 最近探测色带（E4）。**不是 ping 图**：采样点由后端按时间节点调度
 * （默认每半小时一个节点，`CONSOLE_MACHINE_PROBE_INTERVAL` 可调），
 * 密度远低于 ICMP，所以只表达「最近 N 次探测的可用性与耗时」。
 * 不足 20 格时左侧留空位，保证最新一次永远在最右边。
 */
export function ProbeBand({ samples, mode = 'latency' }: ProbeBandProps) {
  const recent = (samples || []).slice(-BAND_SIZE);
  const padding = Math.max(0, BAND_SIZE - recent.length);
  const tone = mode === 'failure' ? failureToneOf : toneOf;
  const tip = mode === 'failure' ? failureTooltipOf : tooltipOf;
  const what = mode === 'failure' ? '探测成败' : '探测';

  return (
    <div
      className="flex items-end gap-[2px]"
      role="img"
      aria-label={recent.length ? `最近 ${recent.length} 次${what}` : `暂无${what}记录`}
      title={recent.length ? undefined : '还没有探测记录'}
    >
      {Array.from({ length: padding }).map((_, i) => (
        // 空位用 muted-foreground/20 而不是 bg-muted：后者在玻璃卡底上几乎看不见，
        // 色带会散成「几个悬空的彩色小块」（与 D 修骨架同一个坑）
        <span key={`pad-${i}`} className="h-4 flex-1 rounded-[2px] bg-muted-foreground/20" />
      ))}
      {recent.map((sample, i) => (
        <span
          key={`${sample.ts}-${i}`}
          className={cn('h-4 flex-1 rounded-[2px]', tone(sample))}
          title={tip(sample)}
        />
      ))}
    </div>
  );
}

/**
 * 色带行（§4.1 第四区）：左延迟、右探测失败率，两块等宽。
 * 无样本时整行不渲染，交给调用方判断——卡片不该出现一整行「—」。
 */
export function ProbeBandRow({ samples }: { samples?: ProbeSample[] }) {
  const rate = failureRate(samples);

  return (
    <div className="grid grid-cols-2 gap-2">
      <div className="flex flex-col gap-1 rounded-md bg-muted/40 p-2">
        <div className="flex items-baseline justify-between text-[11px] leading-none">
          <span className="text-muted-foreground">延迟</span>
          <span className="font-mono tabular-nums">{latestLatencyText(samples)}</span>
        </div>
        <ProbeBand samples={samples} />
      </div>
      <div
        className="flex flex-col gap-1 rounded-md bg-muted/40 p-2"
        title="最近 20 次探测里失败的占比。TCP connect 测不出丢包，所以这里不是丢包率"
      >
        <div className="flex items-baseline justify-between text-[11px] leading-none">
          <span className="text-muted-foreground">探测失败率</span>
          <span className={cn('font-mono tabular-nums', failureRateTone(rate))}>
            {rate != null ? `${rate.toFixed(1)}%` : '—'}
          </span>
        </div>
        <ProbeBand samples={samples} mode="failure" />
      </div>
    </div>
  );
}
