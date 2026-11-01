/** Shared device-summary DTO presentation; no plugin runtime dependency. */
import type { RenewalStatus } from './types';

export const RENEWAL_STATUS_LABELS: Record<RenewalStatus, string> = {
  active: '待续费',
  not_renewing: '不再续费',
  ended: '已结束',
};

export function isActiveRenewal(row: { status?: RenewalStatus }): boolean {
  return (row.status ?? 'active') === 'active';
}
