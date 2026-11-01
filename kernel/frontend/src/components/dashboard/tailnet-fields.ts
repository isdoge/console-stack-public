/**
 * C8 / D4：tailnet 卡「fields 白名单可选」。
 *
 * 借鉴 gethomepage.dev Tailscale widget 的字段开关设计：显示列可挑，
 * 挑中的 key 列表存 localStorage `console.tailnet.fields`——
 * 与布局存储（`console.*dashboard*`）不同键，互不冲突；
 * 与 C10-a / configuration.md §7 的其余 `console.*` 偏好键同风格。
 *
 * 语义：
 * - 键 absent（没设过）= 默认全显示；这是「默认体验」与「显式关光」两种状态，
 *   与 hidden-model 的「空集合=删键」不同：这里**显式空集合是合法状态**
 *   （用户就要最小视图），因此存储形态区分「absent」与「空数组」。
 * - 坏 JSON / 非数组 / 非法项：降级成默认全显（不让显示偏好把卡搞挂）；
 * - 未知字段 key（未来字段被删）在解析时丢弃。
 */

export const TAILNET_FIELDS_STORAGE_KEY = 'console.tailnet.fields';

/** 可选字段及顺序（即默认展示顺序）。 */
export const TAILNET_FIELD_DEFS = [
  { key: 'os', label: '系统' },
  { key: 'update', label: '版本更新' },
  { key: 'expiry', label: 'key 到期' },
] as const;

export type TailnetFieldKey = (typeof TAILNET_FIELD_DEFS)[number]['key'];

const ALL_KEYS: TailnetFieldKey[] = TAILNET_FIELD_DEFS.map((d) => d.key);

function parseFields(raw: string | null | undefined): TailnetFieldKey[] | null {
  if (raw == null) return null; // 与「显式空数组」区分：absent → null → 默认全显
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const valid = new Set<string>(ALL_KEYS);
  const seen = new Set<TailnetFieldKey>();
  for (const item of parsed) {
    if (typeof item === 'string' && valid.has(item)) {
      seen.add(item as TailnetFieldKey);
    }
  }
  return ALL_KEYS.filter((key) => seen.has(key)); // 按定义顺序输出
}

/** 读字段白名单；absent / 坏数据 → null（调用方回退默认全显）。 */
export function readTailnetFields(storage?: Storage): TailnetFieldKey[] | null {
  try {
    const store = storage ?? localStorage;
    return parseFields(store.getItem(TAILNET_FIELDS_STORAGE_KEY));
  } catch {
    return null;
  }
}

/** 显式写入选中集合（含空数组）；localStorage 不可用（隐私模式等）静默降级。 */
export function writeTailnetFields(fields: TailnetFieldKey[], storage?: Storage): void {
  try {
    const store = storage ?? localStorage;
    const unique = Array.from(new Set(fields));
    // 按定义顺序落盘，让「显式选择」与「默认顺序」只有一种形态
    const ordered = ALL_KEYS.filter((key) => unique.includes(key));
    store.setItem(TAILNET_FIELDS_STORAGE_KEY, JSON.stringify(ordered));
  } catch {
    // 存不进就算了，本次会话内选择照常生效
  }
}
